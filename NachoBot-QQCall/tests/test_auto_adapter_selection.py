import copy
import json
from types import SimpleNamespace

import pytest

from service.manager import ServiceError
from service import snowluma
from service.napcat import configured_onebot_connections
from service.windows import WindowsServiceError
import test_service_manager as fixtures


class Candidates(fixtures.FakeSystem):
    def __init__(self):
        super().__init__()
        self.manifests = {
            name: {**copy.deepcopy(self.manifest), "adapter": name}
            for name in ("napcat", "snowluma")
        }
        self.seen = []
        self.reject = set()

    def read_manifest(self, config):
        self.seen.append(config["adapter"])
        return self.manifests.get(config["adapter"])

    def bind_adapter(self, config, manifest):
        if config["adapter"] in self.reject:
            raise WindowsServiceError("adapter_process_mismatch")
        return super().bind_adapter(config, manifest)


def supervisor(root, system):
    return fixtures.ServiceManagerTests()._supervisor(
        root, config=fixtures._config(root, adapter="auto"), system=system,
    )


def test_two_valid_candidates_prefer_napcat_without_launching_host(tmp_path):
    system = Candidates()
    manager = supervisor(tmp_path, system)
    assert manager.evaluate().manifest["adapter"] == "napcat"
    assert manager.config["adapter"] == "napcat"
    assert "snowluma" not in system.seen
    assert system.launch_count == 0


def test_runtime_snapshot_publishes_only_selected_process_identity(tmp_path):
    system = Candidates()
    manager = supervisor(tmp_path, system)
    evaluation = manager.evaluate()
    active = SimpleNamespace(
        evaluation=evaluation, peer_snapshot_written=False,
        service_instance="12345678-1234-4123-8123-123456789abc",
        host_generation=1, connection_generation=2, directory=tmp_path / "generation",
    )
    active.directory.mkdir()
    manager._write_generation_identity(active)
    snapshot = json.loads((manager.runtime_dir / "adapter-runtime.private.json").read_text())
    assert snapshot["adapterBinding"] == {
        "adapter": "napcat", "pid": 8095, "creationTime100ns": "809500",
    }
    assert active.peer_snapshot_written is True


@pytest.mark.parametrize("failure", ["missing", "stale", "offline", "account", "malformed", "pid"])
def test_invalid_napcat_does_not_hide_verified_snowluma(tmp_path, failure):
    system = Candidates()
    napcat = system.manifests["napcat"]
    if failure == "missing":
        del system.manifests["napcat"]
    elif failure == "stale":
        napcat["updatedAtMs"] -= 10001
    elif failure == "offline":
        napcat.update(connected=False, accountId="")
    elif failure == "account":
        napcat["accountId"] = "99999"
    elif failure == "malformed":
        napcat["unexpected"] = True
    else:
        system.reject.add("napcat")
    manager = supervisor(tmp_path, system)
    assert manager.evaluate().manifest["adapter"] == "snowluma"
    assert system.launch_count == 0


def test_consumed_napcat_is_skipped_and_no_candidate_returns_exact_failure(tmp_path, monkeypatch):
    system = Candidates()
    manager = supervisor(tmp_path, system)
    monkeypatch.setattr(manager, "_kernel_quarantined", lambda evaluation: evaluation.manifest["adapter"] == "napcat")
    assert manager.evaluate().manifest["adapter"] == "snowluma"
    del system.manifests["snowluma"]
    with pytest.raises(ServiceError, match="fixed_shim_kernel_consumed"):
        manager.evaluate()
    assert manager.config["adapter"] == "snowluma"


def test_active_snowluma_is_pinned_even_if_napcat_appears(tmp_path):
    system = Candidates()
    napcat = system.manifests.pop("napcat")
    manager = supervisor(tmp_path, system)
    evaluation = manager.evaluate()
    manager.active = SimpleNamespace(evaluation=evaluation)
    system.manifests["napcat"] = napcat
    system.seen.clear()
    assert manager.evaluate().manifest["adapter"] == "snowluma"
    assert "napcat" not in system.seen
    system.manifests.pop("snowluma")
    with pytest.raises(ServiceError, match="adapter_not_connected"):
        manager.evaluate()
    assert "napcat" not in system.seen


def test_snowluma_discovery_uses_its_own_local_endpoint_and_keeps_token_private(tmp_path, monkeypatch):
    monkeypatch.setattr(snowluma, "_read_adapter_document", lambda _: {
        "snowluma": {"host": "127.0.0.1", "port": 3999, "path": "/ob", "token": "private-token"},
    })
    assert snowluma.configured_onebot_connection(tmp_path) == ("ws://127.0.0.1:3999/ob", "private-token")
    for host in ("example.com", "0.0.0.0"):
        monkeypatch.setattr(snowluma, "_read_adapter_document", lambda _, host=host: {
            "snowluma": {"host": host, "port": 3999, "token": "private-token"},
        })
        with pytest.raises(snowluma.SnowLumaBindingError, match="adapter_endpoint_mismatch") as exc:
            snowluma.configured_onebot_connection(tmp_path)
        assert "private-token" not in str(exc.value)


def test_napcat_discovers_only_configured_account_local_enabled_servers(tmp_path):
    folder = tmp_path / "config"
    folder.mkdir()
    document = {"network": {"httpServers": [
        {"enable": True, "host": "127.0.0.1", "port": 9999, "token": "private-token"},
        {"enable": True, "host": "127.0.0.1", "port": 5701, "token": ""},
        {"enable": False, "host": "127.0.0.1", "port": 8070},
        {"enable": True, "host": "example.com", "port": 3001},
    ]}}
    path = folder / "onebot11_12345.json"
    path.write_text(json.dumps(document))
    config = {"account_id": "12345", "paths": {"loader_root": str(tmp_path)},
              "onebot": {"url": "ws://127.0.0.1:3001/"}}
    assert configured_onebot_connections(config) == [
        ("http://127.0.0.1:9999/", "private-token"), ("http://127.0.0.1:5701/", ""),
    ]
    config["onebot"]["url"] = "http://127.0.0.1:5701"
    assert configured_onebot_connections(config)[0][0] == "http://127.0.0.1:5701/"
    config["account_id"] = "99999"
    with pytest.raises(ValueError, match="adapter_endpoint_mismatch"):
        configured_onebot_connections(config)
    config["account_id"] = "../12345"
    with pytest.raises(ValueError, match="adapter_endpoint_mismatch"):
        configured_onebot_connections(config)
