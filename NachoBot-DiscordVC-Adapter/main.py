import asyncio
import logging
import sys
from pathlib import Path

# Add dependency path if needed, though usually handled by venv
sys.path.append(str(Path(__file__).parent))

from config import load_config
from adapter import DiscordAdapter

def setup_logging(level: str = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("DiscordVCAdapter")

async def main():
    config_path = Path(__file__).parent / "config.toml"
    if not config_path.exists():
        # Fallback to example if not found (or error out)
        # For first run, user needs to rename example
        example_path = Path(__file__).parent / "config.toml.example"
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
    # Try multiple common locations
    possible_ffmpeg_paths = [
        # User reported path
        Path(r"C:\Users\BigSh0t\Nacho-with-u\NachoBot\plugins\bilibili_video_sender_plugin\ffmpeg"),
        # Relative path attempt
        Path(__file__).parent.parent / "NachoBot" / "plugins" / "bilibili_video_sender_plugin" / "ffmpeg",
    ]

    import os
    for p in possible_ffmpeg_paths:
        if p.exists() and p.is_dir():
            # Check if it has bin or is bin
            if (p / "bin").exists():
                p = p / "bin"
            
            logger.info(f"Found FFmpeg at {p}, adding to PATH")
            os.environ["PATH"] += os.pathsep + str(p)
            break
            
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
