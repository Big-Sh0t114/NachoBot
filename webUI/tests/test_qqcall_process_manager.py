from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import process_manager


def test_qqcall_job_is_the_only_service_job_that_allows_breakaway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    calls: list[tuple[int, dict[str, object]]] = []

    class FakeJobFacade:
        def create_assign_resume(self, pid: int, **kwargs):
            calls.append((pid, kwargs))
            return object()

    monkeypatch.setattr(manager, "_get_windows_job_facade", lambda: FakeJobFacade())

    manager._create_service_windows_job("qqcall", 101)
    manager._create_service_windows_job("snowluma_runtime", 202)

    assert calls == [(101, {"allow_breakaway": True}), (202, {})]


def test_windows_job_limits_keep_kill_on_close_and_breakaway_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_flags: list[int] = []

    class FakeFunction:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    def capture_limits(_job, _info_class, information, _size):
        pointer = process_manager.ctypes.cast(
            information,
            process_manager.ctypes.POINTER(process_manager._WindowsJobExtendedLimitInformation),
        )
        captured_flags.append(int(pointer.contents.BasicLimitInformation.LimitFlags))
        return True

    handles = iter((101, 102, 103, 104))
    kernel32 = SimpleNamespace(
        CreateJobObjectW=FakeFunction(lambda *_args: 100),
        SetInformationJobObject=FakeFunction(capture_limits),
        OpenProcess=FakeFunction(lambda *_args: next(handles)),
        AssignProcessToJobObject=FakeFunction(lambda *_args: True),
        CloseHandle=FakeFunction(lambda *_args: True),
        TerminateProcess=FakeFunction(lambda *_args: True),
        TerminateJobObject=FakeFunction(lambda *_args: True),
        QueryInformationJobObject=FakeFunction(lambda *_args: True),
    )
    ntdll = SimpleNamespace(NtResumeProcess=FakeFunction(lambda *_args: 0))

    monkeypatch.setattr(process_manager, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_manager.ctypes,
        "WinDLL",
        lambda name, **_kwargs: kernel32 if name == "kernel32" else ntdll,
        raising=False,
    )
    facade = process_manager._WindowsJobFacade()

    ordinary_job = facade.create_assign_resume(1)
    qqcall_job = facade.create_assign_resume(2, allow_breakaway=True)
    facade.close(ordinary_job)
    facade.close(qqcall_job)

    assert captured_flags == [
        process_manager._WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        process_manager._WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | process_manager._WINDOWS_JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    ]


def _fixed_qqcall_config_module():
    path = WEBUI_DIR.parent / "NachoBot-QQCall" / "service" / "config.py"
    spec = importlib.util.spec_from_file_location("_qqcall_process_manager_test_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_qqcall_public_config(
    root: Path,
    *,
    enabled: bool,
    adapter: str = "napcat",
    audio_overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    config_module = _fixed_qqcall_config_module()
    public_config = copy.deepcopy(config_module.DEFAULT_CONFIG)
    plugin_config = public_config["plugin"]
    qqcall_config = public_config["qqcall"]
    plugin_config["enabled"] = enabled
    qqcall_config["adapter"] = adapter
    if enabled:
        qqcall_config["account_id"] = "12345"
        qqcall_config["onebot"]["url"] = "ws://127.0.0.1:3001/ws"
        qqcall_config["paths"]["qq_exe"] = r"C:\QQ\QQ.exe"
    qqcall_config["runtime_dir"] = str(
        (root / "NachoBot-QQCall" / ".runtime" / "service").resolve()
    )

    if audio_overrides is None:
        qqcall_config.pop("audio", None)
    else:
        qqcall_config["audio"] = copy.deepcopy(audio_overrides)
    native = qqcall_config.get("native")
    if isinstance(native, dict) and "guard_config_file" in native:
        guard_path = root / "NachoBot-QQCall" / ".runtime" / "test-guard.json"
        guard_path.parent.mkdir(parents=True, exist_ok=True)
        guard_path.write_text('{"schema":1}\n', encoding="utf-8")
        native["guard_config_file"] = str(guard_path.resolve())

    public_path = root / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(tomlkit.dumps(public_config), encoding="utf-8")
    assert tomlkit.parse(public_path.read_text(encoding="utf-8"))["plugin"]["enabled"] is enabled
    return public_config


def test_disabled_canonical_config_ignores_legacy_json(
    tmp_path: Path,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=False, adapter="napcat")
    legacy_json = tmp_path / "NachoBot-QQCall" / "config.json"
    legacy_json.parent.mkdir(parents=True, exist_ok=True)
    legacy_json.write_text('{"enabled":true,"adapter":"napcat"}', encoding="utf-8")

    command, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS["napcat_adapter"])

    assert command == ["uv", "run", "python", "main.py"]


