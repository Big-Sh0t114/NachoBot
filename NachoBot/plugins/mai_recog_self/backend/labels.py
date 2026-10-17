"""Stable labels and strict schema-2 model metadata validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any


RESERVED_LABELS = {"self", "skip", "unlabelled"}
WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


def validate_class_name(value: Any) -> str:
    """Return an NFC class label after rejecting unsafe or reserved names.

    ``other`` is the one reserved class name that is valid for a multiclass
    catalog.  ``self`` remains reserved for the legacy binary task.
    """
    if not isinstance(value, str):
        raise ValueError("类别名称必须是文本")
    name = unicodedata.normalize("NFC", value)
    if not 1 <= len(name) <= 64:
        raise ValueError("类别名称长度必须为1到64个字符")
    if name != name.strip() or name.endswith((".", " ")):
        raise ValueError("类别名称不能以空格或点开头/结尾")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in name):
        raise ValueError("类别名称不能包含控制或格式字符")
    if any(char in '<>:"/\\|?*' for char in name):
        raise ValueError("类别名称不能包含路径分隔符或文件系统保留字符")
    if name in {".", ".."} or WINDOWS_RESERVED.fullmatch(name):
        raise ValueError("类别名称是保留名称")
    folded = name.casefold()
    if folded in {label.casefold() for label in RESERVED_LABELS}:
        raise ValueError(f"{name}是保留标签，不能作为角色类别")
    if folded == "other" and name != "other":
        raise ValueError("背景类别请使用固定名称 other")
    if folded == "self":
        raise ValueError("self仅用于旧版二分类任务，请为角色使用其他名称")
    return name


def ordered_class_names(values: list[str] | tuple[str, ...] | set[str], *, require_other: bool = False) -> list[str]:
    """Sort labels deterministically and keep ``other`` last when present."""
    names = [validate_class_name(value) for value in values]
    folded: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in folded and folded[key] != name:
            raise ValueError(f"类别名称大小写冲突：{folded[key]} / {name}")
        folded[key] = name
    unique = set(names)
    if len(unique) > 128:
        raise ValueError("多类别模型最多支持128个类别")
    if require_other and "other" not in unique:
        raise ValueError("多类别任务需要背景类别 other")
    if require_other and len(unique) < 2:
        raise ValueError("多类别任务至少需要一个角色类别和 other")
    return sorted(unique - {"other"}) + (["other"] if "other" in unique else [])


def metadata_digest(metadata: dict[str, Any]) -> str:
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_multiclass_metadata(metadata: Any) -> dict[str, Any]:
    """Validate the complete schema-2 mapping and thresholds without torch."""
    if not isinstance(metadata, dict):
        raise ValueError("模型类别元数据格式无效")
    if metadata.get("format_version") != 2 or metadata.get("task") != "multiclass":
        raise ValueError("模型任务类型或格式版本无效")
    try:
        class_names = ordered_class_names(metadata.get("class_names", []), require_other=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"模型类别映射无效：{exc}") from exc
    if class_names != metadata.get("class_names"):
        raise ValueError("模型类别映射未按稳定顺序保存")
    score_threshold = metadata.get("score_threshold")
    margin_threshold = metadata.get("margin_threshold")
    if isinstance(score_threshold, bool) or not isinstance(score_threshold, (int, float)) or not math.isfinite(score_threshold) or not 0 <= score_threshold <= 1:
        raise ValueError("模型分数阈值必须是0到1之间的有限数")
    if isinstance(margin_threshold, bool) or not isinstance(margin_threshold, (int, float)) or not math.isfinite(margin_threshold) or not 0 <= margin_threshold <= 1:
        raise ValueError("模型边际阈值必须是0到1之间的有限数")
    return {
        "format_version": 2,
        "task": "multiclass",
        "class_names": class_names,
        "score_threshold": float(score_threshold),
        "margin_threshold": float(margin_threshold),
    }
