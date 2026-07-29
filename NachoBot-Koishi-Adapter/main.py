# ruff: noqa: E402
import asyncio
import sys
from pathlib import Path

# Add the NachoBot core path so the adapter uses the canonical ncnk_message.
NACHOBOT_PATH = Path(__file__).resolve().parents[1] / "NachoBot"
if (NACHOBOT_PATH / "ncnk_message").is_dir():
    nachobot_path = str(NACHOBOT_PATH)
    if nachobot_path not in sys.path:
        sys.path.insert(1, nachobot_path)

from config import load_config, setup_logging
from adapter import KoishiOneBotAdapter


async def main() -> None:
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    logger = setup_logging(config.log_level)
    adapter = KoishiOneBotAdapter(config, logger)
    await adapter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
