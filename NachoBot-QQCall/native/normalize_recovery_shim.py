"""Reproduce one pinned, unsigned Shim release; never accept a different DLL."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
from pathlib import Path


def validate_release(release: dict) -> None:
    if (type(release) is not dict or set(release) != {"schema", "artifact", "sha256", "size", "pe_offset",
                                                   "timestamp", "timestamp_offsets", "msvc_toolset"}
            or type(release["schema"]) is not int or release["schema"] != 1
            or release["artifact"] != "QQCallRecoveryShim2.dll"
            or type(release["size"]) is not int or release["size"] != 16896
            or type(release["pe_offset"]) is not int or release["pe_offset"] != 200
            or type(release["timestamp"]) is not int or not 0 <= release["timestamp"] <= 0xffffffff
            or type(release["timestamp_offsets"]) is not list
            or any(type(offset) is not int for offset in release["timestamp_offsets"])
            or release["timestamp_offsets"] != [208, 14612]
            or type(release["sha256"]) is not str or not re.fullmatch(r"[0-9A-F]{64}", release["sha256"])
            or release["msvc_toolset"] != "14.44.35207"):
        raise ValueError("recovery_release_profile_invalid")


def normalize(data: bytes, release: dict) -> bytes:
    validate_release(release)
    if len(data) != release["size"] or data[:2] != b"MZ":
        raise ValueError("recovery_build_format_mismatch")
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    if pe != release["pe_offset"] or data[pe:pe+4] != b"PE\0\0" or \
            struct.unpack_from("<H", data, pe + 4)[0] != 0x8664 or \
            struct.unpack_from("<H", data, pe + 24)[0] != 0x20B:
        raise ValueError("recovery_build_format_mismatch")
    offsets = release["timestamp_offsets"]
    if offsets != [pe + 8, 14612] or struct.unpack_from("<I", data, offsets[1] + 8)[0] != 13:
        raise ValueError("recovery_build_metadata_mismatch")
    # These two locations are the PE COFF and VC-feature debug timestamps of
    # this exact release. Every other byte must still match its original hash.
    candidate = bytearray(data)
    for offset in offsets:
        struct.pack_into("<I", candidate, offset, release["timestamp"])
    if hashlib.sha256(candidate).hexdigest().upper() != release["sha256"]:
        raise ValueError("recovery_build_hash_mismatch")
    return bytes(candidate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    try:
        release = json.loads(Path(__file__).with_name("recovery-shim-release.json").read_text(encoding="utf-8"))
        validate_release(release)
        if args.artifact.name != release["artifact"] or args.artifact.is_symlink():
            raise ValueError("recovery_build_path_invalid")
        if args.artifact.stat().st_size != release["size"]:
            raise ValueError("recovery_build_format_mismatch")
        original = args.artifact.read_bytes()
        candidate = normalize(original, release)
        if candidate != original:
            args.artifact.write_bytes(candidate)
        print(json.dumps({"verified": True, "sha256": release["sha256"], "normalized": candidate != original}))
        return 0
    except (OSError, ValueError, KeyError, struct.error):
        print(json.dumps({"verified": False, "code": "recovery_build_verification_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
