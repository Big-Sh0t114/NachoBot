from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from plugins.mai_recog_self import deploy as deployment
from plugins.mai_recog_self.backend import control
from plugins.mai_recog_self.backend.environment import worker_environment
from plugins.mai_recog_self.backend.labels import metadata_digest
from plugins.mai_recog_self.backend.paths import CORE_ROOT
from plugins.mai_recog_self.backend.store import Store, digest


VERSION = "b" * 32
JOB_ID = "a" * 32
CLASSES = ["NachoBot", "other"]
PROBE_BYTES = b"frozen TRAIN probe fixture; tests never train on it"
PLUGIN_CLI = CORE_ROOT / "plugins" / "mai_recog_self" / "deploy.py"
BACKEND_CLI = CORE_ROOT / "plugins" / "mai_recog_self" / "self_recognition.py"
WRAPPER = CORE_ROOT / "plugins" / "mai_recog_self" / "setup_self_recognition.ps1"


def _make_catalog(tmp_path: Path, *, config_bytes: bytes | None = None) -> dict:
    root = tmp_path / "catalog & data"
    store = Store(root)
    worker = tmp_path / "worker python.exe"
    worker.write_bytes(b"test worker path")
    previous_worker = tmp_path / "previous worker.exe"
    previous_worker.write_bytes(b"previous worker path")

    probe_path = root / "images" / "frozen-train-probe.bin"
    probe_path.write_bytes(PROBE_BYTES)
    probe_sha = hashlib.sha256(PROBE_BYTES).hexdigest()
    metadata = {
        "format_version": 2,
        "task": "multiclass",
        "class_names": CLASSES,
        "score_threshold": 0.6,
        "margin_threshold": 0.15,
    }
    snapshot = {
        **metadata,
        "input_size": 224,
        "metadata_sha256": metadata_digest({**metadata, "input_size": 224}),
        "images": [{
            "id": "1" * 32,
            "path": "images/frozen-train-probe.bin",
            "sha": probe_sha,
            "label": "NachoBot",
            "split": "train",
        }],
    }
    snapshot_sha = digest(snapshot)
    job_dir = root / "jobs" / JOB_ID
    job_dir.mkdir(parents=True)
    (job_dir / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    model_dir = root / "models" / VERSION
    model_dir.mkdir(parents=True)
    weights = model_dir / "weights.pt"
    weights.write_bytes(b"fixture model weights; CPU validation is mocked")
    weights_sha = hashlib.sha256(weights.read_bytes()).hexdigest()
    expected_metadata = {
        **metadata,
        "metadata_sha256": snapshot["metadata_sha256"],
    }
    audit = {
        "passed": True,
        "sha256": weights_sha,
        "snapshot_sha": snapshot_sha,
        **expected_metadata,
    }
    (model_dir / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
    metrics = {**expected_metadata, "input_size": 224}
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (JOB_ID, JOB_ID, "complete", 1.0, snapshot_sha),
        )
        db.execute(
            "INSERT INTO models (id,job_id,sha,metrics,created) VALUES (?,?,?,?,?)",
            (VERSION, JOB_ID, weights_sha, json.dumps(metrics), 1.0),
        )
    store.set_setting("active", "c" * 32)
    store.set_setting("previous", "d" * 32)
    store.set_setting("worker_python", str(previous_worker))

    config_path = tmp_path / "config & plugin.toml"
    if config_bytes is None:
        config_bytes = (
            b"\xef\xbb\xbf# top-level comment\r\n"
            b"[plugin]\r\n"
            b"# keep plugin comment\r\n"
            b"enabled = false\r\n"
            b"version = \"1.0.0\"\r\n"
            b"\r\n[recognition]\r\n"
            b"# keep recognition comment\r\n"
            b"auto_recognize = false\r\n"
            b"character_name = \"Existing Name\"\r\n"
        )
    config_path.write_bytes(config_bytes)
    return {
        "root": root,
        "store": store,
        "worker": worker,
        "previous_worker": previous_worker,
        "config": config_path,
        "weights": weights,
        "probe": probe_path,
        "snapshot": snapshot,
        "weights_sha": weights_sha,
    }


def _mock_dependencies(monkeypatch):
    def ensure(worker_python, root, install_missing, changes):
        return {
            "torch": "2.13.0+cu126",
            "torchvision": "0.28.0+cu126",
            "Pillow": "12.3.0",
            "numpy": "2.4.6",
            "psutil": "7.2.2",
        }

    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", ensure)


class _InferenceService:
    def __init__(self, root, worker_python):
        self.root = Path(root)
        self.worker_python = worker_python
        self.closed = False

    def status(self):
        return {"desired_version": VERSION, "loaded_version": VERSION, "ready": True, "warming": False, "last_error": ""}

    def infer(self, raw, timeout):
        assert raw == PROBE_BYTES
        assert timeout > 0
        return {
            "status": "ok",
            "task": "multiclass",
            "format_version": 2,
            "class_names": list(CLASSES),
            "scores": {"NachoBot": 0.81, "other": 0.19},
            "predicted_class": "NachoBot",
            "character_id": "NachoBot",
            "version": VERSION,
            "desired_version": VERSION,
            "loaded_version": VERSION,
        }

    def close(self):
        self.closed = True


def _setup_deployment(monkeypatch, fixture, service_type=_InferenceService):
    _mock_dependencies(monkeypatch)
    monkeypatch.setattr(deployment, "decode", lambda _raw: None)
    instances = []

    def service_factory(root, worker_python):
        service = service_type(root, worker_python)
        instances.append(service)
        return service

    monkeypatch.setattr(deployment, "InferenceService", service_factory)
    monkeypatch.setattr(control, "_run_validation", lambda _store, version, _worker: {"status": "ready", "version": version})
    return instances


def _settings(store: Store) -> dict:
    return {key: store.setting(key, "<missing>") for key in ("active", "previous", "worker_python")}


def _run_deploy(fixture: dict, **kwargs) -> dict:
    return deployment.deploy(
        fixture["root"],
        VERSION,
        "NachoBot",
        worker_python=str(fixture["worker"]),
        plugin_config=fixture["config"],
        **kwargs,
    )


def test_success_activates_probes_then_preserves_config_comments_and_worker_settings(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    services = _setup_deployment(monkeypatch, fixture)
    original = fixture["config"].read_bytes()

    result = _run_deploy(fixture)

    assert result["status"] == "deployed"
    assert result["version"] == VERSION
    assert result["worker_python"] == str(fixture["worker"].resolve())
    assert result["config_changed"] is True
    assert result["restart_required"] is True
    assert Path(result["config_backup_path"]).read_bytes() == original
    assert result["probe"]["source"] == "snapshot_train"
    assert result["probe"]["sha256"] == hashlib.sha256(PROBE_BYTES).hexdigest()
    assert result["dependency_changes"] == []
    assert result["inference"]["class_names"] == CLASSES
    assert len(services) == 1 and services[0].closed is True
    assert services[0].root == fixture["root"].resolve()
    assert services[0].worker_python == str(fixture["worker"].resolve())

    written = fixture["config"].read_bytes()
    assert written.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in written and b"\r\r\n" not in written
    document = deployment.tomlkit.parse(written.decode("utf-8-sig"))
    assert document["plugin"]["enabled"] is True
    assert document["plugin"]["version"] == "1.0.0"
    assert document["recognition"]["data_dir"] == str(fixture["root"].resolve())
    assert document["recognition"]["self_character_id"] == "NachoBot"
    assert document["recognition"]["worker_python"] == str(fixture["worker"].resolve())
    assert document["recognition"]["auto_recognize"] is False
    assert document["recognition"]["character_name"] == "Existing Name"
    assert b"# top-level comment" in written
    assert b"# keep plugin comment" in written
    assert b"# keep recognition comment" in written
    assert _settings(Store(fixture["root"], initialize=False)) == {
        "active": VERSION,
        "previous": "c" * 32,
        "worker_python": str(fixture["worker"].resolve()),
    }


def test_repeat_deploy_is_config_noop_and_preserves_previous_rollback_target(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    _setup_deployment(monkeypatch, fixture)
    first = _run_deploy(fixture)
    config_bytes = fixture["config"].read_bytes()
    config_mtime = fixture["config"].stat().st_mtime_ns
    backups_before = list(tmp_path.glob("config & plugin.toml.bak.*"))

    second = _run_deploy(fixture)

    assert first["config_changed"] is True
    assert second["config_changed"] is False
    assert second["config_backup_path"] is None
    assert second["restart_required"] is False
    assert fixture["config"].read_bytes() == config_bytes
    assert fixture["config"].stat().st_mtime_ns == config_mtime
    assert list(tmp_path.glob("config & plugin.toml.bak.*")) == backups_before
    assert Store(fixture["root"], initialize=False).setting("previous") == "c" * 32


def test_repeat_deploy_detects_concurrent_config_edit_during_inference_even_for_noop_payload(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    _setup_deployment(monkeypatch, fixture)
    _run_deploy(fixture)
    before_settings = _settings(Store(fixture["root"], initialize=False))
    backups_before = list(tmp_path.glob("config & plugin.toml.bak.*"))
    concurrent_edit = fixture["config"].read_bytes() + b"# edited during inference\r\n"

    def inference_probe(*_args, **_kwargs):
        fixture["config"].write_bytes(concurrent_edit)
        return {"status": "ok", "version": VERSION}

    monkeypatch.setattr(deployment, "_run_inference_probe", inference_probe)
    with pytest.raises(deployment.DeploymentError, match="并发编辑"):
        _run_deploy(fixture)

    assert fixture["config"].read_bytes() == concurrent_edit
    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert list(tmp_path.glob("config & plugin.toml.bak.*")) == backups_before


@pytest.mark.parametrize("table_name", ["plugin", "recognition"])
def test_non_table_live_plugin_sections_fail_before_any_mutation(tmp_path, monkeypatch, table_name):
    fixture = _make_catalog(tmp_path, config_bytes=f'{table_name} = "not a table"\n'.encode())
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes()
    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", lambda *_args, **_kwargs: pytest.fail("dependency check must not run"))

    with pytest.raises(deployment.DeploymentError, match=rf"\[{table_name}\].*TOML表"):
        _run_deploy(fixture)

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes() == before_config


def test_malformed_live_toml_fails_before_model_or_dependency_mutation(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path, config_bytes=b"[plugin\nenabled = true\n")
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes()
    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", lambda *_args, **_kwargs: pytest.fail("dependency check must not run"))

    with pytest.raises(deployment.DeploymentError, match="TOML格式错误"):
        _run_deploy(fixture)

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes() == before_config


def test_wrong_case_self_class_fails_before_dependencies_or_activation(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    before_settings = _settings(fixture["store"])
    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", lambda *_args, **_kwargs: pytest.fail("dependency check must not run"))

    with pytest.raises(deployment.DeploymentError, match="大小写敏感"):
        deployment.deploy(
            fixture["root"], VERSION, "nachobot", worker_python=str(fixture["worker"]), plugin_config=fixture["config"]
        )

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes().startswith(b"\xef\xbb\xbf")


def test_other_cannot_be_selected_as_self_character_id(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", lambda *_args, **_kwargs: pytest.fail("dependency check must not run"))

    with pytest.raises(deployment.DeploymentError, match="不能是other"):
        deployment.deploy(
            fixture["root"], VERSION, "other", worker_python=str(fixture["worker"]), plugin_config=fixture["config"]
        )


def test_tampered_weight_hash_fails_without_changing_settings_or_config(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    fixture["weights"].write_bytes(b"tampered after catalog registration")
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes()
    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", lambda *_args, **_kwargs: pytest.fail("dependency check must not run"))

    with pytest.raises(ValueError, match="模型哈希"):
        _run_deploy(fixture)

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes() == before_config


@pytest.mark.parametrize("missing", ["psutil", "torch"])
def test_missing_dependencies_require_explicit_opt_in_and_never_install_heavy_packages(tmp_path, monkeypatch, missing):
    root = tmp_path / "catalog"
    root.mkdir()
    packages = {
        "torch": {"version": "2.13.0+cu126", "path": "torch.py", "missing": False, "error": ""},
        "torchvision": {"version": "0.28.0+cu126", "path": "torchvision.py", "missing": False, "error": ""},
        "Pillow": {"version": "12.3.0", "path": "PIL.py", "missing": False, "error": ""},
        "numpy": {"version": "2.4.6", "path": "numpy.py", "missing": False, "error": ""},
        "psutil": {"version": "7.2.2", "path": "psutil.py", "missing": False, "error": ""},
    }
    packages[missing] = {"version": "", "path": "", "missing": True, "error": "ModuleNotFoundError"}
    monkeypatch.setattr(deployment, "_discover_worker_packages", lambda *_args: _package_discovery(missing={missing}))
    monkeypatch.setattr(deployment, "_probe_worker", lambda *_args: packages)
    monkeypatch.setattr(deployment.shutil, "which", lambda _name: pytest.fail("installer discovery must not run"))
    monkeypatch.setattr(
        deployment,
        "_load_requirements",
        lambda: {
            "torch": deployment.Requirement("torch>=2.6,<3"),
            "torchvision": deployment.Requirement("torchvision>=0.21,<1"),
            "Pillow": deployment.Requirement("Pillow>=11,<13"),
            "numpy": deployment.Requirement("numpy>=2,<3"),
            "psutil": deployment.Requirement("psutil>=6,<8"),
        },
    )
    changes = []

    expected = "--install-missing" if missing == "psutil" else "不会下载或重建大型框架"
    with pytest.raises(deployment.DeploymentError, match=expected):
        deployment._ensure_worker_dependencies(str(tmp_path / "worker.exe"), root, False, changes)
    assert changes == []


def _package_probe(missing_psutil: bool = False, root: Path | None = None) -> dict:
    psutil_path = (
        str(root.resolve() / "worker_deps" / "psutil" / "__init__.py")
        if root is not None
        else "C:/catalog/worker_deps/psutil/__init__.py"
    )
    packages = {
        "torch": {"version": "2.13.0+cu126", "path": "C:/worker/torch.py", "missing": False, "error": ""},
        "torchvision": {"version": "0.28.0+cu126", "path": "C:/worker/torchvision.py", "missing": False, "error": ""},
        "Pillow": {"version": "12.3.0", "path": "C:/worker/PIL.py", "missing": False, "error": ""},
        "numpy": {"version": "2.4.6", "path": "C:/worker/numpy.py", "missing": False, "error": ""},
        "psutil": {"version": "", "path": "", "missing": True, "error": "ModuleNotFoundError: No module named 'psutil'"}
        if missing_psutil
        else {"version": "7.2.2", "path": psutil_path, "missing": False, "error": ""},
    }
    return {"packages": packages}


def _package_discovery(missing: set[str] | None = None, versions: dict[str, str] | None = None) -> dict:
    missing = missing or set()
    versions = versions or {
        "torch": "2.13.0+cu126",
        "torchvision": "0.28.0+cu126",
        "Pillow": "12.3.0",
        "numpy": "2.4.6",
        "psutil": "7.2.2",
    }
    return {
        name: {
            "present": name not in missing,
            "version": "" if name in missing else versions.get(name, ""),
            "path": f"{name}.py" if name not in missing else "",
            "error": "",
        }
        for name in deployment._WORKER_IMPORTS
    }


def _probe_with_transitive_missing_small_package(missing: str) -> dict:
    module_name = deployment._WORKER_IMPORTS[missing]
    missing_error = f"ModuleNotFoundError: No module named '{module_name}'"
    packages = _package_probe()["packages"]
    packages["torchvision"] = {
        "version": "",
        "path": "",
        "missing": False,
        "missing_dependency": module_name,
        "error": missing_error,
    }
    packages[missing] = {
        "version": "",
        "path": "",
        "missing": True,
        "missing_dependency": module_name,
        "error": missing_error,
    }
    return packages


@pytest.mark.parametrize("missing", ["Pillow", "numpy"])
@pytest.mark.parametrize("install_missing", [False, True])
def test_transitive_torchvision_import_failure_installs_only_discovered_small_dependency_and_rechecks_fresh(
    tmp_path, monkeypatch, missing, install_missing
):
    root = tmp_path / "catalog"
    root.mkdir()
    worker = str(tmp_path / "worker.exe")
    monkeypatch.setattr(deployment, "_load_requirements", lambda: {
        "torch": deployment.Requirement("torch>=2.6,<3"),
        "torchvision": deployment.Requirement("torchvision>=0.21,<1"),
        "Pillow": deployment.Requirement("Pillow>=11,<13"),
        "numpy": deployment.Requirement("numpy>=2,<3"),
        "psutil": deployment.Requirement("psutil>=6,<8"),
    })
    monkeypatch.setattr(deployment, "_discover_worker_packages", lambda *_args: _package_discovery(missing={missing}))
    probes = []

    def probe(*_args):
        probes.append(1)
        if len(probes) == 1:
            return _probe_with_transitive_missing_small_package(missing)
        return _package_probe()["packages"]

    monkeypatch.setattr(deployment, "_probe_worker", probe)
    install_calls = []

    def install(packages, _worker, _root, _requirements, changes):
        install_calls.append(list(packages))
        change = {"package": packages[0], "installer": "fixture"}
        changes.append(change)
        return [change]

    monkeypatch.setattr(deployment, "_install_missing_packages", install)
    changes = []
    if install_missing:
        versions = deployment._ensure_worker_dependencies(worker, root, True, changes)
        assert versions["torchvision"] == "0.28.0+cu126"
        assert install_calls == [[missing]]
        assert len(probes) == 2
        assert [change["package"] for change in changes] == [missing]
    else:
        with pytest.raises(deployment.DeploymentError, match="--install-missing"):
            deployment._ensure_worker_dependencies(worker, root, False, changes)
        assert install_calls == []
        assert len(probes) == 1
        assert changes == []


def test_broken_framework_import_is_rejected_before_installing_an_absent_small_package(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    root.mkdir()
    worker = str(tmp_path / "worker.exe")
    monkeypatch.setattr(deployment, "_load_requirements", lambda: {
        "torch": deployment.Requirement("torch>=2.6,<3"),
        "torchvision": deployment.Requirement("torchvision>=0.21,<1"),
        "Pillow": deployment.Requirement("Pillow>=11,<13"),
        "numpy": deployment.Requirement("numpy>=2,<3"),
        "psutil": deployment.Requirement("psutil>=6,<8"),
    })
    monkeypatch.setattr(deployment, "_discover_worker_packages", lambda *_args: _package_discovery(missing={"Pillow"}))
    packages = _probe_with_transitive_missing_small_package("Pillow")
    packages["torchvision"] = {
        "version": "",
        "path": "",
        "missing": False,
        "missing_dependency": "",
        "error": "OSError: fixture broken framework import",
    }
    monkeypatch.setattr(deployment, "_probe_worker", lambda *_args: packages)
    monkeypatch.setattr(deployment, "_install_missing_packages", lambda *_args: pytest.fail("broken framework must fail before install"))
    changes = []

    with pytest.raises(deployment.DeploymentError, match="torchvision导入失败"):
        deployment._ensure_worker_dependencies(worker, root, True, changes)

    assert changes == []


@pytest.mark.parametrize("framework_state", ["missing", "incompatible"])
def test_missing_or_incompatible_torchvision_fails_before_installing_absent_small_dependency(
    tmp_path, monkeypatch, framework_state
):
    root = tmp_path / "catalog"
    root.mkdir()
    worker = str(tmp_path / "worker.exe")
    monkeypatch.setattr(deployment, "_load_requirements", lambda: {
        "torch": deployment.Requirement("torch>=2.6,<3"),
        "torchvision": deployment.Requirement("torchvision>=0.21,<1"),
        "Pillow": deployment.Requirement("Pillow>=11,<13"),
        "numpy": deployment.Requirement("numpy>=2,<3"),
        "psutil": deployment.Requirement("psutil>=6,<8"),
    })
    discovered = _package_discovery(missing={"Pillow"})
    if framework_state == "missing":
        discovered["torchvision"]["present"] = False
        discovered["torchvision"]["version"] = ""
    else:
        discovered["torchvision"]["version"] = "1.0.0"
    monkeypatch.setattr(deployment, "_discover_worker_packages", lambda *_args: discovered)
    monkeypatch.setattr(deployment, "_probe_worker", lambda *_args: pytest.fail("framework discovery must fail before import"))
    monkeypatch.setattr(deployment, "_install_missing_packages", lambda *_args: pytest.fail("framework must fail before install"))
    changes = []

    expected = "worker缺少torchvision" if framework_state == "missing" else "torchvision版本1.0.0不满足"
    with pytest.raises(deployment.DeploymentError, match=expected):
        deployment._ensure_worker_dependencies(worker, root, True, changes)

    assert changes == []


def test_uv_installs_only_missing_small_package_to_catalog_overlay_and_probe_scrubs_parent_python_paths(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    root.mkdir()
    worker = str(tmp_path / "worker python.exe")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "temporary test overlay"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "bad python home"))
    monkeypatch.setattr(deployment.shutil, "which", lambda name: "C:/tools/uv.exe" if name == "uv" else None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((list(command), kwargs))
        if command[0] == worker:
            if command[-1] == deployment._PACKAGE_DISCOVERY:
                payload = {"packages": _package_discovery(missing={"psutil"})}
            else:
                payload = _package_probe(
                    missing_psutil=len([call for call in calls if call[0][0] == worker and call[0][-1] == deployment._PACKAGE_PROBE]) == 1,
                    root=root,
                )
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        (root / "worker_deps").mkdir(exist_ok=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deployment.subprocess, "run", fake_run)
    changes = []
    versions = deployment._ensure_worker_dependencies(worker, root, True, changes)

    install_calls = [command for command, _kwargs in calls if command[0] != worker]
    assert install_calls == [[
        "C:/tools/uv.exe", "pip", "install", "--python", worker, "--target",
        str((root / "worker_deps").resolve()), "--no-deps", "psutil<8,>=6",
    ]]
    assert [item["package"] for item in changes] == ["psutil"]
    assert versions["psutil"] == "7.2.2"
    assert Path(versions["psutil_source"]).as_posix().endswith("/worker_deps/psutil/__init__.py")
    for _command, kwargs in calls:
        assert "PYTHONHOME" not in kwargs["env"]
        python_path = kwargs["env"].get("PYTHONPATH", "")
        assert python_path in ("", str((root / "worker_deps").resolve()))
        assert str(tmp_path / "temporary test overlay") not in python_path
    assert calls[-1][1]["env"]["PYTHONPATH"] == str((root / "worker_deps").resolve())


def test_partial_small_dependency_install_is_reported_when_later_package_fails(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    root.mkdir()
    worker = str(tmp_path / "worker.exe")
    monkeypatch.setattr(deployment.shutil, "which", lambda name: "uv.exe" if name == "uv" else None)
    requirements = {
        "numpy": deployment.Requirement("numpy>=2,<3"),
        "psutil": deployment.Requirement("psutil>=6,<8"),
    }
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[-1].startswith("psutil"):
            return subprocess.CompletedProcess(command, 1, "", "fixture install failure")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(deployment, "_run_process", fake_run)
    changes = []

    with pytest.raises(deployment.DeploymentError, match="fixture install failure"):
        deployment._install_missing_packages(["numpy", "psutil"], worker, root, requirements, changes)

    assert [item["package"] for item in changes] == ["numpy"]
    assert calls[0][-1] == "numpy<3,>=2"
    assert calls[1][-1] == "psutil<8,>=6"


@pytest.mark.parametrize(
    ("package", "version", "error", "message"),
    [
        ("numpy", "3.0.0", "", "不满足"),
        ("psutil", "", "OSError: broken import", "导入失败"),
    ],
)
def test_existing_out_of_range_or_broken_package_fails_without_installing(tmp_path, monkeypatch, package, version, error, message):
    root = tmp_path / "catalog"
    root.mkdir()
    packages = _package_probe()["packages"]
    packages[package] = {"version": version, "path": "existing", "missing": False, "error": error}
    discovered = _package_discovery()
    discovered[package]["version"] = version or "7.2.2"
    monkeypatch.setattr(deployment, "_discover_worker_packages", lambda *_args: discovered)
    monkeypatch.setattr(deployment, "_probe_worker", lambda *_args: packages)
    monkeypatch.setattr(deployment.shutil, "which", lambda _name: pytest.fail("must not upgrade existing packages"))
    changes = []

    with pytest.raises(deployment.DeploymentError, match=message):
        deployment._ensure_worker_dependencies(str(tmp_path / "worker.exe"), root, True, changes)
    assert changes == []


def test_worker_environment_clears_inherited_python_overlays_and_uses_only_catalog_deps(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    overlay = root / "worker_deps"
    overlay.mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "temporary-overlay")
    monkeypatch.setenv("PYTHONHOME", "temporary-home")

    environment = worker_environment(root)

    assert environment["PYTHONPATH"] == str(overlay.resolve())
    assert "PYTHONHOME" not in environment
    no_overlay = worker_environment(tmp_path / "empty-catalog")
    assert "PYTHONPATH" not in no_overlay
    assert "PYTHONHOME" not in no_overlay
    assert os.environ["PYTHONPATH"] == "temporary-overlay"


def test_worker_environment_is_passed_to_validation_training_and_inference_children(tmp_path, monkeypatch):
    root = tmp_path / "catalog"
    store = Store(root)
    overlay = root / "worker_deps"
    overlay.mkdir()
    monkeypatch.setenv("PYTHONPATH", "parent-overlay")
    monkeypatch.setenv("PYTHONHOME", "parent-home")
    captured = {}

    def validation_run(command, **kwargs):
        captured["validation"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": "ready", "version": VERSION}), "")

    monkeypatch.setattr(control.subprocess, "run", validation_run)
    control._run_validation(store, VERSION, str(tmp_path / "worker.exe"))

    job_dir = root / "jobs" / JOB_ID
    job_dir.mkdir()
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs (id,request_key,state,pid,error,created,snapshot_sha) VALUES (?,?,?,NULL,'',?,?)",
            (JOB_ID, JOB_ID, "queued", 1.0, "0" * 64),
        )

    class Child:
        pid = 12345

    def training_popen(_command, **kwargs):
        captured["training"] = kwargs["env"]
        return Child()

    monkeypatch.setattr(control.subprocess, "Popen", training_popen)
    control._spawn(store, JOB_ID, str(tmp_path / "worker.exe"))

    from plugins.mai_recog_self.backend import runtime

    class RuntimeChild:
        stdin = type("Stream", (), {"close": lambda self: None})()
        stdout = type("Stream", (), {"close": lambda self: None})()
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            self.returncode = self.returncode or 0
            return self.returncode

        def kill(self):
            self.returncode = -9

    def inference_popen(_command, **kwargs):
        captured["inference"] = kwargs["env"]
        return RuntimeChild()

    monkeypatch.setattr(runtime.subprocess, "Popen", inference_popen)
    service = runtime.InferenceService(root)
    service._spawn()
    service.close()

    for name in ("validation", "training", "inference"):
        assert captured[name]["PYTHONPATH"] == str(overlay.resolve())
        assert "PYTHONHOME" not in captured[name]


class _FailingInference(_InferenceService):
    def infer(self, raw, timeout):
        return {"status": "model_error", "error": "fixture rejection", "loaded_version": ""}


def test_failed_inference_closes_service_and_restores_active_previous_worker_and_config(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes()
    services = _setup_deployment(monkeypatch, fixture, _FailingInference)

    with pytest.raises(deployment.DeploymentError, match="推理探针失败"):
        _run_deploy(fixture)

    assert len(services) == 1 and services[0].closed is True
    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes() == before_config
    assert list(tmp_path.glob("config & plugin.toml.bak.*")) == []


def test_failed_cpu_activation_restores_worker_setting_written_before_validation(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes()
    _mock_dependencies(monkeypatch)
    monkeypatch.setattr(deployment, "decode", lambda _raw: None)
    monkeypatch.setattr(control, "_run_validation", lambda *_args: (_ for _ in ()).throw(ValueError("CPU rejected")))

    with pytest.raises(deployment.DeploymentError, match="CPU rejected"):
        _run_deploy(fixture)

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert fixture["config"].read_bytes() == before_config


@pytest.mark.parametrize("config_exists", [True, False])
def test_config_write_failure_restores_catalog_and_original_file_or_absence(tmp_path, monkeypatch, config_exists):
    fixture = _make_catalog(tmp_path)
    if not config_exists:
        fixture["config"].unlink()
    before_settings = _settings(fixture["store"])
    before_config = fixture["config"].read_bytes() if config_exists else None
    _setup_deployment(monkeypatch, fixture)
    monkeypatch.setattr(
        deployment,
        "_atomic_write_if_unchanged",
        lambda *_args: (_ for _ in ()).throw(OSError("fixture write failure")),
    )

    with pytest.raises(deployment.DeploymentError, match="fixture write failure"):
        _run_deploy(fixture)

    assert _settings(Store(fixture["root"], initialize=False)) == before_settings
    assert (fixture["config"].read_bytes() if fixture["config"].exists() else None) == before_config
    assert list(tmp_path.glob("config & plugin.toml.bak.*")) == []


def test_concurrent_config_edit_after_preflight_is_preserved_and_blocks_activation(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    before_settings = _settings(fixture["store"])
    user_edit = b"# concurrent user edit\n[plugin]\nenabled = false\n[recognition]\nauto_recognize = true\n"
    calls = 0

    def mutate_during_dependency_probe(worker, root, install_missing, changes):
        nonlocal calls
        calls += 1
        if calls == 1:
            fixture["config"].write_bytes(user_edit)
        return {"torch": "2.13.0+cu126"}

    monkeypatch.setattr(deployment, "_ensure_worker_dependencies", mutate_during_dependency_probe)
    monkeypatch.setattr(control, "_run_validation", lambda *_args: pytest.fail("activation must not run"))
    monkeypatch.setattr(deployment, "decode", lambda _raw: None)

    with pytest.raises(deployment.DeploymentError, match="并发编辑"):
        _run_deploy(fixture)

    assert fixture["config"].read_bytes() == user_edit
    assert _settings(Store(fixture["root"], initialize=False)) == before_settings


def test_missing_config_creates_minimal_default_tables(tmp_path, monkeypatch):
    fixture = _make_catalog(tmp_path)
    fixture["config"].unlink()
    _setup_deployment(monkeypatch, fixture)

    result = _run_deploy(fixture)

    assert result["config_changed"] is True
    assert result["config_backup_path"] is None
    document = deployment.tomlkit.parse(fixture["config"].read_text("utf-8"))
    assert document["plugin"]["enabled"] is True
    assert document["recognition"]["data_dir"] == str(fixture["root"].resolve())
    assert document["recognition"]["self_character_id"] == "NachoBot"
    assert document["recognition"]["worker_python"] == str(fixture["worker"].resolve())
    assert "auto_recognize" not in document["recognition"]


def test_tomlkit_round_trip_keeps_crlf_bom_comments_and_multiline_value_semantics(tmp_path):
    raw = (
        b"\xef\xbb\xbf# original\r\n"
        b"description = \"\"\"first line\r\nsecond line\"\"\"\r\n"
        b"[plugin]\r\nenabled = false\r\n\r\n[recognition]\r\nauto_recognize = true\r\n"
    )
    path = tmp_path / "existing.toml"
    path.write_bytes(raw)

    snapshot = deployment._load_config(path)
    rendered = deployment._render_config(snapshot, tmp_path / "catalog", "NachoBot", str(tmp_path / "worker.exe"))

    assert rendered.startswith(b"\xef\xbb\xbf")
    assert b"\r\r\n" not in rendered
    assert b"\r\n" in rendered
    parsed = deployment.tomlkit.parse(rendered.decode("utf-8-sig"))
    assert parsed["description"] == "first line\r\nsecond line"
    assert b"# original" in rendered


def test_plugin_cli_help_from_foreign_directory_stays_torch_and_plugin_system_free(tmp_path):
    code = f"""
import runpy, sys
sys.argv = [{str(PLUGIN_CLI)!r}, '--help']
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit:
    print('TORCH_IMPORTED', 'torch' in sys.modules)
    print('PLUGIN_IMPORTED', 'plugins.mai_recog_self.plugin' in sys.modules)
    print('PLUGIN_SYSTEM_IMPORTED', 'src.plugin_system' in sys.modules)
    raise
"""
    completed = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    assert "--self-character-id" in completed.stdout
    assert "--install-missing" in completed.stdout
    assert "TORCH_IMPORTED False" in completed.stdout
    assert "PLUGIN_IMPORTED False" in completed.stdout
    assert "PLUGIN_SYSTEM_IMPORTED False" in completed.stdout


def test_plugin_backend_cli_help_has_no_deployment_command(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(BACKEND_CLI), "--help"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "deploy" not in completed.stdout.lower()
    assert "--install-missing" not in completed.stdout


def test_deployment_entrypoints_and_test_are_owned_by_plugin_directory():
    assert PLUGIN_CLI.is_file()
    assert BACKEND_CLI.is_file()
    assert WRAPPER.is_file()
    assert deployment.DEFAULT_PLUGIN_CONFIG == (PLUGIN_CLI.parent / "config.toml").resolve()
    assert Path(__file__).resolve() == (CORE_ROOT / "plugins" / "mai_recog_self" / "tests" / "test_deploy.py").resolve()
    assert (CORE_ROOT / "plugins" / "mai_recog_self" / "backend").is_dir()
    assert (CORE_ROOT / "plugins" / "mai_recog_self" / "BACKEND.md").is_file()
    assert not (CORE_ROOT / "src" / "self_recognition").exists()
    assert not (CORE_ROOT / "scripts" / "self_recognition.py").exists()
    assert not (CORE_ROOT / "docs" / "self-recognition.md").exists()
    assert not (CORE_ROOT / "scripts" / "setup_self_recognition.ps1").exists()
    for test_name in (
        "test_character_recognition_backend.py",
        "test_character_recognition_plugin.py",
        "test_self_recognition_backend.py",
        "test_self_recognition_backend_cpu_acceptance.py",
        "test_self_recognition_backend_runtime.py",
        "test_self_recognition_plugin.py",
        "test_self_recognition_tool_context.py",
    ):
        assert not (CORE_ROOT / "tests" / test_name).exists()


@pytest.mark.skipif(os.name != "nt" or not (shutil.which("powershell.exe") or shutil.which("powershell")), reason="Windows PowerShell is required")
def test_powershell_wrapper_forwards_quoted_path_arguments_as_an_array(tmp_path):
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    fixture = _make_catalog(tmp_path)
    bad_config = tmp_path / "malformed & plugin.toml"
    bad_config.write_text("[plugin\nenabled = true\n", encoding="utf-8")
    command = [
        powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER),
        "-DataDir", str(fixture["root"]), "-Version", VERSION, "-SelfCharacterId", "NachoBot",
        "-WorkerPython", str(fixture["worker"]), "-PluginConfig", str(bad_config),
        "-ProbeImage", str(tmp_path / "probe & image.png"), "-Timeout", "2.5",
    ]
    completed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert completed.returncode != 0
    payload = json.loads(completed.stdout or completed.stderr)
    assert str(bad_config) in payload["error"]
    assert "TOML" in payload["error"] or "toml" in payload["error"]
    assert "--install-missing" in WRAPPER.read_text("utf-8")
