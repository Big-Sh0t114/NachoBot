import asyncio
import logging
import os
import sys
from pathlib import Path

# Add dependency path if needed, though usually handled by venv
sys.path.append(str(Path(__file__).resolve().parent))

from config import load_config
from adapter import DiscordAdapter


class CryptoErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "CryptoError" in record.getMessage() or (record.exc_info and "CryptoError" in str(record.exc_info[0])):
            return False
        return True


def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Suppress verbose CryptoError from pycord voice receiver (known harmless issue)
    discord_voice_logger = logging.getLogger("discord.voice.receive.reader")
    discord_voice_logger.addFilter(CryptoErrorFilter())
    
    return logging.getLogger("DiscordVCAdapter")


async def main():
    current_dir = Path(__file__).resolve().parent
    config_path = current_dir / "config.toml"

    if not config_path.exists():
        # Fallback to example if not found (or error out)
        # For first run, user needs to rename example
        example_path = current_dir / "config.toml.example"
        if example_path.exists():
            print(f"Please copy {example_path} to {config_path} and configure it.")
            return
        else:
            print("Config file not found.")
            return

    try:
        config = load_config(config_path)
    except Exception as e:
        print(f"Failed to load config: {e}")
        return

    logger = setup_logging(config.log_level)

    # Auto-configure FFmpeg if found in shared NachoBot plugins
    # Dynamically resolve based on the sibling directory structure
    project_root = current_dir.parent
    possible_ffmpeg_paths = [
        # Relative path attempt 1: Sibling NachoBot directory
        project_root
        / "NachoBot"
        / "plugins"
        / "bilibili_video_sender_plugin"
        / "ffmpeg",
        # Relative path attempt 2: Local adapter directory (just in case)
        current_dir / "ffmpeg",
    ]

    for p in possible_ffmpeg_paths:
        if p.exists() and p.is_dir():
            # Check if it has bin or is bin
            if (p / "bin").exists():
                p = p / "bin"

            logger.info(f"Found FFmpeg at {p}, adding to PATH")
            os.environ["PATH"] += os.pathsep + str(p)
            break

    # Configure Proxy for Discord if enabled
    # We now pass proxy explicitly to Discord Client and ASR handler
    # instead of setting global env vars, to respect "independence".
    if config.discord.proxy_enabled and config.discord.proxy_url:
        logger.info(f"Proxy enabled: {config.discord.proxy_url} (Independent Mode)")
    else:
        logger.info("Proxy disabled in config")

    logger.info("Starting NachoBot Discord Voice Adapter...")

    adapter = DiscordAdapter(config, logger)

    try:
        await adapter.run()
    except KeyboardInterrupt:
        logger.info("Stopping...")
        await adapter.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        await adapter.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
