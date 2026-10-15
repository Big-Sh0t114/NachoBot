"""Prepare a missing machine-local guard from the canonical public configuration."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from audio.windows_routes import enumerate_active_endpoints

from service.audio_routes import (
    audio_route_identity,
    resolve_audio_route,
    select_audio_guard_route,
)
from service.config import load_config
from service.private_files import (
    atomic_private_json_if_absent,
    is_reparse_point,
    read_bounded_bytes,
)


def artifact_guard(manifest_path: Path) -> dict[str, Any]:
    document = json.loads(read_bounded_bytes(manifest_path, 16384).decode("utf-8-sig"))
    if (type(document) is not dict or set(document) != {"schema", "qqBuild", "artifacts"}
            or type(document["schema"]) is not int or document["schema"] != 1
            or document["qqBuild"] != "9.9.26-44343" or type(document["artifacts"]) is not list
            or len(document["artifacts"]) != 2):
        raise ValueError("audio_guard_manifest_invalid")
    records = {}
    for item in document["artifacts"]:
        if (type(item) is not dict or set(item) != {"name", "sha256", "bytes"}
                or type(item["name"]) is not str
                or item["name"] not in {"QQCallAudioRouteGuard-v2.node", "QQCallAudioRouteGuard-v2.dll"}
                or item["name"] in records or type(item["bytes"]) is not int or not 1 <= item["bytes"] <= 16*1024*1024
                or type(item["sha256"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])):
            raise ValueError("audio_guard_manifest_invalid")
        path = manifest_path.resolve().parent / item["name"]
        if is_reparse_point(path) or not path.is_file() or path.stat().st_size != item["bytes"]:
            raise ValueError("audio_guard_artifact_invalid")
        value = read_bounded_bytes(path, 16*1024*1024)
        if hashlib.sha256(value).hexdigest() != item["sha256"]:
            raise ValueError("audio_guard_artifact_mismatch")
        records[item["name"]] = (str(path), item["sha256"])
    node = records["QQCallAudioRouteGuard-v2.node"]
    dll = records["QQCallAudioRouteGuard-v2.dll"]
    return {"version": 1, "qqBuild": document["qqBuild"], "hostGeneration": 1,
            "nodeArtifact": node[0], "nodeSha256": node[1], "guardDllPath": dll[0], "dllSha256": dll[1]}


def prepare_guard(config_path: Path, manifest_path: Path, *, apply: bool = False,
                  inventory: Callable[[], Any] = enumerate_active_endpoints,
                  project_root: Path | None = None) -> dict[str, Any]:
    from service.manager import Supervisor, _validate_guard

    original = read_bounded_bytes(config_path, 65536)
    config = load_config(config_path)
    if not config["enabled"]:
        return {"ok": True, "phase": "disabled", "changed": False}
    target = Path(config["native"]["guard_config_file"])
    if target.exists() or is_reparse_point(target):
        _validate_guard(config, inventory)
        return {"ok": True, "phase": "existing_guard_valid", "changed": False}
    runtime = Path(config["runtime_dir"]).resolve()
    if not target.resolve().is_relative_to(runtime) or is_reparse_point(runtime):
        raise ValueError("audio_guard_output_outside_runtime")
    Supervisor(config, project_root=project_root)._validate_private_runtime_location()
    base = artifact_guard(manifest_path)
    overrides = {key: config["audio"][key] for key in ("capture_device", "playback_device")}
    route = select_audio_guard_route(base, inventory(), overrides)
    if read_bounded_bytes(config_path, 65536) != original:
        raise ValueError("audio_guard_configuration_changed")
    if not apply:
        return {"ok": True, "phase": "guard_prepared", "changed": False}
    current = resolve_audio_route(route["guard"], inventory(), overrides)
    if audio_route_identity(current) != audio_route_identity(route) or artifact_guard(manifest_path) != base:
        raise ValueError("audio_guard_preparation_changed")
    if read_bounded_bytes(config_path, 65536) != original:
        raise ValueError("audio_guard_configuration_changed")
    created = atomic_private_json_if_absent(target, route["guard"])
    if not created:
        _validate_guard(config, inventory)
    return {"ok": True, "phase": "guard_created" if created else "existing_guard_valid", "changed": created}
