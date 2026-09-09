from __future__ import annotations

import sys
import unittest
from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from db_manager import DatabaseManager  # noqa: E402


class DatabaseLimitTests(unittest.TestCase):
    def test_query_rejects_unbounded_page_arguments_before_opening_database(self) -> None:
        manager = DatabaseManager()
        for page in (0, -1, 1_000_001, True):
            with self.subTest(page=page), self.assertRaises(ValueError):
                manager.query_table("anything", page=page)
        for size in (0, -1, 201, 10_000, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                manager.query_table("anything", page_size=size)

    def test_column_value_limit_is_bounded(self) -> None:
        manager = DatabaseManager()
        for limit in (0, -1, 201, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                manager.get_column_values("anything", "column", limit=limit)


if __name__ == "__main__":
    unittest.main()
