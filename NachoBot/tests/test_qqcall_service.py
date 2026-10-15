from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import asyncio
import stat
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import toml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qqcall_client.qqcall import QQCallConfig, QQCallRuntime
from qqcall_client import qqcall_service
from qqcall_client.qqcall_service import (
    QQCallPlatformBindingHeartbeat,
    canonical_qqcall_config_path,
    is_qqcall_enabled,
    load_qqcall_adapter_settings,
    read_qqcall_public_config,
)


SERVICE_CONFIG = Path(__file__).resolve().parents[2] / "NachoBot-QQCall" / "service" / "config.py"


@pytest.fixture
def private_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "NachoBot-QQCall"
    root.mkdir()
    monkeypatch.setattr(qqcall_service, "_SERVICE_PROJECT_ROOT", root)
    return root


@pytest.fixture
def private_runtime_dir(private_project_root: Path) -> Path:
    return private_project_root / ".runtime" / "napcat"


def _fixed_config_module():
    spec = importlib.util.spec_from_file_location("_qqcall_service_test_config", SERVICE_CONFIG)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _public_config(runtime_dir: Path, *, enabled: bool = True, adapter: str = "napcat") -> dict:
    module = _fixed_config_module()
    value = copy.deepcopy(module.DEFAULT_CONFIG)
    value["plugin"]["enabled"] = enabled
    qqcall = value["qqcall"]
    qqcall.update({
        "adapter": adapter,
        "account_id": "12345" if enabled else "",
        "identity_file": "",
        "runtime_dir": str(runtime_dir),
    })
    qqcall["onebot"]["url"] = "ws://127.0.0.1:3001/ws" if enabled else ""
    qqcall["paths"]["qq_exe"] = r"C:\QQ\QQ.exe" if enabled else ""
    if enabled:
        qqcall["audio"]["capture_device"] = "Virtual Output (loopback)"
        qqcall["audio"]["playback_device"] = "Virtual Input"
    qqcall["peer_map"] = {"u_" + "A" * 22: "12345"}
    return value


def _adapter_config(path: Path, reference: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[qqcall]\nservice_config_file = "{reference}"\n', encoding="utf-8")


def _write_canonical_config(_root: Path, value: dict) -> Path:
    path = canonical_qqcall_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(toml.dumps(value), encoding="utf-8")
    return path


def test_canonical_path_and_root_switch_override_adapter_legacy_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    runtime_dir = private_runtime_dir
    public_path = _write_canonical_config(private_project_root, _public_config(runtime_dir))
    _adapter_config(adapter_config, "../ignored-decoy.json")
    elsewhere = tmp_path / "different-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = load_qqcall_adapter_settings(
        "napcat", adapter_config,
        {"enabled": False, "audio": {"enabled": False}, "account_id": "", "peer_map": {}},
    )

    assert canonical_qqcall_config_path() == (
        private_project_root.parent / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    )
    assert read_qqcall_public_config()["enabled"] is True
    assert is_qqcall_enabled() is True
    assert settings.error_code is None
    assert settings.config.enabled is True
    assert settings.config.audio["enabled"] is True
    assert settings.config.account_id == "12345"
    assert settings.config.endpoint == "http://127.0.0.1:6120"
    assert settings.config.peer_map == {"u_" + "A" * 22: "12345"}
    assert settings.config.peer_map_file == str(runtime_dir / "adapter-runtime.private.json")
    assert settings.config.token_file == str(runtime_dir / "control.token.private")
    assert settings.shared_config_file == public_path.resolve()
    assert settings.config.shared_config_file == str(public_path.resolve())
    token = (runtime_dir / "control.token.private").read_text(encoding="ascii")
    assert len(token.strip()) >= 40
    if os.name != "nt":
        assert (runtime_dir / "control.token.private").stat().st_mode & 0o777 == 0o600


