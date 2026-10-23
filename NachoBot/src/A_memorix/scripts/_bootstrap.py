from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = CURRENT_DIR.parent
SRC_ROOT = PLUGIN_ROOT.parent
PROJECT_ROOT = SRC_ROOT.parent
WORKSPACE_ROOT = PROJECT_ROOT
MAIBOT_ROOT = PROJECT_ROOT

for _path in (SRC_ROOT, PROJECT_ROOT, PLUGIN_ROOT):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

_PATHS_SPEC = importlib.util.spec_from_file_location(
    "_nachobot_a_memorix_paths",
    PLUGIN_ROOT / "paths.py",
)
if _PATHS_SPEC is None or _PATHS_SPEC.loader is None:
    raise ImportError(f"Unable to load A_Memorix path helpers from {PLUGIN_ROOT / 'paths.py'}")
_PATHS_MODULE = importlib.util.module_from_spec(_PATHS_SPEC)
_PATHS_SPEC.loader.exec_module(_PATHS_MODULE)

config_path = _PATHS_MODULE.config_path
default_data_dir = _PATHS_MODULE.default_data_dir
resolve_repo_path = _PATHS_MODULE.resolve_repo_path

DEFAULT_CONFIG_PATH = config_path()
DEFAULT_DATA_DIR = default_data_dir()
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "MaiBot.db"
