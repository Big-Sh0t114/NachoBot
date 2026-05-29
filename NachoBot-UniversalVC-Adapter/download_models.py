"""
Standalone script to download all required ML models.

Usage:
    python download_models.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_manager import ModelManager


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger("ModelDownloader")

    models_dir = Path(__file__).resolve().parent / "models"
    mgr = ModelManager(models_dir=str(models_dir), logger=logger)

    logger.info("=" * 50)
    logger.info("  NachoBot Universal VC Adapter - Model Downloader")
    logger.info("=" * 50)

    if mgr.ensure_all():
        logger.info("All models are ready!")
        paths = mgr.get_model_paths()
        for name, path in paths.items():
            logger.info(f"  {name}: {path}")
    else:
        logger.error("Some models failed to download. Check errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
