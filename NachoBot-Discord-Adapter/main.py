"""Discord adapter process entry point with one-time migration and singleton locks."""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

from loguru import logger

sys.path.append(str(Path(__file__).resolve().parent))

from adapter import DiscordAdapter
from config import load_config
from migration import _default_paths, migrate_legacy_config, needs_migration
from process_lock import SingletonProcessLock


class CryptoErrorRateLimitFilter(logging.Filter):
    """Keep the first receive CryptoError visible and summarize repeat bursts."""

    def __init__(self, interval: float = 30.0):
        super().__init__()
        self.interval = max(1.0, float(interval))
        self._lock = threading.Lock()
        self._last_report_at: float | None = None
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        is_crypto_error = (
            "CryptoError" in record.getMessage()
            or (record.exc_info and "CryptoError" in str(record.exc_info[0]))
        )
        if not is_crypto_error:
            return True

        now = time.monotonic()
        with self._lock:
            if self._last_report_at is None:
                self._last_report_at = now
                return True
            self._suppressed += 1
            if now - self._last_report_at < self.interval:
                return False

            repeats = self._suppressed
            self._suppressed = 0
            self._last_report_at = now
            message = record.getMessage()
            record.msg = f"{message} (CryptoError repeats since previous report: {repeats})"
            record.args = ()
            return True


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
    logger.configure(extra={"name": "NachoBot-Discord-Adapter"})
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
    reader_logger = logging.getLogger("discord.voice.receive.reader")
    reader_logger.filters[:] = [
        item
        for item in reader_logger.filters
        if not isinstance(item, CryptoErrorRateLimitFilter)
    ]
    reader_logger.addFilter(CryptoErrorRateLimitFilter())
    return logger.bind(name="NachoBot-Discord-Adapter")


def _legacy_inputs_present(paths: dict[str, Path]) -> bool:
    keys = (
        "legacy_discordvc_config_path",
        "legacy_koishi_adapter_config_path",
        "koishi_config_path",
        "koishi_db_path",
    )
    return any(paths[key].is_file() for key in keys)


def _migrate_before_start(root: Path, config_path: Path) -> bool:
    """Migrate missing/v1 targets only; current v2 startup never reads Koishi."""
    try:
        if config_path.exists() and not needs_migration(config_path):
            return False
    except Exception as exc:
        raise RuntimeError("Existing Discord adapter config is invalid; startup stopped") from exc

    paths = _default_paths(root)
    if not _legacy_inputs_present(paths):
        return False
    result = migrate_legacy_config(**paths)
    if not result.success or result.error:
        raise RuntimeError(result.error or "One-time Discord config migration failed")
    if result.migrated:
        print(
            "One-time Discord migration completed: "
            f"users={result.user_count}, channels={result.channel_count}, "
            f"private_channels={result.private_channel_count}"
        )
    return result.migrated


async def main():
    adapter_dir = Path(__file__).resolve().parent
    root_dir = adapter_dir.parent
    config_path = adapter_dir / "config.toml"
    try:
        _migrate_before_start(root_dir, config_path)
    except Exception as exc:
        print(f"Discord adapter migration failed ({type(exc).__name__})")
        raise SystemExit(1) from None

    if not config_path.is_file():
        example_path = adapter_dir / "config.toml.example"
        print(f"Discord adapter config is missing. Copy {example_path} to {config_path} and configure it.")
        raise SystemExit(2)

    try:
        config = load_config(config_path)
    except Exception as exc:
        print(f"Failed to load Discord adapter config ({type(exc).__name__})")
        raise SystemExit(1) from None

    adapter_logger = setup_logging(config.log_level)
    if config.discord.proxy_enabled and config.discord.proxy_url:
        adapter_logger.info("Discord proxy enabled; URL is kept out of logs")
    else:
        adapter_logger.info("Discord proxy disabled")

    token = config.discord.token
    shared_lock = SingletonProcessLock(
        Path(tempfile.gettempdir()) / "NachoBot" / "discord",
        token,
    )
    volume_lock = SingletonProcessLock(adapter_dir / "data", token)
    locks = (shared_lock, volume_lock)
    adapter = None
    acquired = []
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        adapter_logger.info("Starting the single-process Discord adapter")
        adapter = DiscordAdapter(config, adapter_logger)
        await adapter.run()
    except KeyboardInterrupt:
        adapter_logger.info("Stopping Discord adapter")
    except Exception as exc:
        adapter_logger.error("Discord adapter stopped after error (%s)", type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        if adapter is not None:
            try:
                await adapter.stop()
            except Exception as exc:
                adapter_logger.warning("Discord adapter cleanup failed (%s)", type(exc).__name__)
        for lock in reversed(acquired):
            lock.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
