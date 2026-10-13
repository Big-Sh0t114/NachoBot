from __future__ import annotations

import ast
import json
import unittest
from dataclasses import dataclass
from pathlib import Path

from ncnk_message.qq_history_identity import positive_qq_id, validate_qq_private_identity


class _Condition:
    def __and__(self, _other):
        return self


class _Field:
    def __eq__(self, _other):
        return _Condition()

    def is_null(self, _value):
        return _Condition()

    def desc(self):
        return self


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def where(self, _condition):
        return self

    def order_by(self, *_fields):
        return self

    def __iter__(self):
        return iter(self.rows)


class _Messages:
    message_id = _Field()
    chat_info_platform = _Field()
    chat_info_group_id = _Field()
    chat_info_user_id = _Field()
    time = _Field()
    id = _Field()
    rows = []

    @classmethod
    def select(cls):
        return _Query(cls.rows)


@dataclass(frozen=True)
class _StoredMessageRef:
    row_id: int
    chat_id: str
    message_id: str
    message_time: float


class _NullLogger:
    def exception(self, *_args, **_kwargs):
        return None


def _load_find_existing_ingress():
    source_path = Path(__file__).resolve().parents[1] / "src" / "chat" / "message_receive" / "storage.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    storage_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MessageStorage")
    function = next(
        node
        for node in storage_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "find_existing_ingress"
    )
    function.decorator_list = []
    ast.fix_missing_locations(function)
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "json": json,
        "Messages": _Messages,
        "StoredMessageRef": _StoredMessageRef,
        "logger": _NullLogger(),
        "positive_qq_id": positive_qq_id,
        "validate_qq_private_identity": validate_qq_private_identity,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["find_existing_ingress"]


find_existing_ingress = _load_find_existing_ingress()


def _delivery(account_id: str = "account-A") -> dict:
    return {
        "backend": "napcat",
        "account_id": account_id,
        "platform": "qq",
        "session_kind": "group",
        "session_id": "group-1",
        "message_id": "same-platform-message-id",
        "delivery_key": f"key-{account_id}",
    }


def _row(row_id: int, config: dict | None):
    return type(
        "Row",
        (),
        {
            "id": row_id,
            "chat_id": "qq:group:group-1",
            "message_id": "same-platform-message-id",
            "time": 100.25,
            "additional_config": json.dumps(config) if config is not None else None,
        },
    )()


class ExistingIngressScopeTests(unittest.TestCase):
    def test_explicit_different_account_row_is_not_reused_for_same_route_and_message_id(self):
        _Messages.rows = [
            _row(2, None),
            _row(
                1,
                {
                    "offline_recovery": {
                        "delivery": {
                            **_delivery("account-B"),
                        }
                    }
                },
            )
        ]
        self.assertIsNone(find_existing_ingress(_delivery("account-A")))

    def test_exact_scoped_row_is_reused_and_untagged_legacy_row_remains_compatible(self):
        exact = _row(2, {"offline_recovery": {"delivery": _delivery("account-A")}})
        _Messages.rows = [
            _row(1, {"offline_recovery": {"delivery": _delivery("account-B")}}),
            exact,
        ]
        self.assertEqual(find_existing_ingress(_delivery("account-A")).row_id, 2)

        _Messages.rows = [_row(3, None)]
        self.assertEqual(find_existing_ingress(_delivery("account-A")).row_id, 3)


if __name__ == "__main__":
    unittest.main()
