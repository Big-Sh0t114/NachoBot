"""Emit exact GetProcessTimes identity for a single Windows PID."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.windows import WindowsServiceError, WindowsSystem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("pid", type=int)
    args = parser.parse_args(argv)
    if os.name != "nt" or args.pid <= 0 or args.pid > 0x7FFFFFFF:
        print(json.dumps({"ok": False, "code": "process_identity_unavailable"}, separators=(",", ":")))
        return 2
    try:
        identity = WindowsSystem().process_identity(args.pid)
    except WindowsServiceError:
        identity = None
    if identity is None:
        print(json.dumps({"ok": False, "code": "process_identity_unavailable"}, separators=(",", ":")))
        return 2
    print(json.dumps({"ok": True, "pid": args.pid, "creationTime100ns": str(identity[0])}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
