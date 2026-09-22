# ruff: noqa: E402
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Keep the same deployment convention as the existing NachoBot adapters:
# <repo>/NachoBot and <repo>/NachoBot-SnowLuma-Adapter are siblings.
_NACHOBOT_PATH = Path(__file__).resolve().parents[1] / "NachoBot"
if (_NACHOBOT_PATH / "ncnk_message").is_dir():
    path = str(_NACHOBOT_PATH)
    if path not in sys.path:
        sys.path.insert(1, path)

from src import __version__
from src.bridge import SnowLumaBridge
from src.logger import logger
from src.windows_console import register_console_close_handler


async def amain() -> None:
    bridge = SnowLumaBridge()
    logger.info(f"NachoBot-SnowLuma-Adapter v{__version__}")
    try:
        await bridge.run()
    finally:
        await bridge.stop()


def main() -> None:
    if sys.platform == "win32" and not register_console_close_handler():
        logger.debug("Windows console close handler unavailable; continuing startup")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.warning("收到中断信号，适配器已停止")


if __name__ == "__main__":
    main()
