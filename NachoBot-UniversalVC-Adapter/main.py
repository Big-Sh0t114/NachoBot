import asyncio
import logging
import sys
from pathlib import Path

from loguru import logger

sys.path.append(str(Path(__file__).resolve().parent))

from multimodal_bridge import ensure_multimodal_import

ensure_multimodal_import()

from nachobot_multimodal.asr.onnxruntime_compat import preload_onnxruntime  # noqa: E402

# Prevent Windows' older System32 onnxruntime.dll from shadowing the venv copy.
preload_onnxruntime()

from config import load_config
from adapter import UniversalVCAdapter


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
    logger.configure(extra={"name": "NachoBot-UniversalVC-Adapter"})
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
    return logger.bind(name="NachoBot-UniversalVC-Adapter")


async def main():
    current_dir = Path(__file__).resolve().parent
    config_path = current_dir / "config.toml"

    if not config_path.exists():
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

    # ── Download models if needed ──
    logger.info("Checking ML models...")
    try:
        from model_manager import ModelManager
        models_dir = current_dir / "models"
        mgr = ModelManager(models_dir=str(models_dir), logger=logger)
        if not mgr.ensure_all():
            logger.warning(
                "Some models could not be downloaded. "
                "Features may be degraded. Run `python download_models.py` manually."
            )
    except Exception as e:
        logger.warning(f"Model check failed: {e}")

    logger.info("=" * 60)
    logger.info("  NachoBot Universal Voice Adapter")
    logger.info("  Platform: universal_vc")
    logger.info("=" * 60)
    logger.info(f"Target Process: {config.capture.target_process_name or config.capture.target_pid}")
    logger.info(f"Output Device: {config.output.device_name}")
    logger.info(f"NachoBot Core: ws://{config.nachobot.host}:{config.nachobot.port}/ws")
    logger.info(f"Denoise: {'ON' if config.denoise.enabled else 'OFF'}")
    logger.info(f"Speaker Tracking: {'ON' if config.speaker.enabled else 'OFF'}")
    logger.info("ASR: shared Multimodal streaming engine")
    logger.info(f"STT Remote API: {'ON' if config.stt.enabled else 'OFF (fallback)'}")
    logger.info(f"Microphone Capture: {'ON' if config.microphone.enabled else 'OFF'}")
    if config.microphone.enabled and config.microphone.push_to_talk:
        logger.info(f"  Push-to-Talk: ON (key: {config.microphone.ptt_key})")
    elif config.microphone.enabled:
        logger.info(f"  Push-to-Talk: OFF (continuous capture)")
    logger.info("TTS Handler: GPT-SoVITS (from NachoBot-Multimodal-Adapter)")

    adapter = UniversalVCAdapter(config, logger)

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
