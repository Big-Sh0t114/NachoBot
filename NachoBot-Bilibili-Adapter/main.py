"""Entry point for NachoBot Bilibili Adapter.

This is a modular adapter that bridges Bilibili live streaming with NachoBot.

Modules:
- config.py: Configuration dataclasses and loading functions
- utils.py: Text processing utilities
- api.py: BilibiliApi and WbiSigner classes for API calls
- live_worker.py: LiveRoomWorker for WebSocket connections
- screen_monitor.py: ScreenMonitor for VLM-based screen analysis
- adapter.py: BilibiliAdapter main class
- mic_capture.py: MicCaptureWorker for ASR (external)
"""

import asyncio
import logging
from pathlib import Path

from bili_src.core.config import AdapterConfig, load_config

BUILD_TAG = "bilibili-adapter-v2.0-modular"


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Set up logging with the specified level."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("BilibiliAdapter")


async def main() -> None:
    """Main entry point for the adapter."""
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    logger = setup_logging(config.log_level)
    logger.info(f"Adapter build tag: {BUILD_TAG}")

    # Import adapter here to avoid circular imports
    from adapter import BilibiliAdapter

    adapter = BilibiliAdapter(config, logger, config_path)
    await adapter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
