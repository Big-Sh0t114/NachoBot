import asyncio
import sys
from pathlib import Path

# Add core paths to sys.path so the adapter can find ncnk_message
ROOT_DIR = Path(__file__).resolve().parents[1]
for candidate in ("NachoBot", "NachoBot-Napcat-Adapter", "NachoBot-TTS-Adapter"):
    candidate_path = ROOT_DIR / candidate
    if candidate_path.exists():
        candidate_str = str(candidate_path)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)

from config import load_config, setup_logging, BUILD_TAG
from adapter import KoishiOneBotAdapter


async def main() -> None:
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    logger = setup_logging(config.log_level)
    logger.info(f"Adapter build tag: {BUILD_TAG}")
    adapter = KoishiOneBotAdapter(config, logger)
    await adapter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
