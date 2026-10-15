#!/usr/bin/env python3
"""Read-only metadata check for a prepared Windows audio-route guard config.

This helper never starts or inspects a live process, opens an audio endpoint,
loads a native module, changes device settings, or sends network traffic.
Endpoint IDs and generation values are deliberately omitted from its output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import sys
from typing import Any


SUPPORTED_BUILD = "9.9.26-44343"
MAX_CONFIG_BYTES = 16 * 1024
MAX_ID_CHARS = 1024
MAX_IMAGE_CHARS = 1024
MAX_PID = 0xFFFFFFFF
MAX_FILETIME = 0xFFFFFFFFFFFFFFFF
MAX_SAFE_GENERATION = 0x1FFFFFFFFFFFFF


def _absolute_file(value: Any, field: str) -> pathlib.Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field}_invalid")
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise ValueError(f"{field}_must_be_absolute")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{field}_not_file")
    if len(str(resolved)) >= MAX_IMAGE_CHARS:
        raise ValueError(f"{field}_too_long")
    return resolved


def _bounded_id(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) >= MAX_ID_CHARS or "\x00" in value:
        raise ValueError(f"{field}_invalid")


def validate_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("config_version_invalid")
    if document.get("qqBuild") != SUPPORTED_BUILD:
        raise ValueError("qq_build_unverified")
    for field in ("parentPid", "childPid", "hostGeneration", "parentCreationTime", "childCreationTime"):
        value = document.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{field}_invalid")
        maximum = MAX_PID if field.endswith("Pid") else (
            MAX_SAFE_GENERATION if field == "hostGeneration" else MAX_FILETIME
        )
        if value > maximum:
            raise ValueError(f"{field}_out_of_range")
    if document["parentPid"] == document["childPid"]:
        raise ValueError("pid_binding_invalid")
    parent_image = _absolute_file(document.get("parentImage"), "parent_image")
    child_image = _absolute_file(document.get("childImage"), "child_image")
    if parent_image.name.casefold() != "qq.exe" or child_image.name.casefold() != "qq.exe":
        raise ValueError("image_name_unverified")
    _bounded_id(document.get("captureEndpointId"), "capture_endpoint_id")
    _bounded_id(document.get("renderEndpointId"), "render_endpoint_id")
    if document["captureEndpointId"] == document["renderEndpointId"]:
        raise ValueError("endpoint_ids_must_differ")
    return {
        "version": 1,
        "qqBuild": SUPPORTED_BUILD,
        "parentImage": str(parent_image),
        "childImage": str(child_image),
        "endpointIdsPresent": True,
        "generationPresent": True,
    }


def inspect_artifact(path_value: str) -> dict[str, Any]:
    path = _absolute_file(path_value, "artifact")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "sizeBytes": path.stat().st_size, "sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="prepared private schema-1 JSON path")
    parser.add_argument("--node-artifact", required=True)
    parser.add_argument("--dll-artifact", required=True)
    args = parser.parse_args(argv)
    if platform.system() != "Windows":
        print(json.dumps({"ready": False, "code": "windows_required"}, separators=(",", ":")))
        return 2
    config_path = pathlib.Path(args.config).resolve(strict=True)
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ValueError("config_too_large")
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("config_invalid") from exc
    config = validate_config(document)
    node = inspect_artifact(args.node_artifact)
    dll = inspect_artifact(args.dll_artifact)
    print(json.dumps({"ready": True, "config": config, "artifacts": {"node": node, "dll": dll}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        code = str(error) if isinstance(error, ValueError) else "input_unavailable"
        print(json.dumps({"ready": False, "code": code}, separators=(",", ":")))
        raise SystemExit(2)
