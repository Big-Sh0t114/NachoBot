"""Ensure the Chromium revision required by the active Playwright is usable."""

from __future__ import annotations

import argparse
import subprocess
import sys


def _probe_chromium() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return False, f"Playwright Python package is unavailable: {exc}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                return True, browser.version
            finally:
                browser.close()
    except Exception as exc:
        return False, str(exc)


def ensure_chromium(*, with_deps: bool = False) -> bool:
    ready, detail = _probe_chromium()
    if ready:
        print(f"[Playwright] Chromium ready: {detail}", flush=True)
        return True

    print(f"[Playwright] Chromium unavailable, installing the required revision: {detail}", flush=True)
    command = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        command.append("--with-deps")
    command.append("chromium")

    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        print(
            f"[Playwright] Chromium installation failed with exit code {completed.returncode}",
            file=sys.stderr,
            flush=True,
        )
        return False

    ready, detail = _probe_chromium()
    if not ready:
        print(
            f"[Playwright] Chromium still cannot launch after installation: {detail}",
            file=sys.stderr,
            flush=True,
        )
        return False

    print(f"[Playwright] Chromium installed and verified: {detail}", flush=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-deps",
        action="store_true",
        help="also install Linux system dependencies through Playwright",
    )
    args = parser.parse_args()
    return 0 if ensure_chromium(with_deps=args.with_deps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
