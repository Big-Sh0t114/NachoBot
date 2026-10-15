from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

import db_manager  # noqa: E402
from db_manager import DatabaseManager, TRUNCATE_LENGTH  # noqa: E402


class DatabaseDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "details.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE focus_chat_cursor ("
            "chat_id TEXT NOT NULL, cursor_name TEXT NOT NULL, context TEXT, "
            "PRIMARY KEY (chat_id, cursor_name))"
        )
        self.long_value = "complete-value-" + ("x" * (TRUNCATE_LENGTH + 40))
        conn.execute(
            "INSERT INTO focus_chat_cursor VALUES (?, ?, ?)",
            ("chat/one", "latest", self.long_value),
        )
        conn.execute(
            "CREATE TABLE legacy_records (name TEXT, payload TEXT)"
        )
        conn.execute(
            "INSERT INTO legacy_records VALUES (?, ?)",
            ("no-key", "still browseable"),
        )
        conn.commit()
        conn.close()
        self.db_path_patch = patch.object(db_manager, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        self.addCleanup(self.db_path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.manager = DatabaseManager()

    def test_composite_primary_key_locator_returns_untruncated_row(self) -> None:
        result = self.manager.query_table("focus_chat_cursor")

        self.assertEqual(result["columns"][0]["name"], "chat_id")
        self.assertEqual(result["columns"][1]["name"], "cursor_name")
        self.assertNotIn(result["row_locator_field"], {column["name"] for column in result["columns"]})
        listed = result["data"][0]
        self.assertEqual(
            listed[result["row_locator_field"]],
            {"chat_id": "chat/one", "cursor_name": "latest"},
        )
        self.assertEqual(len(listed["context"]), TRUNCATE_LENGTH + 3)
        self.assertTrue(listed["context"].endswith("..."))

        detail = self.manager.get_row_by_primary_key(
            "focus_chat_cursor",
            listed[result["row_locator_field"]],
        )
        self.assertEqual(detail["data"]["context"], self.long_value)
        self.assertEqual(
            [column["name"] for column in detail["columns"]],
            ["chat_id", "cursor_name", "context"],
        )
        self.assertFalse(detail["editable"])

    def test_locator_rejects_missing_extra_malformed_unknown_and_missing_row(self) -> None:
        invalid_mappings = (
            {"chat_id": "chat/one"},
            {"chat_id": "chat/one", "cursor_name": "latest", "other": "x"},
            {"chat_id": {"predicate": "x"}, "cursor_name": "latest"},
        )
        for mapping in invalid_mappings:
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.manager.get_row_by_primary_key("focus_chat_cursor", mapping)

        with self.assertRaisesRegex(ValueError, "Table not found"):
            self.manager.get_row_by_primary_key("unknown_table", {"id": 1})
        with self.assertRaisesRegex(ValueError, "Row not found"):
            self.manager.get_row_by_primary_key(
                "focus_chat_cursor",
                {"chat_id": "missing", "cursor_name": "latest"},
            )

    def test_table_without_primary_key_stays_listable_without_a_locator(self) -> None:
        result = self.manager.query_table("legacy_records")
        self.assertEqual(result["data"][0]["name"], "no-key")
        self.assertIsNone(result["data"][0][result["row_locator_field"]])
        with self.assertRaisesRegex(ValueError, "no declared primary key"):
            self.manager.get_row_by_primary_key("legacy_records", {})


if __name__ == "__main__":
    unittest.main()
