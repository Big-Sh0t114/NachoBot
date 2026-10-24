from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import qrcode
import requests
import toml as toml_writer

try:
    import tomllib as toml_reader
except ImportError:  # pragma: no cover
    import toml as toml_reader  # type: ignore


QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
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
    resp = session.get(QR_GENERATE_URL, timeout=10, headers=DEFAULT_HEADERS, allow_redirects=False)
    resp.raise_for_status()
    payload = resp.json()
    if resp.status_code != 200 or payload.get("code") != 0:
        raise ValueError("QR generation failed")
    data = payload.get("data", {})
    parsed = urlsplit(data.get("url", ""))
    if parsed.scheme != "https" or parsed.hostname not in {"passport.bilibili.com", "account.bilibili.com"} or parsed.username or parsed.password:
        raise ValueError("Unexpected QR destination")
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
    # The QR contains a login ticket: publish the image only, not terminal logs.


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
            allow_redirects=False,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {})
        if payload.get("code") != 0 or not isinstance(data, dict):
            return {"status": "api_error"}
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
    # Only cookies eligible for api.bilibili.com root requests. Never silently
    # choose between conflicting domain/path values or passport-only cookies.
    matches = [c for c in session.cookies if c.name == name
               and c.domain.lstrip(".").lower() in {"bilibili.com", "api.bilibili.com"}
               and c.path == "/" and not c.is_expired()]
    values = {c.value for c in matches}
    if len(values) > 1:
        raise ValueError("Conflicting official-domain cookies; config unchanged.")
    return next(iter(values), None)


def diagnose_login(session: requests.Session, result: dict[str, Any]) -> None:
    data = result.get("data") or {}
    print(json.dumps({"poll_data_keys": sorted(data) if isinstance(data, dict) else [],
                      "cookies": [{"name": c.name, "domain": c.domain,
                                   "path": c.path, "present": bool(c.value)}
                                  for c in session.cookies]}, ensure_ascii=True))


def validate_and_save(session: requests.Session, config_path: Path,
                      config: dict[str, Any]) -> bool:
    try:
        credentials = tuple(extract_cookie(session, name) or "" for name in PRIMARY_COOKIE_NAMES)
    except ValueError:
        print("Conflicting login cookies; config unchanged.")
        return False
    if not all(_is_nonblank_string(value) for value in credentials):
        print("Required login cookies missing; config unchanged.")
        return False
    sessdata, bili_jct, uid = credentials
    # Use exactly the candidate credentials that will be saved, in the same
    # fresh session. Do not mix them with cookies from the old configuration.
    headers = dict(DEFAULT_HEADERS)
    headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in zip(PRIMARY_COOKIE_NAMES, credentials))
    try:
        response = session.get(NAV_URL, headers=headers, timeout=10, allow_redirects=False)
        if response.status_code != 200:
            print(f"Login verification HTTP status: {response.status_code}; config unchanged.")
            return False
        payload = response.json()
        data = payload.get("data") or {}
        valid = (payload.get("code") == 0 and data.get("isLogin") is True
                 and str(data.get("mid")) == uid)
        print(json.dumps({"nav_code": payload.get("code"),
                          "isLogin": data.get("isLogin"),
                          "account_matches": str(data.get("mid")) == uid}))
        if not valid:
            print("Login verification rejected candidate cookies; config unchanged.")
            return False
    except Exception:
        # Requests exceptions can include sensitive query strings. Never print them.
        print("Login verification failed; config unchanged.")
        return False
    updated = copy.deepcopy(config)
    section = updated.setdefault("bilibili", {})
    section.update(sessdata=sessdata, bili_jct=bili_jct, dede_user_id=uid)
    buvid = fetch_buvid(session)
    for field in ("buvid3", "buvid4"):
        # No stale device identifier from an unrelated login attempt.
        section[field] = extract_cookie(session, field) or buvid.get(field) or ""
    save_config(config_path, updated)
    return True


def fetch_buvid(session: requests.Session) -> dict[str, str]:
    try:
        resp = session.get(BUVID_URL, timeout=10, headers=DEFAULT_HEADERS, allow_redirects=False)
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

    diagnose_login(session, result)
    if not validate_and_save(session, config_path, config):
        return 1
    print("Login verified. Updated config.toml (credential values hidden).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("Login helper failed; sensitive diagnostic details suppressed.")
        sys.exit(1)
