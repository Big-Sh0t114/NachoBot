"""Package ownership and cold-import checks for the extracted clients."""

from pathlib import Path
import hashlib
import os
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[2]


def test_message_library_has_no_platform_voice_modules_or_references():
    library = ROOT / "NachoBot" / "ncnk_message"
    for path in library.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert not any(term in text or term in path.name.lower() for term in (
            "qqcall", "universalvc", "universal_vc",
        )), path


def test_installed_clients_and_generic_messages_have_separate_import_boundaries():
    # No SDK paths in PYTHONPATH: exercise the installed local distributions.
    env = dict(os.environ, PYTHONPATH=str(ROOT / "NachoBot"))
    check = """
import importlib.util
import sys
import ncnk_message
assert not any(name.startswith(('qqcall_client', 'universalvc_client')) for name in sys.modules)
for name in ('qqcall', 'qqcall_service', 'universalvc_client'):
    assert importlib.util.find_spec('ncnk_message.' + name) is None
import qqcall_client.qqcall
import universalvc_client
forbidden = ('qqcall_pcm', 'universalvc_service', 'numpy', 'onnxruntime', 'sherpa_onnx', 'sounddevice', 'pyaudio', 'pyaudiowpatch')
assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules for prefix in forbidden)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", check], cwd=ROOT / "NachoBot",
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_extracted_service_keeps_the_canonical_workspace_paths():
    from qqcall_client import qqcall_service

    assert qqcall_service._REPO_ROOT == ROOT
    assert qqcall_service._SERVICE_PROJECT_ROOT == ROOT / "NachoBot-QQCall"
    assert qqcall_service.canonical_qqcall_config_path() == (
        ROOT / "NachoBot" / "plugins" / "qqcall_plugin" / "config.toml"
    )


def test_frozen_universal_client_keeps_its_provenance():
    sdk = ROOT / "NachoBot-UniversalVC-Adapter" / "sdk"
    provenance = dict(line.split("=", 1) for line in (
        sdk / "universalvc_client.provenance.txt"
    ).read_text(encoding="utf-8").splitlines())
    assert hashlib.sha256((sdk / "universalvc_client.py").read_bytes()).hexdigest() == provenance["sha256"]


def test_base_client_dependencies_do_not_install_pcm_or_models():
    for project in ("NachoBot-QQCall", "NachoBot-UniversalVC-Adapter"):
        document = tomllib.loads((ROOT / project / "sdk" / "pyproject.toml").read_text(encoding="utf-8"))
        deps = document["project"]["dependencies"]
        assert all(dep.startswith(("aiohttp", "nachobot-universalvc-client")) for dep in deps)
    for project in ("NachoBot", "NachoBot-Napcat-Adapter", "NachoBot-SnowLuma-Adapter"):
        document = tomllib.loads((ROOT / project / "pyproject.toml").read_text(encoding="utf-8"))
        assert "nachobot-qqcall-client" in document["project"]["dependencies"]
        for name in ("nachobot-qqcall-client", "nachobot-universalvc-client"):
            source = document["tool"]["uv"]["sources"][name]
            assert source["editable"] is True
            assert (ROOT / project / source["path"] / "pyproject.toml").is_file()
