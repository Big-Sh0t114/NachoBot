import argparse
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import qrcode
import requests

try:
    import tomllib as toml_reader
except ImportError:  # pragma: no cover
    import toml as toml_reader  # type: ignore

import toml as toml_writer


QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
BUVID_URL = "https://api.bilibili.com/x/frontend/finger/spi"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def load_config(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if hasattr(toml_reader, "loads"):
        return toml_reader.loads(raw.decode("utf-8"))
    return toml_reader.load(path)  # type: ignore[attr-defined]


def save_config(path: Path, data: Dict[str, Any]) -> None:
    payload = toml_writer.dumps(data)
    path.write_text(payload, encoding="utf-8")


def generate_qr(session: requests.Session) -> Dict[str, str]:
    resp = session.get(QR_GENERATE_URL, timeout=10, headers=DEFAULT_HEADERS)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    return {
        "url": data.get("url", ""),
        "qrcode_key": data.get("qrcode_key", ""),
    }


def print_qr(url: str, output_path: Path) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output_path)
    print(f"QR saved to: {output_path}")
    try:
        qr.print_ascii(invert=True)
    except Exception:
        print(f"QR URL: {url}")


def poll_login(session: requests.Session, qrcode_key: str, timeout_seconds: int = 180) -> Dict[str, Any]:
    start = time.time()
    while True:
        if time.time() - start > timeout_seconds:
            return {"status": "expired"}
        resp = session.get(
            QR_POLL_URL,
            params={"qrcode_key": qrcode_key},
            timeout=10,
            headers=DEFAULT_HEADERS,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {})
        code = data.get("code")
        if code == 0:
            return {"status": "success", "data": data}
        if code == 86038:
            return {"status": "expired"}
        if code == 86090:
            print("Scanned, waiting for confirmation...")
        elif code == 86101:
            print("Waiting for scan...")
        else:
            print(f"Login status: {code}")
        time.sleep(2)


def extract_cookie(session: requests.Session, name: str) -> Optional[str]:
    return session.cookies.get(name)


def fetch_buvid(session: requests.Session) -> Dict[str, str]:
    try:
        resp = session.get(BUVID_URL, timeout=10, headers=DEFAULT_HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {
            "buvid3": data.get("b_3", "") or "",
            "buvid4": data.get("b_4", "") or "",
        }
    except Exception:
        return {"buvid3": "", "buvid4": ""}


def main() -> int:
    parser = argparse.ArgumentParser(description="Bilibili QR login helper")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "config.toml"),
        help="Path to config.toml",
    )
    parser.add_argument(
        "--qr-output",
        default=str(Path(__file__).parent / "qr_login.png"),
        help="Output path for QR image",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return 1
    config = load_config(config_path)

    session = requests.Session()
    info = generate_qr(session)
    if not info["url"] or not info["qrcode_key"]:
        print("Failed to generate QR.")
        return 1
    print_qr(info["url"], Path(args.qr_output))
    result = poll_login(session, info["qrcode_key"])
    if result.get("status") != "success":
        print("QR login failed or expired.")
        return 1

    sessdata = extract_cookie(session, "SESSDATA") or ""
    bili_jct = extract_cookie(session, "bili_jct") or ""
    dede_user_id = extract_cookie(session, "DedeUserID") or ""
    buvid = fetch_buvid(session)

    bilibili_cfg = config.setdefault("bilibili", {})
    if sessdata:
        bilibili_cfg["sessdata"] = sessdata
    if bili_jct:
        bilibili_cfg["bili_jct"] = bili_jct
    if dede_user_id:
        bilibili_cfg["dede_user_id"] = dede_user_id
    if buvid.get("buvid3") and not bilibili_cfg.get("buvid3"):
        bilibili_cfg["buvid3"] = buvid["buvid3"]
    if buvid.get("buvid4") and not bilibili_cfg.get("buvid4"):
        bilibili_cfg["buvid4"] = buvid["buvid4"]

    save_config(config_path, config)
    print("Login success. Updated config.toml.")
    print(f"SESSDATA: {sessdata}")
    print(f"bili_jct: {bili_jct}")
    print(f"DedeUserID: {dede_user_id}")
    if buvid.get("buvid3"):
        print(f"buvid3: {buvid['buvid3']}")
    if buvid.get("buvid4"):
        print(f"buvid4: {buvid['buvid4']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
