import asyncio
import logging
import sys
from pathlib import Path

from loguru import logger

# Add dependency path if needed, though usually handled by venv
sys.path.append(str(Path(__file__).resolve().parent))

from config import load_config
from adapter import DiscordAdapter


class CryptoErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            "CryptoError" in record.getMessage()
            or (record.exc_info and "CryptoError" in str(record.exc_info[0]))
        )


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.bind(name=record.name).opt(
            depth=depth,
            exception=record.exc_info,
        ).log(level, record.getMessage())


def setup_logging(level: str = "INFO"):
    normalized_level = level.upper()
    logger.remove()
    logger.configure(extra={"name": "NachoBot-DiscordVC-Adapter"})
    logger.add(
        sys.stderr,
        level=normalized_level,
        colorize=True,
        format=(
            "<blue>{time:YYYY-MM-DD HH:mm:ss}</blue> | "
            "<level>{level: <8}</level> | "
            "<cyan>{extra[name]}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )

    logging.basicConfig(
        handlers=[_InterceptHandler()],
        level=getattr(logging, normalized_level, logging.INFO),
        force=True,
    )

    # Suppress verbose CryptoError from pycord voice receiver (known harmless issue)
    discord_voice_logger = logging.getLogger("discord.voice.receive.reader")
    discord_voice_logger.addFilter(CryptoErrorFilter())

    return logger.bind(name="NachoBot-DiscordVC-Adapter")


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
        logger.exception(f"Fatal error: {e}")
        await adapter.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
