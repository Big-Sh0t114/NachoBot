"""Versioned SQLite persistence for Focus.

This package intentionally does not register models with the common Peewee
auto-sync path.  Migrations are owned and applied only by Focus.
"""

from .migrations import FOCUS_SCHEMA_VERSION, migrate_focus_database
from .repository import FocusSQLiteStorage, default_focus_database_path

__all__ = [
    "FOCUS_SCHEMA_VERSION",
    "FocusSQLiteStorage",
    "default_focus_database_path",
    "migrate_focus_database",
]
