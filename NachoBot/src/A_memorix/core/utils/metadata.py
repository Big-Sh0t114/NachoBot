from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict


def coerce_metadata_dict(value: Any) -> Dict[str, Any]:
    """返回字典，如果输入值不是字典则返回空字典。"""
    if isinstance(value, Mapping):
        return dict(value)
    return {}
