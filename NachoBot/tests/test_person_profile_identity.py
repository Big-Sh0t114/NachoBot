from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INJECTOR_PATH = PROJECT_ROOT / "src" / "memory_system" / "person_profile_injector.py"


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None


def _load_injector(get_person_id: Mock, get_person_id_by_name: Mock):
    modules = {
        "src": types.ModuleType("src"),
        "src.common": types.ModuleType("src.common"),
        "src.common.logger": types.ModuleType("src.common.logger"),
        "src.config": types.ModuleType("src.config"),
        "src.config.config": types.ModuleType("src.config.config"),
        "src.person_info": types.ModuleType("src.person_info"),
        "src.person_info.person_info": types.ModuleType("src.person_info.person_info"),
        "src.A_memorix": types.ModuleType("src.A_memorix"),
        "src.A_memorix.core": types.ModuleType("src.A_memorix.core"),
        "src.A_memorix.core.utils": types.ModuleType("src.A_memorix.core.utils"),
        "src.A_memorix.core.utils.profile_text": types.ModuleType(
            "src.A_memorix.core.utils.profile_text"
        ),
    }
    for name in (
        "src",
        "src.common",
        "src.config",
        "src.person_info",
        "src.A_memorix",
        "src.A_memorix.core",
        "src.A_memorix.core.utils",
    ):
        modules[name].__path__ = []

    modules["src.common.logger"].get_logger = lambda _name: _Logger()
    modules["src.config.config"].global_config = types.SimpleNamespace(
        bot=types.SimpleNamespace(qq_account="bot-account"),
        chat=types.SimpleNamespace(person_profile_injection_enabled=True),
    )
    modules["src.person_info.person_info"].get_person_id = get_person_id
    modules["src.person_info.person_info"].get_person_id_by_person_name = get_person_id_by_name
    modules["src.A_memorix.core.utils.profile_text"].build_profile_injection_text = lambda text: text

    spec = importlib.util.spec_from_file_location("person_profile_injector_identity_test", INJECTOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
    return module


def _message(*, platform: str, user_id: str, name: str):
    return types.SimpleNamespace(
        chat_info=types.SimpleNamespace(platform=platform),
        user_info=types.SimpleNamespace(
            platform=platform,
            user_id=user_id,
            user_cardname=name,
            user_nickname=name,
        ),
    )


def test_collect_uses_platform_account_mapping_before_same_name_person():
    get_person_id = Mock(return_value="person-b")
    get_person_id_by_name = Mock(return_value="person-a")
    injector = _load_injector(get_person_id, get_person_id_by_name)

    candidates = injector.collect_person_candidates(
        [_message(platform="discord", user_id="account-b", name="Shared name")]
    )

    assert [candidate.person_id for candidate in candidates] == ["person-b"]
    get_person_id.assert_called_once_with("discord", "account-b")
    get_person_id_by_name.assert_not_called()


def test_collect_preserves_resolved_cross_platform_person_binding():
    get_person_id = Mock(return_value="merged-person-id")
    get_person_id_by_name = Mock(return_value="unrelated-name-match")
    injector = _load_injector(get_person_id, get_person_id_by_name)

    candidates = injector.collect_person_candidates(
        [_message(platform="matrix", user_id="bound-account", name="Bound person")]
    )

    assert [candidate.person_id for candidate in candidates] == ["merged-person-id"]
    get_person_id.assert_called_once_with("matrix", "bound-account")
    get_person_id_by_name.assert_not_called()


def test_collect_skips_incomplete_identity_instead_of_guessing_by_name():
    get_person_id = Mock(return_value="should-not-be-used")
    get_person_id_by_name = Mock(return_value="person-a")
    injector = _load_injector(get_person_id, get_person_id_by_name)

    candidates = injector.collect_person_candidates(
        [_message(platform="", user_id="", name="Shared name")]
    )

    assert candidates == []
    get_person_id.assert_not_called()
    get_person_id_by_name.assert_not_called()


def test_collect_does_not_fall_back_to_name_after_authoritative_lookup_failure():
    get_person_id = Mock(side_effect=RuntimeError("binding lookup failed"))
    get_person_id_by_name = Mock(return_value="person-a")
    injector = _load_injector(get_person_id, get_person_id_by_name)

    candidates = injector.collect_person_candidates(
        [_message(platform="discord", user_id="account-b", name="Shared name")]
    )

    assert candidates == []
    get_person_id.assert_called_once_with("discord", "account-b")
    get_person_id_by_name.assert_not_called()
