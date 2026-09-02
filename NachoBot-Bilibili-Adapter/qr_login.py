from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import qrcode
import requests
import toml as toml_writer

try:
    import tomllib as toml_reader
except ImportError:  # pragma: no cover
    import toml as toml_reader  # type: ignore


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
PRIMARY_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID")


def _is_nonblank_string(value: object) -> bool:
    """Return whether a credential value contains non-whitespace text."""
    return isinstance(value, str) and bool(value.strip())


def load_config(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if hasattr(toml_reader, "loads"):
        return toml_reader.loads(raw.decode("utf-8"))
    return toml_reader.load(path)  # type: ignore[attr-defined]


def save_config(path: Path, data: dict[str, Any]) -> None:
    payload = toml_writer.dumps(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def generate_qr(session: requests.Session) -> dict[str, str]:
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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish only after the encoder has completed.  The WebUI reads this
    # fixed path while the helper is running, so a direct write could expose a
    # truncated PNG to a concurrent request.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        img.save(temp_path, format="PNG")
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    print("QR image ready (URL hidden).")
    try:
        qr.print_ascii(invert=True)
    except Exception:  # noqa: BLE001 - terminal may not support ASCII preview
        print("QR preview unavailable; scan the published image.")


def poll_login(session: requests.Session, qrcode_key: str, timeout_seconds: int = 180) -> dict[str, Any]:
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


def extract_cookie(session: requests.Session, name: str) -> str | None:
    return session.cookies.get(name)


def fetch_buvid(session: requests.Session) -> dict[str, str]:
    try:
        resp = session.get(BUVID_URL, timeout=10, headers=DEFAULT_HEADERS)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {
            "buvid3": data.get("b_3", "") or "",
            "buvid4": data.get("b_4", "") or "",
        }
    except Exception:  # noqa: BLE001 - buvid 是可选补充数据
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

    primary_credentials = tuple(
        extract_cookie(session, name) or "" for name in PRIMARY_COOKIE_NAMES
    )
    sessdata, bili_jct, dede_user_id = primary_credentials

    # Do not merge a partial current login with credentials left by an older
    # attempt.  The config is still untouched when any primary cookie is
    # absent or blank.
    if not all(isinstance(value, str) and value.strip() for value in primary_credentials):
        print("Login succeeded, but required credentials were not returned; config unchanged.")
        return 1

    buvid = fetch_buvid(session)
    updated_fields = ["SESSDATA", "bili_jct", "DedeUserID"]

    bilibili_cfg = config.setdefault("bilibili", {})
    if sessdata:
        bilibili_cfg["sessdata"] = sessdata
    if bili_jct:
        bilibili_cfg["bili_jct"] = bili_jct
    if dede_user_id:
        bilibili_cfg["dede_user_id"] = dede_user_id
    for field in ("buvid3", "buvid4"):
        fetched_value = buvid.get(field)
        if _is_nonblank_string(fetched_value) and not _is_nonblank_string(
            bilibili_cfg.get(field)
        ):
            bilibili_cfg[field] = fetched_value
            updated_fields.append(field)

    save_config(config_path, config)
    print("Login success. Updated config.toml.")
    print(f"Stored credential fields: {', '.join(updated_fields) or 'none'} (values hidden).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