def test_enabled_canonical_adapter_adds_extra_even_when_webui_selects_other_adapter(
    tmp_path: Path,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True, adapter="napcat")
    (tmp_path / "NachoBot").mkdir(parents=True, exist_ok=True)
    (tmp_path / "NachoBot" / ".env").write_text("qq_adapter=snowluma\n", encoding="utf-8")
    process_manager._register_services(tmp_path)

    matching, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS["napcat_adapter"])
    mismatching, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS["snowluma_adapter"])

    assert matching == ["uv", "run", "--extra", "qqcall-audio", "python", "main.py"]
    assert mismatching == ["uv", "run", "python", "main.py"]


def test_auto_adapter_adds_audio_dependency_for_both_runtimes(tmp_path):
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True, adapter="auto")
    process_manager._register_services(tmp_path)
    for service_id in ("napcat_adapter", "snowluma_adapter"):
        command, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS[service_id])
        assert command == ["uv", "run", "--extra", "qqcall-audio", "python", "main.py"]


@pytest.mark.parametrize("adapter_dir", ("NachoBot-Napcat-Adapter", "NachoBot-SnowLuma-Adapter"))
def test_optional_qqcall_extra_installs_only_local_pcm_package(adapter_dir: str) -> None:
    root = WEBUI_DIR.parent
    manifest_path = root / adapter_dir / "pyproject.toml"
    manifest = tomlkit.parse(manifest_path.read_text(encoding="utf-8"))
    assert manifest["project"]["optional-dependencies"]["qqcall-audio"] == ["nachobot-qqcall-pcm"]
    source = manifest["tool"]["uv"]["sources"]["nachobot-qqcall-pcm"]
    assert source["editable"] is True
    assert (manifest_path.parent / source["path"]).resolve() == (root / "NachoBot-QQCall").resolve()
    assert not set(manifest["project"]["dependencies"]) & {
        "PyAudioWPatch>=0.2.12.8; sys_platform == 'win32'",
        "audioop-lts; python_version >= '3.13'",
    }
    extra = manifest["project"]["optional-dependencies"]["qqcall-audio"]
    assert not any(name in str(extra).lower() for name in ("numpy", "onnx", "sherpa", "universalvc"))


@pytest.mark.parametrize(
    "audio_overrides",
    (
        {},
        {"capture_device": "Verified loopback override"},
        {"playback_device": "Verified virtual playback override"},
        {"capture_device": "", "playback_device": ""},
    ),
)
def test_enabled_canonical_config_accepts_partial_audio_overrides_for_webui_command(
    tmp_path: Path,
    audio_overrides: dict[str, object],
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True, audio_overrides=audio_overrides)
    process_manager._register_services(tmp_path)

    command, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS["napcat_adapter"])

    assert command == ["uv", "run", "--extra", "qqcall-audio", "python", "main.py"]


