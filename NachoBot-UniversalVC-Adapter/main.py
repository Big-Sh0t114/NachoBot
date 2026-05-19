import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from config import load_config
from adapter import UniversalVCAdapter


def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("UniversalVCAdapter")


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

    # Auto-configure FFmpeg if found in shared NachoBot plugins
    project_root = current_dir.parent
    possible_ffmpeg_paths = [
        project_root / "NachoBot" / "plugins" / "bilibili_video_sender_plugin" / "ffmpeg",
        current_dir / "ffmpeg",
    ]

    for p in possible_ffmpeg_paths:
        if p.exists() and p.is_dir():
            if (p / "bin").exists():
                p = p / "bin"
            logger.info(f"Found FFmpeg at {p}, adding to PATH")
            os.environ["PATH"] += os.pathsep + str(p)
            break

    logger.info("=" * 60)
    logger.info("  NachoBot Universal Voice Adapter")
    logger.info("  Platform: universal_vc")
    logger.info("=" * 60)
    logger.info(f"Target Process: {config.capture.target_process_name or config.capture.target_pid}")
    logger.info(f"Output Device: {config.output.device_name}")
    logger.info(f"NachoBot Core: ws://{config.nachobot.host}:{config.nachobot.port}/ws")
    logger.info(f"STT Enabled: {config.stt.enabled}")
    logger.info("TTS Handler: GPT-SoVITS (from NachoBot-TTS-Adapter)")

    adapter = UniversalVCAdapter(config, logger)

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
