"""Select the one optional QQCall adapter dependency from canonical config.

This startup helper deliberately loads only the repository's fixed public
configuration validator. It does not import the adapter, QQCall service, or
audio transport.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = frozenset(("napcat", "snowluma"))
_EXTRA = "qqcall-audio"
_NONE = "none"
_WARNING = "[WARN] QQCall audio extra unavailable; using ordinary adapter startup."


def _load_validator(validator_path: Path):
    if not validator_path.is_file():
        raise OSError
    spec = importlib.util.spec_from_file_location(
        "_nachobot_qqcall_adapter_extra_validator",
        validator_path,
    )
    if spec is None or spec.loader is None:
        raise ImportError
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator


def _select_extra(adapter: object) -> tuple[str, bool]:
    """Return (extra token, config-unavailable flag) without exposing details."""
    if type(adapter) is not str or adapter not in _ADAPTERS:
        return _NONE, True

    validator_path = REPO_ROOT / "NachoBot-QQCall" / "service" / "config.py"
    public_config_path = (
        REPO_ROOT / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    )
    try:
        config = _load_validator(validator_path).load_config(public_config_path)
        if (
            isinstance(config, dict)
            and config.get("enabled") is True
            and config.get("adapter") in ("auto", adapter)
        ):
            return _EXTRA, False
    except Exception:
        return _NONE, True
    return _NONE, False


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    adapter: object = None
    valid_arguments = (
        len(args) == 2
        and args[0] == "--adapter"
        and args[1] in _ADAPTERS
    )
    if valid_arguments:
        adapter = args[1]

    token, unavailable = _select_extra(adapter)
    print(token)
    if unavailable:
        print(_WARNING, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
