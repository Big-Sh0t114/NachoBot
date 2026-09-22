from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import setup_deployment as deployment


@pytest.fixture
def desired_ws_entry() -> dict[str, object]:
    return {
        "enable": True,
        "name": "NachoBot",
        "url": "ws://localhost:8095",
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
    }


def test_fresh_config_adds_only_core_websocket_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desired_ws_entry: dict[str, object],
) -> None:
    napcat_root = tmp_path / "napcat"
    napcat_root.mkdir()
    monkeypatch.setattr(
        deployment.NapCatConfigurator,
        "_load_adapter_ws_entry",
        staticmethod(lambda: dict(desired_ws_entry)),
    )

    result = deployment.NapCatConfigurator.configure(str(napcat_root), "123456")

    assert result["errors"] == []
    config_path = napcat_root / "config" / "onebot11_123456.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["network"]["websocketClients"] == [desired_ws_entry]
    assert document["network"]["httpServers"] == []


def test_existing_http_servers_are_preserved_structurally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desired_ws_entry: dict[str, object],
) -> None:
    napcat_root = tmp_path / "napcat"
    config_dir = napcat_root / "config"
    config_dir.mkdir(parents=True)
    http_servers = [
        {
            "name": "user-service",
            "enable": True,
            "port": 5800,
            "token": "user-owned-token",
            "headers": {"X-Owner": "operator"},
        },
        {"name": "legacy", "enable": False, "path": "/hook"},
    ]
    config_path = config_dir / "onebot11_123456.json"
    config_path.write_text(
        json.dumps(
            {
                "network": {
                    "websocketClients": [],
                    "httpServers": http_servers,
                },
                "operatorSetting": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        deployment.NapCatConfigurator,
        "_load_adapter_ws_entry",
        staticmethod(lambda: dict(desired_ws_entry)),
    )
    monkeypatch.setattr(
        deployment.BackupManager,
        "backup",
        staticmethod(lambda _path: None),
    )

    result = deployment.NapCatConfigurator.configure(str(napcat_root), "123456")

    assert result["errors"] == []
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["network"]["httpServers"] == http_servers
    assert document["operatorSetting"] == {"keep": True}
    assert document["network"]["websocketClients"] == [desired_ws_entry]
