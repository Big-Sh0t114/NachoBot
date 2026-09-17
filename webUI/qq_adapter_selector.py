"""Shared parser and resolver for the NachoBot QQ backend selector.

The selector deliberately has a tiny contract: ``qq_adapter`` is the only
setting this module interprets.  The parser scans the complete ``.env`` text,
ignores comments and unrelated variables, and never includes selector values
or any other environment contents in its error messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

SELECTOR_KEY: Final[str] = "qq_adapter"
SUPPORTED_QQ_ADAPTERS: Final[frozenset[str]] = frozenset({"napcat", "snowluma"})
DEFAULT_QQ_ADAPTER: Final[str] = "napcat"


class QQAdapterSelectorError(ValueError):
    """Raised when the QQ selector is missing a valid, unambiguous value."""


def _selector_values(env_text: str) -> list[str]:
    """Return raw selector values without exposing them to callers/logs."""
    if not isinstance(env_text, str):
        raise QQAdapterSelectorError(".env 内容无效")

    values: list[str] = []
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().casefold() == SELECTOR_KEY:
            values.append(value.strip())
    return values


def parse_qq_adapter_env(
    env_text: str,
    *,
    missing_default: str = DEFAULT_QQ_ADAPTER,
) -> str:
    """Validate full ``.env`` text and return the normalized QQ adapter.

    A missing selector is intentionally compatible with older deployments and
    resolves to ``napcat`` by default.  A present selector must be non-blank,
    known, and unique using a case-insensitive key comparison.
    """
    normalized_default = str(missing_default or "").strip().casefold()
    if normalized_default not in SUPPORTED_QQ_ADAPTERS:
        raise QQAdapterSelectorError("缺少 QQ 适配器时的默认值无效")

    values = _selector_values(env_text)
    if not values:
        return normalized_default
    if len(values) != 1:
        raise QQAdapterSelectorError(".env 中 qq_adapter 重复")

    value = values[0].strip().casefold()
    if not value:
        raise QQAdapterSelectorError(".env 中 qq_adapter 不能为空")
    if value not in SUPPORTED_QQ_ADAPTERS:
        raise QQAdapterSelectorError(".env 中 qq_adapter 无效")
    return value


def resolve_qq_adapter(
    source: str | Path,
    *,
    missing_default: str = DEFAULT_QQ_ADAPTER,
) -> str:
    """Resolve either full ``.env`` text or a selector file path."""
    if isinstance(source, Path):
        return read_qq_adapter(source, missing_default=missing_default)
    return parse_qq_adapter_env(source, missing_default=missing_default)


def read_qq_adapter(
    path: str | Path,
    *,
    missing_default: str = DEFAULT_QQ_ADAPTER,
) -> str:
    """Read and resolve a selector file without returning any raw contents."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parse_qq_adapter_env("", missing_default=missing_default)
    except OSError as exc:
        raise QQAdapterSelectorError("无法读取 QQ 适配器选择") from exc
    return parse_qq_adapter_env(text, missing_default=missing_default)


def validate_qq_adapter_env(env_text: str) -> None:
    """Validate selector syntax for API/configuration write paths."""
    parse_qq_adapter_env(env_text)


def selector_value_from_request(value: object) -> str:
    """Validate a request-supplied selector value without accepting blanks."""
    if not isinstance(value, str) or not value.strip():
        raise QQAdapterSelectorError("qq_adapter 不能为空")
    return parse_qq_adapter_env(f"{SELECTOR_KEY}={value}")


# Descriptive compatibility aliases for callers that use noun-oriented names.
parse_qq_adapter = parse_qq_adapter_env
load_qq_adapter = read_qq_adapter


__all__ = [
    "DEFAULT_QQ_ADAPTER",
    "QQAdapterSelectorError",
    "SELECTOR_KEY",
    "SUPPORTED_QQ_ADAPTERS",
    "parse_qq_adapter_env",
    "parse_qq_adapter",
    "read_qq_adapter",
    "resolve_qq_adapter",
    "load_qq_adapter",
    "selector_value_from_request",
    "validate_qq_adapter_env",
]
