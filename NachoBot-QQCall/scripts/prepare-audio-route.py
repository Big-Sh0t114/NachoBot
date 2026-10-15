"""Dry-run or create the missing private audio guard; never bind or start QQ."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from audio.windows_routes import enumerate_active_endpoints
from service.audio_routes import describe_virtual_endpoints
from service.guard_setup import prepare_guard


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT.parent / "NachoBot/plugins/qqcall_plugin/config.toml")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "native/audio_route/build/artifact-manifest.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()
    canonical = ROOT.parent / "NachoBot/plugins/qqcall_plugin/config.toml"
    try:
        if args.config.resolve() != canonical.resolve():
            raise ValueError("config_path_noncanonical")
        if args.list_devices:
            print(json.dumps({"ok": True, "virtualEndpoints": describe_virtual_endpoints(enumerate_active_endpoints())}))
            return 0
        print(json.dumps(prepare_guard(args.config, args.artifacts, apply=args.apply)))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        code = getattr(error, "code", None)
        if code is None and isinstance(error, ValueError) and error.args:
            code = error.args[0]
        if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 80:
            code = "audio_guard_preparation_failed"
        print(json.dumps({"ok": False, "code": code, "changed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