@pytest.mark.parametrize("central_state", ("missing", "invalid"))
@pytest.mark.parametrize("service_id", ("napcat_adapter", "snowluma_adapter"))
def test_invalid_or_missing_canonical_config_keeps_ordinary_adapter_without_audio_extra(
    tmp_path: Path,
    central_state: str,
    service_id: str,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    public_path = tmp_path / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    if central_state == "invalid":
        public_path.parent.mkdir(parents=True)
        public_path.write_text("not = [valid TOML", encoding="utf-8")
    legacy_json = tmp_path / "NachoBot-QQCall" / "config.json"
    legacy_json.parent.mkdir(parents=True, exist_ok=True)
    legacy_json.write_text('{"enabled":true,"adapter":"napcat"}', encoding="utf-8")

    command, _cwd, _env = manager._resolve_cmd(process_manager.SERVICE_DEFS[service_id])

    assert command == ["uv", "run", "python", "main.py"]


@pytest.mark.parametrize("start_mode", ("service", "group"))
def test_start_preflight_rereads_disabled_flag_and_never_spawns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_mode: str,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True)
    process_manager._register_services(tmp_path)
    # Simulate a switch changed after the service registry and manager were cached.
    _write_qqcall_public_config(tmp_path, enabled=False)
    created: list[tuple[object, ...]] = []
    messages: list[str] = []

    async def fake_create_subprocess_exec(*args, **_kwargs):
        created.append(args)
        raise AssertionError("disabled QQCall must be rejected before spawning")

    async def collect_broadcast(_service_id: str, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(manager, "_broadcast", collect_broadcast)

    async def scenario() -> None:
        if start_mode == "group":
            await manager.start_group("qqcall")
        else:
            await manager.start_service("qqcall")

    asyncio.run(scenario())

    assert created == []
    assert manager.get_service_status("qqcall")["status"] == "error"
    assert any("QQCall 已禁用" in message for message in messages)
    assert any("NachoBot/plugins/qqcall_plugin/config.toml" in message for message in messages)


@pytest.mark.parametrize("central_state", ("missing", "invalid"))
@pytest.mark.parametrize("start_mode", ("service", "group"))
def test_start_preflight_rejects_missing_or_invalid_config_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    central_state: str,
    start_mode: str,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    public_path = tmp_path / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    if central_state == "invalid":
        public_path.parent.mkdir(parents=True)
        public_path.write_text("not = [valid TOML", encoding="utf-8")
    legacy_json = tmp_path / "NachoBot-QQCall" / "config.json"
    legacy_json.parent.mkdir(parents=True, exist_ok=True)
    legacy_json.write_text('{"enabled":true,"adapter":"napcat"}', encoding="utf-8")
    created: list[tuple[object, ...]] = []
    messages: list[str] = []

    async def fake_create_subprocess_exec(*args, **_kwargs):
        created.append(args)
        raise AssertionError("invalid QQCall config must be rejected before spawning")

    async def collect_broadcast(_service_id: str, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(manager, "_broadcast", collect_broadcast)

    async def scenario() -> None:
        if start_mode == "group":
            await manager.start_group("qqcall")
        else:
            await manager.start_service("qqcall")

    asyncio.run(scenario())

    assert created == []
    assert manager.get_service_status("qqcall")["status"] == "error"
    assert any("公共配置无效或不可用" in message for message in messages)
    assert any("NachoBot/plugins/qqcall_plugin/config.toml" in message for message in messages)


@pytest.mark.parametrize("start_mode", ("service", "group"))
@pytest.mark.parametrize(
    ("missing_input", "expected_code", "expected_hint"),
    (
        (
            "onebot_url",
            "enabled_identity_and_onebot_required",
            "OneBot 地址或身份信息",
        ),
        ("qq_exe", "enabled_qq_exe_required", "QQ 程序路径"),
    ),
)
def test_start_preflight_broadcasts_limited_validator_hints_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_mode: str,
    missing_input: str,
    expected_code: str,
    expected_hint: str,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True)
    public_path = tmp_path / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    public_config = tomlkit.parse(public_path.read_text(encoding="utf-8"))
    if missing_input == "onebot_url":
        public_config["qqcall"]["onebot"]["url"] = ""
    else:
        public_config["qqcall"]["paths"]["qq_exe"] = ""
    public_path.write_text(tomlkit.dumps(public_config), encoding="utf-8")
    process_manager._register_services(tmp_path)

    created: list[tuple[object, ...]] = []
    messages: list[str] = []

    async def fake_create_subprocess_exec(*args, **_kwargs):
        created.append(args)
        raise AssertionError("invalid QQCall config must be rejected before spawning")

    async def collect_broadcast(_service_id: str, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(
        process_manager.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(manager, "_broadcast", collect_broadcast)

    async def scenario() -> None:
        if start_mode == "group":
            manager.request_start_group("qqcall")
            task = manager._operation_tasks["group:qqcall"]
        else:
            manager.request_start_service("qqcall")
            task = manager._operation_tasks["service:qqcall"]
        await task

    asyncio.run(scenario())

    displayed = "\n".join(messages)
    assert created == []
    assert manager.get_service_status("qqcall")["status"] == "error"
    assert expected_code in displayed
    assert expected_hint in displayed
    assert "NachoBot/plugins/qqcall_plugin/config.toml" in displayed
    assert "http://" not in displayed
    if start_mode == "group":
        assert any("启动失败，回滚本次已启动服务" in message for message in messages)
    else:
        assert manager.states["qqcall"].start_reservation is False


@pytest.mark.parametrize("start_mode", ("service", "group"))
def test_start_preflight_keeps_unknown_validator_error_generic_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_mode: str,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True)
    process_manager._register_services(tmp_path)

    class _FakeConfigError(ValueError):
        def __init__(self) -> None:
            self.code = "secret-token-value"
            super().__init__("raw-secret-and-input-value")

    class _FakeLoader:
        def exec_module(self, module) -> None:
            module.ConfigError = _FakeConfigError

            def fail(_path: Path) -> None:
                raise _FakeConfigError()

            module.load_config = fail

    class _FakeSpec:
        name = "_fake_qqcall_validator"
        loader = _FakeLoader()

    monkeypatch.setattr(
        importlib.util,
        "spec_from_file_location",
        lambda *_args, **_kwargs: _FakeSpec(),
    )
    created: list[tuple[object, ...]] = []
    messages: list[str] = []

    async def fake_create_subprocess_exec(*args, **_kwargs):
        created.append(args)
        raise AssertionError("invalid QQCall config must be rejected before spawning")

    async def collect_broadcast(_service_id: str, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(
        process_manager.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(manager, "_broadcast", collect_broadcast)

    async def scenario() -> None:
        if start_mode == "group":
            manager.request_start_group("qqcall")
            task = manager._operation_tasks["group:qqcall"]
        else:
            manager.request_start_service("qqcall")
            task = manager._operation_tasks["service:qqcall"]
        await task

    asyncio.run(scenario())

    displayed = "\n".join(messages)
    assert created == []
    assert manager.get_service_status("qqcall")["status"] == "error"
    assert any("公共配置无效或不可用" in message for message in messages)
    assert "secret-token-value" not in displayed
    assert "raw-secret-and-input-value" not in displayed
    if start_mode == "group":
        assert any("启动失败，回滚本次已启动服务" in message for message in messages)
    else:
        assert manager.states["qqcall"].start_reservation is False


def test_config_error_code_gets_limited_safe_hint(
    tmp_path: Path,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    _write_qqcall_public_config(tmp_path, enabled=True)
    public_path = tmp_path / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    public_config = tomlkit.parse(public_path.read_text(encoding="utf-8"))
    public_config["qqcall"]["onebot"]["url"] = ""
    public_path.write_text(tomlkit.dumps(public_config), encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        manager._load_qqcall_public_config()

    message = str(caught.value)
    assert "enabled_identity_and_onebot_required" in message
    assert "OneBot 地址或身份信息" in message
    assert "NachoBot/plugins/qqcall_plugin/config.toml" in message
    assert "http://" not in message


def test_unknown_validator_error_never_echoes_code_or_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)

    class _FakeConfigError(ValueError):
        def __init__(self) -> None:
            self.code = "secret-token-value"
            super().__init__("raw-secret-and-input-value")

    class _FakeLoader:
        def exec_module(self, module) -> None:
            module.ConfigError = _FakeConfigError

            def fail(_path: Path) -> None:
                raise _FakeConfigError()

            module.load_config = fail

    class _FakeSpec:
        name = "_fake_qqcall_validator"
        loader = _FakeLoader()

    monkeypatch.setattr(
        importlib.util,
        "spec_from_file_location",
        lambda *_args, **_kwargs: _FakeSpec(),
    )

    with pytest.raises(ValueError) as caught:
        manager._load_qqcall_public_config()

    message = str(caught.value)
    assert message == process_manager._QQCALL_PUBLIC_CONFIG_ERROR
    assert "secret-token-value" not in message
    assert "raw-secret-and-input-value" not in message


def test_qqcall_service_definition_is_standalone_and_has_no_port_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_manager, "ROOT_DIR", tmp_path)
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)

    definition = process_manager.SERVICE_DEFS["qqcall"]
    assert definition.group_id == "qqcall"
    assert definition.cwd == "NachoBot-QQCall"
    assert definition.order == 3
    assert definition.wait_port is False
    assert definition.port is None
    assert definition.cmd == [
        "uv", "run", "--project", "../NachoBot", "--locked", "python",
        "service/manager.py", "run", "--config",
        "../NachoBot/plugins/qqcall_plugin/config.toml",
    ]
    assert process_manager.GROUP_DEFS["qqcall"].name == "QQ语音通话"
    assert process_manager.GROUP_DEFS["qqcall"].services == ["qqcall"]
    command, cwd, _env = manager._resolve_cmd(definition)
    assert command == definition.cmd
    assert cwd == "NachoBot-QQCall"


def test_qqcall_group_start_is_mocked_and_scoped_to_control_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    started: list[str] = []

    async def fake_start(service_id: str, *, _prepared: bool = False) -> None:
        started.append(service_id)
        manager.states[service_id] = process_manager.ServiceState(
            status=process_manager.ServiceStatus.RUNNING,
            pid=321,
        )

    async def no_sleep(_duration: float) -> None:
        return None

    monkeypatch.setattr(manager, "start_service", fake_start)
    monkeypatch.setattr(manager, "_ensure_required_components", lambda _services: None)
    monkeypatch.setattr(process_manager.asyncio, "sleep", no_sleep)

    async def scenario() -> None:
        await manager.start_group("qqcall")
        assert started == ["qqcall"]
        assert manager.get_service_status("qqcall")["status"] == "running"
        assert "nachobot" not in manager.states
        assert "napcat_adapter" not in manager.states
        assert "snowluma_adapter" not in manager.states

    asyncio.run(scenario())


def test_stop_requests_supervisor_before_generic_process_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    events: list[str] = []

    class FakeChild:
        def __init__(self, label: str):
            self.label = label

        returncode: int | None = 0

        async def wait(self) -> int:
            events.append(self.label)
            return 0

    request_child = FakeChild("cli-waited")

    async def fake_create_subprocess_exec(*args, **kwargs):
        events.append("graceful-cli")
        assert args == (
            "uv", "run", "--project", "../NachoBot", "--locked", "python",
            "service/manager.py", "stop", "--config",
            "../NachoBot/plugins/qqcall_plugin/config.toml",
        )
        assert Path(kwargs["cwd"]) == tmp_path / "NachoBot-QQCall"
        assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
        return request_child

    async def fake_terminate(state: process_manager.ServiceState) -> None:
        events.append("generic-cleanup")
        assert "graceful-cli" in events
        assert "supervisor-waited" in events
        state.process = None
        state.process_group_id = None

    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(manager, "_terminate_state_process", fake_terminate)
    manager.states["qqcall"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING,
        process=FakeChild("supervisor-waited"),
        pid=321,
        process_group_id=321,
    )

    async def scenario() -> None:
        await manager.stop_service("qqcall")
        assert events == ["graceful-cli", "cli-waited", "supervisor-waited", "generic-cleanup"]
        assert manager.get_service_status("qqcall")["status"] == "stopped"

    asyncio.run(scenario())


def test_qqcall_stop_budget_covers_slow_confirmed_supervisor_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    timeouts: list[float] = []
    events: list[str] = []
    clock = [100.0]

    class FakeChild:
        returncode: int | None = 0

        async def wait(self) -> int:
            events.append("waited")
            return 0

    request_child = FakeChild()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return request_child

    async def fake_wait_for(awaitable, *, timeout: float):
        timeouts.append(timeout)
        result = await awaitable
        if len(timeouts) == 2:
            # The owned supervisor takes the full adapter detach/reap budget.
            clock[0] += 14.0
        return result

    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(process_manager.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(manager, "_qqcall_stop_clock", lambda: clock[0])
    state = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING,
        process=FakeChild(),
        pid=321,
    )

    stopped = asyncio.run(manager._request_qqcall_supervisor_stop(state))

    assert stopped is True
    assert timeouts[0] == process_manager._QQCALL_STOP_REQUEST_TIMEOUT
    assert timeouts[1] == process_manager._QQCALL_GRACEFUL_STOP_TIMEOUT
    assert clock[0] - 100.0 == 14.0
    assert len(events) == 2


def test_qqcall_stop_timeout_falls_back_to_managed_job_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    events: list[str] = []

    class FakeChild:
        returncode: int | None = 0

        async def wait(self) -> int:
            events.append("cli-waited")
            return 0

    request_child = FakeChild()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        events.append("graceful-cli")
        return request_child

    async def fake_wait_for(awaitable, *, timeout: float):
        if timeout == process_manager._QQCALL_STOP_REQUEST_TIMEOUT:
            return await awaitable
        events.append("grace-timeout")
        awaitable.close()
        raise asyncio.TimeoutError

    async def fake_terminate(state: process_manager.ServiceState) -> None:
        events.append("managed-cleanup")
        assert "grace-timeout" in events
        state.process = None
        state.process_group_id = None

    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(process_manager.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(manager, "_terminate_state_process", fake_terminate)
    manager.states["qqcall"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING,
        process=FakeChild(),
        pid=321,
        process_group_id=321,
    )

    async def scenario() -> None:
        await manager.stop_service("qqcall")
        assert events == ["graceful-cli", "cli-waited", "grace-timeout", "managed-cleanup"]
        assert manager.get_service_status("qqcall")["status"] == "stopped"

    asyncio.run(scenario())


def test_qqcall_status_phase_is_sanitized_and_requires_fresh_owned_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = process_manager.ProcessManager(tmp_path)
    process_manager._register_services(tmp_path)
    public_config = _write_qqcall_public_config(tmp_path, enabled=True)
    public_config_path = tmp_path / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    assert public_config_path.is_file()
    runtime_dir = Path(public_config["qqcall"]["runtime_dir"])
    runtime_dir.mkdir(exist_ok=True)
    pid = 12345
    creation = "134000000000000000"
    status = {
        "schema": 1,
        "phase": "ready",
        "ready": True,
        "updatedAtMs": int(time.time() * 1000),
        "supervisorPid": pid,
        "supervisorCreationTime100ns": creation,
    }
    status_path = runtime_dir / "status.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    state = process_manager.ServiceState(
        status=process_manager.ServiceStatus.RUNNING,
        process=type("FakeProcess", (), {"returncode": None})(),
        pid=pid,
    )
    manager.states["qqcall"] = state
    monkeypatch.setattr(manager, "_qqcall_process_creation_time_100ns", lambda _pid: creation)

    ready = manager.get_service_status("qqcall")
    assert ready["detail"] == "通话控制链就绪"
    assert ready["qqcall"] == {"phase": "ready", "ready": True, "detail": "通话控制链就绪"}
    assert "code" not in ready["qqcall"]

    status.update({
        "phase": "waiting_kernel_restart",
        "ready": False,
        "code": "private-uid-token-value",
        "updatedAtMs": int(time.time() * 1000),
    })
    status_path.write_text(json.dumps(status), encoding="utf-8")
    waiting = manager.get_service_status("qqcall")
    assert waiting["status"] == "running"
    assert waiting["detail"] == "等待重启 QQ 以启用通话"
    assert waiting["qqcall"] == {
        "phase": "waiting_kernel_restart",
        "ready": False,
        "detail": "等待重启 QQ 以启用通话",
    }
    assert "private-uid-token-value" not in json.dumps(waiting, ensure_ascii=False)

    monkeypatch.setattr(manager, "_qqcall_process_creation_time_100ns", lambda _pid: "different-process")
    reused = manager.get_service_status("qqcall")
    assert reused["qqcall"] is None
    assert reused["detail"] == "QQCall 控制服务进程已启动，等待经验证的状态"

    monkeypatch.setattr(manager, "_qqcall_process_creation_time_100ns", lambda _pid: creation)
    status["updatedAtMs"] -= process_manager._QQCALL_STATUS_MAX_AGE_MS + 1
    status_path.write_text(json.dumps(status), encoding="utf-8")
    stale = manager.get_service_status("qqcall")
    assert stale["qqcall"] is None
    assert stale["detail"] == "QQCall 控制服务进程已启动，等待经验证的状态"

    status.update({
        "phase": "ready",
        "ready": True,
        "updatedAtMs": int(time.time() * 1000),
        "universalService": {
            "state": "unavailable",
            "code": "universal_api_auth_failed",
            "reused": False,
        },
    })
    status.pop("code", None)
    status_path.write_text(json.dumps(status), encoding="utf-8")
    control_ready_audio_unavailable = manager.get_service_status("qqcall")
    assert control_ready_audio_unavailable["qqcall"]["phase"] == "ready"
    assert control_ready_audio_unavailable["qqcall"]["ready"] is True
    assert control_ready_audio_unavailable["qqcall"]["universalService"] == status["universalService"]
    assert "UniversalVC 认证失败" in control_ready_audio_unavailable["detail"]
    assert "QQCall 音频不可用" in control_ready_audio_unavailable["detail"]

    status["universalService"]["code"] = "private-token-value"
    status["updatedAtMs"] = int(time.time() * 1000)
    status_path.write_text(json.dumps(status), encoding="utf-8")
    rejected_component = manager.get_service_status("qqcall")
    assert rejected_component["qqcall"] is None
    assert "private-token-value" not in json.dumps(rejected_component, ensure_ascii=False)

    status.update({"phase": "not-a-phase", "ready": False, "updatedAtMs": int(time.time() * 1000)})
    status_path.write_text(json.dumps(status), encoding="utf-8")
    unknown = manager.get_service_status("qqcall")
    assert unknown["qqcall"] is None


def _external_qqcall_fixture(tmp_path, monkeypatch, *, phase="ready"):
    public = _write_qqcall_public_config(tmp_path, enabled=True)
    runtime = Path(public["qqcall"]["runtime_dir"])
    runtime.mkdir(exist_ok=True)
    creation = "134000000000000000"
    (runtime / "manager.private.json").write_text(json.dumps({
        "schema": 1, "pid": 12345, "creationTime100ns": creation, "runGeneration": 8,
    }))
    status = {"schema": 1, "phase": phase, "ready": phase == "ready",
              "updatedAtMs": int(time.time() * 1000), "supervisorPid": 12345,
              "supervisorCreationTime100ns": creation}
    (runtime / "status.json").write_text(json.dumps(status))
    manager = process_manager.ProcessManager(tmp_path)
    monkeypatch.setattr(manager, "_qqcall_process_creation_time_100ns", lambda _pid: creation)
    process = SimpleNamespace(cwd=lambda: str(tmp_path / "NachoBot-QQCall"),
        cmdline=lambda: ["python.exe", "service/manager.py", "run", "--config",
                        "../NachoBot/plugins/qqcall_plugin/config.toml"], exe=lambda: "C:/Python/python.exe")
    monkeypatch.setattr(process_manager.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(process_manager.psutil, "pid_exists", lambda _pid: True)
    monkeypatch.setattr(manager, "_ensure_required_components", lambda _services: None)
    return manager, runtime, status, process


@pytest.mark.parametrize("phase", ["ready", "needs_attachment", "waiting_adapter", "waiting_kernel_restart"])
@pytest.mark.parametrize("start_mode", ["service", "group"])
def test_external_qqcall_is_observed_and_never_spawned_or_adopted(tmp_path, monkeypatch, phase, start_mode):
    manager, _runtime, _status, _process = _external_qqcall_fixture(tmp_path, monkeypatch, phase=phase)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("external supervisor must not be spawned or terminated")
    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(manager, "_terminate_state_process", forbidden)
    shown = manager.get_service_status("qqcall")
    assert shown["origin"] == "external" and shown["managed"] is False
    assert shown["pid"] is None and shown["qqcall"]["phase"] == phase
    async def scenario():
        if start_mode == "service":
            await manager.start_service("qqcall")
        else:
            await manager.start_group("qqcall")
        await manager.stop_service("qqcall")
    asyncio.run(scenario())
    assert manager.get_service_status("qqcall")["origin"] == "external"


@pytest.mark.parametrize("failure", ["pid_reuse", "dead", "command", "status_stale", "status_pid", "unqueryable"])
def test_external_qqcall_requires_exact_process_and_fresh_matching_status(tmp_path, monkeypatch, failure):
    manager, runtime, status, process = _external_qqcall_fixture(tmp_path, monkeypatch)
    if failure in {"pid_reuse", "dead", "unqueryable"}:
        monkeypatch.setattr(manager, "_qqcall_process_creation_time_100ns", lambda _pid: "different" if failure == "pid_reuse" else None)
        monkeypatch.setattr(process_manager.psutil, "pid_exists", lambda _pid: failure != "dead")
    elif failure == "command":
        process.cmdline = lambda: ["python.exe", "other-service.py"]
    elif failure == "status_stale":
        status["updatedAtMs"] -= process_manager._QQCALL_STATUS_MAX_AGE_MS + 1
    else:
        status["supervisorPid"] += 1
    (runtime / "status.json").write_text(json.dumps(status))
    observation = manager._external_qqcall_observation()
    if failure in {"pid_reuse", "dead"}:
        assert observation is None
    else:
        assert observation["phase"] is None
        assert observation["external_state"] != "ready"


def test_external_ready_wins_over_failed_duplicate_wrapper_without_adoption(tmp_path, monkeypatch):
    manager, _runtime, _status, _process = _external_qqcall_fixture(tmp_path, monkeypatch)
    manager.states["qqcall"] = process_manager.ServiceState(
        status=process_manager.ServiceStatus.ERROR, process=SimpleNamespace(returncode=2), pid=40048,
    )
    shown = manager.get_service_status("qqcall")
    assert shown["status"] == "running" and shown["origin"] == "external"
    assert shown["managed"] is False and shown["qqcall"]["ready"] is True
    assert manager.states["qqcall"].pid == 40048


def test_qqcall_report_window_covers_normal_identity_discovery_cycle(tmp_path, monkeypatch):
    manager, runtime, status, _process = _external_qqcall_fixture(tmp_path, monkeypatch)
    status["updatedAtMs"] -= 20_000
    (runtime / "status.json").write_text(json.dumps(status))
    assert manager.get_service_status("qqcall")["qqcall"]["ready"] is True
    status["updatedAtMs"] -= process_manager._QQCALL_STATUS_MAX_AGE_MS
    (runtime / "status.json").write_text(json.dumps(status))
    assert manager.get_service_status("qqcall")["qqcall"] is None


def test_qqcall_injection_uses_only_fixed_bat_and_deduplicates_requests(tmp_path, monkeypatch):
    manager, _runtime, _status, _process = _external_qqcall_fixture(tmp_path, monkeypatch, phase="needs_attachment")
    entry = tmp_path / "NachoBot-QQCall/start-qqcall.bat"
    entry.write_text("@echo off")
    calls = []
    async def fake_run(path):
        calls.append(path)
    monkeypatch.setattr(manager, "_run_qqcall_injection", fake_run)
    async def scenario():
        assert manager.request_qqcall_injection() == {"status": "injecting"}
        assert manager.request_qqcall_injection() == {"status": "injecting"}
        await manager._operation_tasks["qqcall:inject"]
        assert calls == [entry]
    asyncio.run(scenario())


def test_qqcall_ready_injection_is_noop_and_kernel_restart_cannot_be_reinjected(tmp_path, monkeypatch):
    manager, runtime, status, _process = _external_qqcall_fixture(tmp_path, monkeypatch)
    assert manager.request_qqcall_injection() == {"status": "ready"}
    assert "qqcall:inject" not in manager._operation_tasks
    status.update(phase="waiting_kernel_restart", ready=False)
    (runtime / "status.json").write_text(json.dumps(status))
    with pytest.raises(RuntimeError, match="等待重启 QQ"):
        manager.request_qqcall_injection()


def test_qqcall_bat_launch_is_hidden_and_never_becomes_a_managed_service(tmp_path, monkeypatch):
    manager, _runtime, _status, _process = _external_qqcall_fixture(tmp_path, monkeypatch)
    calls = []
    class Child:
        async def wait(self): return 0
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Child()
    monkeypatch.setattr(process_manager.asyncio, "create_subprocess_exec", spawn)
    entry = tmp_path / "NachoBot-QQCall/start-qqcall.bat"
    asyncio.run(manager._run_qqcall_injection(entry))
    assert calls[0][0] == ("cmd.exe", "/d", "/c", str(entry))
    assert calls[0][1]["stdin"] is None
    assert calls[0][1]["stdout"] is None
    assert calls[0][1]["stderr"] is None
    assert calls[0][1]["creationflags"] == getattr(process_manager.subprocess, "CREATE_NEW_CONSOLE", 0)
    state = manager.states.get("qqcall")
    assert state is None or (state.process is None and state.pid is None and state.windows_job is None)
    assert manager.get_service_status("qqcall")["managed"] is False
