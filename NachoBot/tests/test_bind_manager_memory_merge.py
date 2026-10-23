from __future__ import annotations

import ast
import hashlib
import itertools
import json
import os
import random
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Union

import pytest
from peewee import BooleanField, FloatField, Model, SqliteDatabase, TextField


CORE_ROOT = Path(__file__).resolve().parents[1]
DB = SqliteDatabase(":memory:")


class BaseModel(Model):
    class Meta:
        database = DB


def _load_models():
    source_path = CORE_ROOT / "src" / "common" / "database" / "database_model.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted = {"PersonInfo", "PersonBinding", "BindRequest"}
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in wanted]
    if {node.name for node in classes} != wanted:
        raise RuntimeError("Could not find all person binding model classes")

    namespace = {
        "BaseModel": BaseModel,
        "BooleanField": BooleanField,
        "FloatField": FloatField,
        "TextField": TextField,
    }
    module = ast.Module(body=classes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["PersonInfo"], namespace["PersonBinding"], namespace["BindRequest"]


PersonInfo, PersonBinding, BindRequest = _load_models()
MODELS = (PersonInfo, PersonBinding, BindRequest)
DB.connect(reuse_if_open=True)
DB.create_tables(MODELS, safe=True)


def _quiet_logger():
    def noop(*_args, **_kwargs):
        return None

    return SimpleNamespace(info=noop, warning=noop, debug=noop, error=noop)


def _load_original_person_id():
    source_path = CORE_ROOT / "src" / "person_info" / "person_info.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_normalize_person_platform", "get_original_person_id"}
    ]
    namespace = {
        "PersonInfo": PersonInfo,
        "Union": Union,
        "hashlib": hashlib,
        "json": json,
        "logger": _quiet_logger(),
    }
    module = ast.Module(body=helpers, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["get_original_person_id"]


get_original_person_id = _load_original_person_id()


def _load_bind_manager():
    configured_source = os.environ.get("NACHOBOT_BIND_MANAGER_SOURCE")
    source_path = Path(configured_source) if configured_source else CORE_ROOT / "src" / "person_info" / "bind_manager.py"
    source_path = source_path.resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    manager_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BindManager")
    namespace = {
        "BindRequest": BindRequest,
        "MERGED_PLATFORM": "__merged__",
        "PersonBinding": PersonBinding,
        "PersonInfo": PersonInfo,
        "Union": Union,
        "db": DB,
        "get_original_person_id": get_original_person_id,
        "hashlib": hashlib,
        "json": json,
        "logger": _quiet_logger(),
        "random": random,
        "time": time,
    }
    module = ast.Module(body=[manager_class], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["BindManager"]


BindManager = _load_bind_manager()


@pytest.fixture(autouse=True)
def clean_database():
    with DB.atomic():
        for model in MODELS:
            model.delete().execute()
    yield


def _add_person(platform: str, user_id: str, memories: list[str | None], *, with_binding: bool = True) -> tuple[str, str]:
    user_id = str(user_id)
    person_id = get_original_person_id(platform, user_id)
    encoded_memories = json.dumps(memories, ensure_ascii=False)
    PersonInfo.create(
        person_id=person_id,
        platform=platform,
        user_id=user_id,
        memory_points=encoded_memories,
    )
    if with_binding:
        PersonBinding.create(person_id=person_id, platform=platform, platform_user_id=user_id)
    return person_id, encoded_memories


_request_codes = itertools.count(1)


def _confirm_bind(manager, requester_person_id: str, target_platform: str, target_user_id: str):
    code = f"bind-{next(_request_codes):05d}"
    BindRequest.create(
        auth_code=code,
        req_person_id=requester_person_id,
        target_platform=target_platform,
        target_user_id=str(target_user_id),
        expire_time=time.time() + 300,
        display_code=code,
    )
    return manager.confirm_bind(target_platform, str(target_user_id), code)


def _make_group(manager, first_person_id: str, second_platform: str, second_user_id: str):
    success, message = _confirm_bind(manager, first_person_id, second_platform, second_user_id)
    assert success, message
    merged = manager._get_merged_row_for_person(first_person_id)
    assert merged is not None
    return merged


def _memories(merged_row) -> list:
    return json.loads(merged_row.merged_memory)


def _assert_member_maps_point_to(group_id: str, member_ids: set[str]):
    rows = list(
        PersonBinding.select().where(
            (PersonBinding.platform != "__merged__") & (PersonBinding.person_id == group_id)
        )
    )
    assert len(rows) == len(member_ids)
    assert {get_original_person_id(row.platform, row.platform_user_id) for row in rows} == member_ids


def test_initial_two_person_binding_collects_original_memories():
    manager = BindManager()
    first_id, _ = _add_person("qq", "1", ["first memory", "shared"])
    second_id, _ = _add_person("discord", "2", ["second memory", "shared"])

    group = _make_group(manager, first_id, "discord", "2")

    assert set(_memories(group)) == {"first memory", "second memory", "shared"}
    assert set(group.merged_ids.split(",")) == {first_id, second_id}


def test_group_to_single_keeps_current_memory_and_ignores_stale_originals():
    manager = BindManager()
    first_id, first_original = _add_person("qq", "11", ["old first", "shared"])
    second_id, second_original = _add_person("discord", "12", ["old second", "shared"])
    group = _make_group(manager, first_id, "discord", "12")
    group.merged_memory = json.dumps(["current", "shared", "current", None], ensure_ascii=False)
    group.save()
    third_id, third_original = _add_person("telegram", "13", ["new member", "shared"], with_binding=False)

    success, message = _confirm_bind(manager, group.person_id, "telegram", "13")
    assert success, message
    merged = manager._get_merged_row_for_person(first_id)

    assert set(_memories(merged)) == {"current", "new member", "shared"}
    assert len(_memories(merged)) == 3
    assert PersonInfo.get(PersonInfo.person_id == first_id).memory_points == first_original
    assert PersonInfo.get(PersonInfo.person_id == second_id).memory_points == second_original
    assert PersonInfo.get(PersonInfo.person_id == third_id).memory_points == third_original


def test_single_to_group_keeps_group_memory_and_adds_single_original_memory():
    manager = BindManager()
    first_id, _ = _add_person("qq", "21", ["stale first"])
    _add_person("discord", "22", ["stale second"])
    group = _make_group(manager, first_id, "discord", "22")
    group.merged_memory = json.dumps(["group current"])
    group.save()
    single_id, single_original = _add_person("telegram", "23", ["single current", "shared"])

    success, message = _confirm_bind(manager, single_id, "qq", "21")
    assert success, message
    merged = manager._get_merged_row_for_person(single_id)

    assert set(_memories(merged)) == {"group current", "single current", "shared"}
    assert PersonInfo.get(PersonInfo.person_id == single_id).memory_points == single_original


def test_group_to_group_keeps_both_current_aggregates_without_stale_originals():
    manager = BindManager()
    first_id, _ = _add_person("qq", "31", ["stale a"])
    second_id, _ = _add_person("discord", "32", ["stale b"])
    first_group = _make_group(manager, first_id, "discord", "32")
    first_group.merged_memory = json.dumps(["current group one"])
    first_group.save()

    third_id, _ = _add_person("telegram", "33", ["stale c"])
    fourth_id, _ = _add_person("matrix", "34", ["stale d"])
    second_group = _make_group(manager, third_id, "matrix", "34")
    second_group.merged_memory = json.dumps(["current group two"])
    second_group.save()

    success, message = _confirm_bind(manager, first_group.person_id, "matrix", "34")
    assert success, message
    merged = manager._get_merged_row_for_person(first_id)

    assert set(_memories(merged)) == {"current group one", "current group two"}
    assert set(merged.merged_ids.split(",")) == {first_id, second_id, third_id, fourth_id}


def test_repeated_fourth_platform_expansion_uses_current_aggregate_and_not_member_originals():
    manager = BindManager()
    first_id, _ = _add_person("qq", "41", ["old first"])
    _add_person("discord", "42", ["old second"])
    group = _make_group(manager, first_id, "discord", "42")
    group.merged_memory = json.dumps(["current before third"])
    group.save()
    third_id, third_original = _add_person("telegram", "43", ["third original"], with_binding=False)

    success, message = _confirm_bind(manager, group.person_id, "telegram", "43")
    assert success, message
    expanded = manager._get_merged_row_for_person(first_id)
    assert set(_memories(expanded)) == {"current before third", "third original"}

    expanded.merged_memory = json.dumps(["replacement after third"])
    expanded.save()
    fourth_id, _ = _add_person("matrix", "44", ["fourth original"], with_binding=False)
    success, message = _confirm_bind(manager, expanded.person_id, "matrix", "44")
    assert success, message
    expanded_again = manager._get_merged_row_for_person(first_id)

    assert set(_memories(expanded_again)) == {"replacement after third", "fourth original"}
    assert "third original" not in _memories(expanded_again)
    assert third_id in expanded_again.merged_ids.split(",")
    assert fourth_id in expanded_again.merged_ids.split(",")
    assert PersonInfo.get(PersonInfo.person_id == third_id).memory_points == third_original


def test_empty_aggregate_memory_is_authoritative():
    manager = BindManager()
    first_id, _ = _add_person("qq", "51", ["stale first"])
    _add_person("discord", "52", ["stale second"])
    group = _make_group(manager, first_id, "discord", "52")
    group.merged_memory = "[]"
    group.save()
    _add_person("telegram", "53", ["only new member memory"], with_binding=False)

    success, message = _confirm_bind(manager, group.person_id, "telegram", "53")
    assert success, message
    merged = manager._get_merged_row_for_person(first_id)

    assert _memories(merged) == ["only new member memory"]


@pytest.mark.parametrize("invalid_memory", ["not-json", "{}", "null", None])
def test_invalid_aggregate_memory_fails_closed_and_rolls_back(invalid_memory):
    manager = BindManager()
    first_id, _ = _add_person("qq", "61", ["old first"])
    second_id, _ = _add_person("discord", "62", ["old second"])
    group = _make_group(manager, first_id, "discord", "62")
    group.merged_memory = invalid_memory
    group.save()
    _add_person("telegram", "63", ["new member"], with_binding=False)
    old_group_id = group.person_id
    old_merged_ids = group.merged_ids

    success, _message = _confirm_bind(manager, old_group_id, "telegram", "63")

    assert not success
    restored_group = PersonBinding.get(
        (PersonBinding.platform == "__merged__") & (PersonBinding.person_id == old_group_id)
    )
    assert restored_group.merged_memory == invalid_memory
    assert restored_group.merged_ids == old_merged_ids
    assert PersonBinding.get_or_none(
        (PersonBinding.platform == "telegram") & (PersonBinding.platform_user_id == "63")
    ) is None
    _assert_member_maps_point_to(old_group_id, {first_id, second_id})
    assert BindRequest.select().count() == 1


def test_valid_group_merge_with_corrupt_group_rolls_back_both_aggregates():
    manager = BindManager()
    first_id, _ = _add_person("qq", "71", ["stale a"])
    second_id, _ = _add_person("discord", "72", ["stale b"])
    valid_group = _make_group(manager, first_id, "discord", "72")
    valid_group.merged_memory = json.dumps(["valid current"])
    valid_group.save()

    third_id, _ = _add_person("telegram", "73", ["stale c"])
    fourth_id, _ = _add_person("matrix", "74", ["stale d"])
    corrupt_group = _make_group(manager, third_id, "matrix", "74")
    corrupt_group.merged_memory = "broken-json"
    corrupt_group.save()
    valid_group_id = valid_group.person_id
    corrupt_group_id = corrupt_group.person_id

    success, _message = _confirm_bind(manager, valid_group_id, "matrix", "74")

    assert not success
    restored_valid = PersonBinding.get(
        (PersonBinding.platform == "__merged__") & (PersonBinding.person_id == valid_group_id)
    )
    restored_corrupt = PersonBinding.get(
        (PersonBinding.platform == "__merged__") & (PersonBinding.person_id == corrupt_group_id)
    )
    assert restored_valid.merged_memory == json.dumps(["valid current"])
    assert restored_corrupt.merged_memory == "broken-json"
    assert _memories(restored_valid) == ["valid current"]
    _assert_member_maps_point_to(valid_group_id, {first_id, second_id})
    _assert_member_maps_point_to(corrupt_group_id, {third_id, fourth_id})
    assert BindRequest.select().count() == 1


def test_group_rebuild_failure_rolls_back_deletion_and_mapping_changes(monkeypatch):
    manager = BindManager()
    first_id, _ = _add_person("qq", "81", ["first"])
    second_id, _ = _add_person("discord", "82", ["second"])
    group = _make_group(manager, first_id, "discord", "82")
    group_id = group.person_id
    group_memory = group.merged_memory
    _add_person("telegram", "83", ["third"], with_binding=False)

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("forced group build failure")

    monkeypatch.setattr(manager, "_build_merged_group", fail_build)
    success, _message = _confirm_bind(manager, group_id, "telegram", "83")

    assert not success
    restored_group = PersonBinding.get(
        (PersonBinding.platform == "__merged__") & (PersonBinding.person_id == group_id)
    )
    assert restored_group.merged_memory == group_memory
    _assert_member_maps_point_to(group_id, {first_id, second_id})
    assert PersonBinding.get_or_none(
        (PersonBinding.platform == "telegram") & (PersonBinding.platform_user_id == "83")
    ) is None
    assert BindRequest.select().count() == 1
