"""Bounded diagnostic history containing only public status fields."""
from __future__ import annotations

from pathlib import Path
import re

from .private_files import PrivateFileError, atomic_private_json, is_reparse_point, read_private_json


LIMIT = 64


def _record(status: dict) -> dict:
    record = {key: status.get(key) for key in (
        "phase", "updatedAtMs", "supervisorPid", "supervisorCreationTime100ns",
    )}
    record["code"] = status.get("code", "")
    for key in ("phase", "code"):
        value = record[key]
        if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}|", value):
            raise ValueError("invalid_history")
    for key in ("updatedAtMs", "supervisorPid"):
        if type(record[key]) is not int or not 0 <= record[key] <= 9_007_199_254_740_991:
            raise ValueError("invalid_history")
    creation = record["supervisorCreationTime100ns"]
    if type(creation) is not str or not re.fullmatch(r"[0-9]{1,20}", creation):
        raise ValueError("invalid_history")
    return record


def _key(record: dict) -> tuple:
    return tuple(record[key] for key in ("phase", "code", "supervisorPid", "supervisorCreationTime100ns"))


class StatusHistory:
    def __init__(self, path: Path):
        self.path = path
        self.last_key = None

    def write(self, status: dict) -> None:
        record = _record(status)
        key = _key(record)
        if key == self.last_key:
            return
        if is_reparse_point(self.path):
            raise PrivateFileError("history_path_invalid")
        records = []
        try:
            previous = read_private_json(self.path, max_bytes=32768)
            if (type(previous) is dict and previous.get("schema") == 1
                    and type(previous.get("events")) is list and len(previous["events"]) <= LIMIT):
                records = [_record(event) for event in previous["events"] if type(event) is dict]
        except (PrivateFileError, ValueError):
            # Corrupt diagnostics never prevent service recovery. Preserve only
            # validated, bounded public fields; no raw exception or config text.
            pass
        if not records or _key(records[-1]) != key:
            records.append(record)
        atomic_private_json(self.path, {"schema": 1, "events": records[-LIMIT:]})
        self.last_key = key