def test_legacy_json_switch_is_ignored_and_audio_enablement_derives_from_plugin_switch(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    path = _write_canonical_config(private_project_root, _public_config(private_runtime_dir))
    legacy_json = private_project_root / "config.json"
    legacy_json.write_text(json.dumps({"schema": 2, "enabled": False}), encoding="utf-8")

    normalized = read_qqcall_public_config()
    settings = load_qqcall_adapter_settings("napcat", path.with_name("missing-adapter.toml"), {})

    assert normalized["audio"]["enabled"] is True
    assert settings.config.enabled is True
    assert settings.config.audio["enabled"] is True
    assert json.loads(legacy_json.read_text(encoding="utf-8"))["enabled"] is False


@pytest.mark.parametrize(
    ("capture_override", "playback_override", "expected_capture", "expected_playback"),
    (
        ("", "", "", ""),
        ("Virtual Output [loopback]", "", "Virtual Output", ""),
        ("", "Virtual Input", "", "Virtual Input"),
    ),
)
def test_enabled_service_audio_keeps_optional_route_overrides_pending(
    private_project_root: Path,
    private_runtime_dir: Path,
    tmp_path: Path,
    capture_override: str,
    playback_override: str,
    expected_capture: str,
    expected_playback: str,
) -> None:
    value = _public_config(private_runtime_dir)
    value["qqcall"]["audio"].update({
        "capture_device": capture_override,
        "playback_device": playback_override,
    })
    path = _write_canonical_config(private_project_root, value)
    settings = load_qqcall_adapter_settings("napcat", tmp_path / "adapter.toml", QQCallConfig())

    assert settings.error_code is None
    assert settings.config.enabled is True
    assert settings.platform_manifest_file == private_runtime_dir / "platform-napcat.private.json"
    assert settings.config.audio["enabled"] is True
    assert settings.config.audio["auto_resolve"] is True
    assert settings.config.audio["capture_device"] == expected_capture
    assert settings.config.audio["playback_device"] == expected_playback
    assert settings.config.audio["capture_mode"] == "loopback"
    assert settings.config.audio["device_scope"] == {
        "capture": "virtual",
        "playback": "virtual",
    }
    assert settings.config.peer_map_file == str(private_runtime_dir / "adapter-runtime.private.json")
    assert settings.shared_config_file == path.resolve()
    runtime = QQCallRuntime(
        settings.config,
        "qq",
        lambda _message: asyncio.sleep(0),
        lambda _kind, _message: asyncio.sleep(0),
        lambda: {"connected": False, "account_id": "", "identity": ()},
        lambda _peer: True,
    )
    audio_status = runtime.status()["audio"]
    assert runtime._voice._backend is None
    assert runtime._voice._backend_factory is not None
    assert audio_status["configured"] is True
    assert audio_status["ready"] is False
    assert audio_status["unavailableReason"] == "audio_route_pending"
    assert runtime.status()["lastFailureCode"] is None


def _audio_plan_fixture(runtime_dir: Path) -> tuple[Path, dict, dict, tuple[int, int]]:
    service_meta = {
        "serviceInstance": "723e4567-e89b-42d3-a456-426614174000",
        "hostGeneration": 7,
        "connectionGeneration": 9,
    }
    kernel_identity = (4321, 134000000000000000)
    plan = {
        "schema": 1,
        "accountId": "12345",
        **service_meta,
        "kernelPid": kernel_identity[0],
        "kernelCreationTime100ns": str(kernel_identity[1]),
        "captureEndpointId": "capture-endpoint-fixture",
        "renderEndpointId": "render-endpoint-fixture",
        "audio": {
            "enabled": True,
            "capture_device": "Virtual Speaker [loopback]",
            "playback_device": "Virtual Microphone",
            "capture_mode": "loopback",
            "device_scope": {
                "capture_render_name": "Virtual Speaker",
                "playback_render_name": "Virtual Microphone",
            },
        },
    }
    peer_path = runtime_dir / "adapter-runtime.private.json"
    peer_path.parent.mkdir(parents=True, exist_ok=True)
    peer_path.write_text("{}", encoding="utf-8")
    plan_path = runtime_dir / "audio-plan.private.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return peer_path, service_meta, plan, kernel_identity


def test_audio_plan_reader_requires_exact_current_generation_and_kernel(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    peer_path, service_meta, plan, kernel_identity = _audio_plan_fixture(private_runtime_dir)
    reader = qqcall_service.read_qqcall_audio_plan
    valid = reader(
        peer_path,
        account_id="12345",
        service_meta=service_meta,
        kernel_identity=kernel_identity,
    )
    assert valid["audio"]["enabled"] is True

    plan_path = private_runtime_dir / "audio-plan.private.json"
    for mutation in (
        lambda value: value.update(hostGeneration=8),
        lambda value: value.update(connectionGeneration=True),
        lambda value: value.update(kernelPid=kernel_identity[0] + 1),
        lambda value: value.update(kernelCreationTime100ns=str(kernel_identity[1] + 1)),
    ):
        broken = copy.deepcopy(plan)
        mutation(broken)
        plan_path.write_text(json.dumps(broken), encoding="utf-8")
        with pytest.raises(ValueError, match="audio_plan_stale"):
            reader(
                peer_path,
                account_id="12345",
                service_meta=service_meta,
                kernel_identity=kernel_identity,
            )

    plan_path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="audio_plan_invalid"):
        reader(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )
    plan_path.write_text("{ malformed", encoding="utf-8")
    with pytest.raises(ValueError, match="audio_plan_invalid"):
        reader(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )
    plan_path.unlink()
    with pytest.raises(ValueError, match="audio_plan_unavailable"):
        reader(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )
    plan_path.write_text('{"schema":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="audio_plan_invalid"):
        reader(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )


def test_audio_plan_reader_rejects_hardlinked_private_plan(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    peer_path, service_meta, _plan, kernel_identity = _audio_plan_fixture(private_runtime_dir)
    plan_path = private_runtime_dir / "audio-plan.private.json"
    extra_link = private_runtime_dir / "plan-copy.json"
    try:
        os.link(plan_path, extra_link)
    except (OSError, NotImplementedError):
        pytest.skip("hard links are unavailable in this test environment")
    try:
        with pytest.raises(ValueError, match="runtime_path_invalid"):
            qqcall_service.read_qqcall_audio_plan(
                peer_path,
                account_id="12345",
                service_meta=service_meta,
                kernel_identity=kernel_identity,
            )
    finally:
        extra_link.unlink(missing_ok=True)


def test_audio_plan_reader_rejects_symlinked_private_plan(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    peer_path, service_meta, plan, kernel_identity = _audio_plan_fixture(private_runtime_dir)
    plan_path = private_runtime_dir / "audio-plan.private.json"
    target = private_runtime_dir / "plan-target.json"
    target.write_text(json.dumps(plan), encoding="utf-8")
    plan_path.unlink()
    try:
        plan_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this test environment")
    with pytest.raises(ValueError, match="runtime_path_invalid"):
        qqcall_service.read_qqcall_audio_plan(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )


def test_audio_plan_reader_rejects_reparse_flagged_private_plan(
    private_project_root: Path,
    private_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_path, service_meta, _plan, kernel_identity = _audio_plan_fixture(private_runtime_dir)
    plan_path = private_runtime_dir / "audio-plan.private.json"
    original_lstat = Path.lstat

    def reparse_lstat(path: Path):
        if path == plan_path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o600,
                st_file_attributes=0x400,
                st_nlink=1,
            )
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(ValueError, match="runtime_path_invalid"):
        qqcall_service.read_qqcall_audio_plan(
            peer_path,
            account_id="12345",
            service_meta=service_meta,
            kernel_identity=kernel_identity,
        )


def test_second_qqcall_enabled_switch_is_rejected(
    tmp_path: Path,
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    config = _public_config(private_runtime_dir)
    config["qqcall"]["enabled"] = False
    path = _write_canonical_config(private_project_root, config)
    adapter_config = tmp_path / "Adapter" / "config.toml"

    settings = load_qqcall_adapter_settings("napcat", adapter_config, {})

    assert settings.config.enabled is False
    assert settings.error_code is not None
    assert is_qqcall_enabled() is False
    assert not private_runtime_dir.exists()
    assert not (private_runtime_dir / "control.token.private").exists()
    assert "enabled = false" in path.read_text(encoding="utf-8")


def test_snowluma_selection_uses_canonical_enabled_runtime_despite_legacy_false(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    canonical = _write_canonical_config(
        private_project_root,
        _public_config(private_runtime_dir, adapter="snowluma"),
    )

    settings = load_qqcall_adapter_settings(
        "snowluma", private_project_root / "adapter-config.toml",
        {"enabled": False, "audio": {"enabled": False}, "service_config_file": "invalid"},
    )

    assert settings.error_code is None
    assert settings.shared_config_file == canonical.resolve()
    assert settings.config.adapter_name == "snowluma"
    assert settings.config.enabled is True
    assert settings.config.audio["enabled"] is True


def test_auto_selection_enables_both_independent_heartbeats(private_project_root, private_runtime_dir):
    _write_canonical_config(private_project_root, _public_config(private_runtime_dir, adapter="auto"))
    for adapter in ("napcat", "snowluma"):
        settings = load_qqcall_adapter_settings(adapter, "", {})
        assert settings.error_code is None
        assert settings.config.enabled is True
        assert settings.config.auto_select_adapter is True
        assert settings.config.adapter_name == adapter
        assert settings.platform_manifest_file.name == f"platform-{adapter}.private.json"


@pytest.mark.parametrize("failure", [None, "adapter", "pid", "filetime", "missing", "unknown"])
def test_only_selected_adapter_process_can_claim_lease(private_runtime_dir, monkeypatch, failure):
    from qqcall_client.qqcall import QQCallError

    path = private_runtime_dir / "adapter-runtime.private.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(qqcall_service, "_windows_process_creation_time_100ns", lambda _pid: 123456)
    binding = {"adapter": "napcat", "pid": os.getpid(), "creationTime100ns": "123456"}
    if failure == "adapter":
        binding["adapter"] = "snowluma"
    elif failure == "pid":
        binding["pid"] += 1
    elif failure == "filetime":
        binding["creationTime100ns"] = "654321"
    elif failure == "unknown":
        binding["extra"] = True
    snapshot = {
        "schema": 1, "accountId": "12345", "serviceInstance": "12345678-1234-4123-8123-123456789abc",
        "hostGeneration": 1, "connectionGeneration": 2, "kernelPid": 123,
        "kernelCreationTime100ns": "999", "peerMap": {}, "adapterBinding": binding,
    }
    if failure == "missing":
        snapshot.pop("adapterBinding")
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    sent = []
    async def transport(route, payload, token):
        sent.append(route)
        return {"accepted": True}
    config = QQCallConfig(account_id="12345", adapter_name="napcat", auto_select_adapter=True,
                         peer_map_file=str(path))
    runtime = QQCallRuntime(config, "qq", lambda *_: None, lambda *_: None,
                            lambda: {}, lambda _: False, transport=transport)
    runtime._token = "test-token"
    async def check():
        if failure:
            with pytest.raises(QQCallError, match="adapter_not_selected"):
                await runtime._post("/v1/adapter/poll", {})
            assert sent == []
        else:
            assert await runtime._post("/v1/adapter/poll", {}) == {"accepted": True}
            assert runtime._read_peer_map(snapshot) == {}
        # A replaced adapter may still release its own old lease.
        assert await runtime._post("/v1/adapter/release", {}) == {"accepted": True}
    asyncio.run(check())


def test_enable_helper_fresh_reads_root_config_without_private_writes(
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    value = _public_config(private_runtime_dir)
    path = _write_canonical_config(private_project_root, value)
    assert is_qqcall_enabled() is True
    assert not private_runtime_dir.exists()

    value["plugin"]["enabled"] = False
    path.write_text(toml.dumps(value), encoding="utf-8")
    assert is_qqcall_enabled() is False
    assert not private_runtime_dir.exists()


def test_root_disabled_ignores_legacy_true_and_creates_no_runtime_or_token(
    tmp_path: Path,
    private_project_root: Path,
    private_runtime_dir: Path,
) -> None:
    canonical = _write_canonical_config(private_project_root, _public_config(private_runtime_dir, enabled=False))
    adapter_config = tmp_path / "Adapter" / "config.toml"
    _adapter_config(adapter_config, "C:/untrusted/other-config.json")

    settings = load_qqcall_adapter_settings(
        "napcat", adapter_config,
        {"enabled": True, "audio": {"enabled": True}, "account_id": "998877", "token_file": "secret"},
    )

    assert canonical == canonical_qqcall_config_path()
    assert settings.config.enabled is False
    assert settings.config.audio["enabled"] is False
    assert is_qqcall_enabled() is False
    assert not private_runtime_dir.exists()
    assert not (private_runtime_dir / "control.token.private").exists()


def test_missing_or_invalid_canonical_config_fails_closed_without_legacy_fallback(
    private_project_root: Path,
    tmp_path: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    legacy = {"enabled": True, "account_id": "778899", "token_file": "legacy-token"}

    missing = load_qqcall_adapter_settings("napcat", adapter_config, legacy)
    assert missing.config.enabled is False
    assert missing.error_code is not None
    assert not missing.config.token_file
    assert is_qqcall_enabled() is False

    invalid_path = canonical_qqcall_config_path()
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_text("schema = 999\n", encoding="utf-8")
    invalid = load_qqcall_adapter_settings("napcat", adapter_config, legacy)
    assert invalid.config.enabled is False
    assert invalid.error_code is not None
    assert not invalid.config.token_file
    assert is_qqcall_enabled() is False


def test_disabled_shared_config_preserves_existing_private_files(
    tmp_path: Path,
    private_runtime_dir: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    runtime_dir = private_runtime_dir
    runtime_dir.mkdir(parents=True)
    token = runtime_dir / "control.token.private"
    manifest = runtime_dir / "adapter-runtime.private.json"
    heartbeat = runtime_dir / "platform-napcat.private.json"
    status = runtime_dir / "status.json"
    token.write_text("existing-token-material\n", encoding="ascii")
    manifest.write_text('{"preserve":"manifest"}', encoding="utf-8")
    heartbeat.write_text('{"preserve":"heartbeat"}', encoding="utf-8")
    status.write_text('{"preserve":"status"}', encoding="utf-8")
    public_path = _write_canonical_config(
        canonical_qqcall_config_path().parent,
        _public_config(runtime_dir, enabled=False),
    )
    _adapter_config(adapter_config, "../ignored-decoy.json")

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.error_code is None
    assert settings.config.enabled is False
    assert settings.platform_manifest_file is None
    assert not settings.config.platform_manifest_file
    assert token.read_text(encoding="ascii") == "existing-token-material\n"
    assert manifest.read_text(encoding="utf-8") == '{"preserve":"manifest"}'
    assert heartbeat.read_text(encoding="utf-8") == '{"preserve":"heartbeat"}'
    assert status.read_text(encoding="utf-8") == '{"preserve":"status"}'


def test_invalid_or_wrong_adapter_shared_config_fails_closed(
    tmp_path: Path,
    private_runtime_dir: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    runtime_dir = private_runtime_dir
    _write_canonical_config(canonical_qqcall_config_path().parent, _public_config(runtime_dir, adapter="snowluma"))
    _adapter_config(adapter_config, "../ignored-decoy.json")

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.config.enabled is False
    assert settings.error_code is not None
    assert not (runtime_dir / "control.token.private").exists()


@pytest.mark.parametrize("retired_field", ("config_version", "allowed_peer_ids", "allowed_users"))
def test_schema5_rejects_retired_public_fields(
    retired_field: str,
    tmp_path: Path,
    private_runtime_dir: Path,
) -> None:
    value = _public_config(private_runtime_dir)
    if retired_field == "config_version":
        value["plugin"][retired_field] = "obsolete"
    elif retired_field == "allowed_peer_ids":
        value["qqcall"][retired_field] = []
    else:
        value["qqcall"]["policy"][retired_field] = []
    _write_canonical_config(canonical_qqcall_config_path().parent, value)
    adapter_config = tmp_path / "Adapter" / "config.toml"

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.config.enabled is False
    assert settings.error_code is not None
    assert not (private_runtime_dir / "control.token.private").exists()


def test_runtime_outside_fixed_private_project_is_rejected_before_creation(
    tmp_path: Path,
    private_project_root: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    outside_runtime = tmp_path / "outside" / "runtime"
    _write_canonical_config(canonical_qqcall_config_path().parent, _public_config(outside_runtime))
    _adapter_config(adapter_config, "../ignored-decoy.json")

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.config.enabled is False
    assert settings.error_code == "runtime_must_be_private_project_path"
    assert not outside_runtime.exists()
    assert not (private_project_root / ".runtime").exists()


def test_runtime_alias_normalization_is_rejected_before_token_or_heartbeat_write(
    tmp_path: Path,
    private_project_root: Path,
) -> None:
    runtime_root = private_project_root / ".runtime"
    real_runtime = runtime_root / "real"
    real_runtime.mkdir(parents=True)
    alias_runtime = runtime_root / "alias"
    public_path = _write_canonical_config(private_project_root, _public_config(alias_runtime))

    with pytest.raises(ValueError, match="runtime_path_invalid"):
        qqcall_service._configured_runtime_directory(public_path, str(real_runtime))

    assert not (real_runtime / "control.token.private").exists()
    assert not (real_runtime / "platform-napcat.private.json").exists()


def test_runtime_windows_reparse_ancestor_is_rejected_before_private_writes(
    private_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_runtime_dir.mkdir(parents=True)
    original_lstat = Path.lstat

    def lstat_with_reparse_flag(path: Path):
        metadata = original_lstat(path)
        if path == private_runtime_dir:
            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(Path, "lstat", lstat_with_reparse_flag)

    with pytest.raises(ValueError, match="runtime_path_invalid"):
        qqcall_service._validate_runtime_directory(private_runtime_dir)

    assert not (private_runtime_dir / "control.token.private").exists()
    assert not (private_runtime_dir / "platform-napcat.private.json").exists()


def test_existing_private_control_token_is_preserved(tmp_path: Path, private_runtime_dir: Path) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    private_runtime_dir.mkdir(parents=True)
    token_path = private_runtime_dir / "control.token.private"
    original_token = "t" * 43 + "\n"
    token_path.write_text(original_token, encoding="ascii")
    _write_canonical_config(canonical_qqcall_config_path().parent, _public_config(private_runtime_dir))
    _adapter_config(adapter_config, "../ignored-decoy.json")

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.error_code is None
    assert token_path.read_text(encoding="ascii") == original_token


def test_partial_private_control_token_fails_closed_without_replacement(
    tmp_path: Path,
    private_runtime_dir: Path,
) -> None:
    adapter_config = tmp_path / "Adapter" / "config.toml"
    private_runtime_dir.mkdir(parents=True)
    token_path = private_runtime_dir / "control.token.private"
    token_path.write_text("partial\n", encoding="ascii")
    _write_canonical_config(canonical_qqcall_config_path().parent, _public_config(private_runtime_dir))
    _adapter_config(adapter_config, "../ignored-decoy.json")

    settings = load_qqcall_adapter_settings("napcat", adapter_config, QQCallConfig())

    assert settings.config.enabled is False
    assert settings.error_code == "token_file_invalid"
    assert token_path.read_text(encoding="ascii") == "partial\n"


def test_platform_heartbeat_keeps_exact_filetime_and_only_publishes_safe_identity(
    tmp_path: Path,
    private_runtime_dir: Path,
) -> None:
    filetime = (1 << 60) + 123_456_789
    manifest = private_runtime_dir / "platform-napcat.private.json"
    heartbeat = QQCallPlatformBindingHeartbeat(
        adapter="napcat",
        manifest_file=manifest,
        account_id="12345",
        pid=4321,
        creation_time_provider=lambda _pid: filetime,
    )
    writes = []
    original_write = heartbeat._write

    def capture_write(**kwargs):
        writes.append(dict(kwargs))
        original_write(**kwargs)

    heartbeat._write = capture_write  # type: ignore[method-assign]

    async def scenario() -> None:
        await heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("private-socket-identity", 7, "12345"),
            "connection_generation": "ordinary-connection-7",
        })
        first = json.loads(manifest.read_text(encoding="utf-8"))
        assert first["connected"] is True
        assert first["accountId"] == "12345"
        assert first["adapterCreationTime100ns"] == str(filetime)
        assert type(first["adapterCreationTime100ns"]) is str
        assert first["connectionGeneration"] == "ordinary-connection-7"
        serialized = manifest.read_text(encoding="utf-8")
        assert "private-socket-identity" not in serialized
        assert "token" not in serialized.casefold()

        writes.clear()
        await heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("replacement-private-socket", 8, "12345"),
            "connection_generation": "ordinary-connection-8",
        })
        assert writes[0] == {"connected": False, "account_id": "", "generation": "ordinary-connection-8"}
        assert writes[1]["connected"] is True

        await heartbeat.publish_state({
            "connected": True,
            "account_id": "99999",
            "identity": ("unselected-account", 9, "99999"),
            "connection_generation": 9,
        })
        offline = json.loads(manifest.read_text(encoding="utf-8"))
        assert offline["connected"] is False
        assert offline["accountId"] == ""
        await heartbeat.stop()

    asyncio.run(scenario())
    assert heartbeat._writer_closed is True
    assert heartbeat._writer is None


def test_platform_heartbeat_serializes_concurrent_identity_replacements(
    private_runtime_dir: Path,
) -> None:
    heartbeat = QQCallPlatformBindingHeartbeat(
        adapter="napcat",
        manifest_file=private_runtime_dir / "platform-napcat.private.json",
        account_id="12345",
        pid=4321,
        creation_time_provider=lambda _pid: 134000000000000000,
    )
    writes: list[dict[str, object]] = []

    def capture_write(**kwargs):
        writes.append(dict(kwargs))

    heartbeat._write = capture_write  # type: ignore[method-assign]

    async def scenario() -> None:
        await heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("socket-a", 1),
            "connection_generation": 1,
        })
        replacement = asyncio.create_task(heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("socket-b", 2),
            "connection_generation": 2,
        }))
        next_replacement = asyncio.create_task(heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("socket-c", 3),
            "connection_generation": 3,
        }))
        await asyncio.gather(replacement, next_replacement)
        assert writes == [
            {"connected": True, "account_id": "12345", "generation": 1},
            {"connected": False, "account_id": "", "generation": 2},
            {"connected": True, "account_id": "12345", "generation": 2},
            {"connected": False, "account_id": "", "generation": 3},
            {"connected": True, "account_id": "12345", "generation": 3},
        ]
        await heartbeat.stop()

    asyncio.run(scenario())


def test_platform_heartbeat_cancel_and_stop_drain_blocked_write_without_late_online(
    private_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_runtime_dir.mkdir(parents=True, exist_ok=True)
    manifest = private_runtime_dir / "platform-napcat.private.json"
    heartbeat = QQCallPlatformBindingHeartbeat(
        adapter="napcat",
        manifest_file=manifest,
        account_id="12345",
        pid=4321,
        creation_time_provider=lambda _pid: 134000000000000000,
    )
    entered = asyncio.Event()
    release = threading.Event()
    payloads: list[dict[str, object]] = []
    loop: asyncio.AbstractEventLoop | None = None

    def blocking_atomic_write(path: Path, payload: dict[str, object]) -> None:
        payloads.append(dict(payload))
        if payload.get("connected") is True:
            assert loop is not None
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3.0):
                raise TimeoutError("test writer gate was not released")
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    # Replace only the final filesystem primitive. The heartbeat's real
    # validation and serialization still run on its owned writer thread.
    private_files = qqcall_service._fixed_private_files()
    monkeypatch.setattr(private_files, "atomic_private_json", blocking_atomic_write)

    async def scenario() -> None:
        nonlocal loop
        loop = asyncio.get_running_loop()
        first = asyncio.create_task(heartbeat.publish_state({
            "connected": True,
            "account_id": "12345",
            "identity": ("socket-a", 1),
            "connection_generation": 1,
        }))
        timer = threading.Timer(0.4, release.set)
        timer.start()
        stop_task: asyncio.Task[None] | None = None
        late_publish: asyncio.Task[None] | None = None
        ticker_task: asyncio.Task[None] | None = None
        ticked = asyncio.Event()
        tick_count = 0

        async def ticker() -> None:
            nonlocal tick_count
            while not release.is_set():
                tick_count += 1
                ticked.set()
                await asyncio.sleep(0.005)

        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            ticker_task = asyncio.create_task(ticker())
            await asyncio.wait_for(ticked.wait(), timeout=0.25)
            assert not release.is_set(), "the event loop must tick before the worker is released"
            assert tick_count >= 1

            stop_task = asyncio.create_task(heartbeat.stop())
            await asyncio.sleep(0)
            late_publish = asyncio.create_task(heartbeat.publish_state({
                "connected": True,
                "account_id": "12345",
                "identity": ("socket-b", 2),
                "connection_generation": 2,
            }))
            await asyncio.sleep(0)
            assert not stop_task.done()

            first.cancel()
            await asyncio.sleep(0)
            first.cancel()
            assert stop_task is not None
            stop_task.cancel()
            await asyncio.sleep(0)
            stop_task.cancel()
            await asyncio.sleep(0.02)
            assert not first.done(), "cancellation must wait for the submitted write to drain"
            assert not stop_task.done(), "stop must wait for the blocked write and final offline commit"
        finally:
            release.set()
            timer.cancel()
            if ticker_task is not None:
                ticker_task.cancel()
                await asyncio.gather(ticker_task, return_exceptions=True)
            await asyncio.gather(
                first,
                *(task for task in (late_publish, stop_task) if task is not None),
                return_exceptions=True,
            )
            await heartbeat.stop()

        with pytest.raises(asyncio.CancelledError):
            await first
        assert stop_task is not None and stop_task.cancelled()
        await heartbeat.stop()
        if late_publish is not None:
            await late_publish

        final = json.loads(manifest.read_text(encoding="utf-8"))
        assert final["connected"] is False
        assert final["accountId"] == ""
        assert [payload["connected"] for payload in payloads] == [True, False]
        assert payloads[-1]["adapterCreationTime100ns"] == "134000000000000000"

    asyncio.run(scenario())


def test_platform_heartbeat_logs_only_fixed_codes_for_provider_errors(
    tmp_path: Path,
    private_project_root: Path,
) -> None:
    for index, secret in enumerate(("u_" + "a" * 22, "99887766554433221100")):
        runtime_dir = private_project_root / ".runtime" / f"napcat-{index}"
        manifest = runtime_dir / "platform-napcat.private.json"
        heartbeat = QQCallPlatformBindingHeartbeat(
            adapter="napcat",
            manifest_file=manifest,
            account_id="12345",
            pid=4321,
            creation_time_provider=lambda _pid: 134000000000000000,
        )

        class ProviderError(RuntimeError):
            code = secret

        async def broken_provider():
            await heartbeat.stop()
            raise ProviderError(secret)

        with patch.object(qqcall_service._LOGGER, "warning") as warning:
            asyncio.run(heartbeat.run(broken_provider))

        assert heartbeat._writer_closed is True
        assert heartbeat._writer is None
        logged_args = warning.call_args.args
        assert logged_args[-1] == "heartbeat_failed"
        assert secret not in repr(logged_args)


def test_platform_heartbeat_rejects_paths_outside_private_runtime_before_writing(
    tmp_path: Path,
    private_project_root: Path,
) -> None:
    outside_manifest = tmp_path / "outside" / "platform-napcat.private.json"

    with pytest.raises(ValueError, match="runtime_must_be_private_project_path"):
        QQCallPlatformBindingHeartbeat(
            adapter="napcat",
            manifest_file=outside_manifest,
            account_id="12345",
            creation_time_provider=lambda _pid: 134000000000000000,
        )

    assert not outside_manifest.parent.exists()
