"""Load the sibling Multimodal Adapter under a stable package name."""

import importlib.util
from pathlib import Path
import sys


MULTIMODAL_ADAPTER_DIR = (
    Path(__file__).resolve().parents[1] / "NachoBot-Multimodal-Adapter"
)
MULTIMODAL_PACKAGE_NAME = "nachobot_multimodal"


def ensure_multimodal_import() -> Path:
    """Expose Multimodal's ``src`` package as ``nachobot_multimodal``.

    Both adapters historically use a top-level package named ``src``. Loading
    the sibling package through an explicit alias avoids importing the wrong
    one while keeping Multimodal as the sole owner of the ASR implementation.
    """
    if MULTIMODAL_PACKAGE_NAME in sys.modules:
        return MULTIMODAL_ADAPTER_DIR

    source_dir = MULTIMODAL_ADAPTER_DIR / "src"
    init_path = source_dir / "__init__.py"
    if not init_path.is_file():
        raise ImportError(
            "NachoBot-Multimodal-Adapter is required for shared streaming ASR: "
            f"{MULTIMODAL_ADAPTER_DIR}"
        )

    spec = importlib.util.spec_from_file_location(
        MULTIMODAL_PACKAGE_NAME,
        init_path,
        submodule_search_locations=[str(source_dir)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load Multimodal package from {init_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[MULTIMODAL_PACKAGE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(MULTIMODAL_PACKAGE_NAME, None)
        raise
    return MULTIMODAL_ADAPTER_DIR
