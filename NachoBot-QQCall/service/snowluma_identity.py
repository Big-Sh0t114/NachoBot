"""SnowLuma-only cached UID compatibility for the QQCall OneBot client.

SnowLuma's OneBot identity response does not expose its native UID.  This
client fills a missing UID from SnowLuma's existing identity database after a
successful, exact-UIN OneBot response.  The database is a cache: this module
does not claim the UID was freshly verified by the RPC.
"""

from __future__ import annotations

import importlib
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

from service.rpc import OneBotClient, OneBotError

_UIN = re.compile(r"^[1-9][0-9]{0,19}$")
_UID = re.compile(r"^u_[A-Za-z0-9_-]{22}$")
_UID_FIELDS = ("uid", "user_uid", "userUid", "peer_uid")
_UIN_FIELDS = ("user_id", "self_id", "account_id")
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_REPARSE_POINT_ATTRIBUTE = 0x0400
_PROGRESS_OPS = 1000
_LOCATOR_IMPORT_LOCK = threading.Lock()
# SnowLuma appends these documented virtual contacts to its friend-list action.
# They are removable only by their exact friend-row metadata, never by name alone.
_DATALINE_VIRTUAL_FRIENDS = {
    "2113929217": "我的电脑",
    "2113929218": "我的手机",
    "2113929219": "我的Pad",
}
_DATALINE_FRIEND_FIELDS = frozenset({"user_id", "nickname", "remark"})


class SnowLumaIdentityError(OneBotError):
    """A stable, sanitized failure while reading cached SnowLuma identity."""

    _CODES = frozenset({
        "snowluma_identity_timeout",
        "snowluma_identity_runtime_unavailable",
        "snowluma_identity_runtime_changed",
        "snowluma_identity_cache_unavailable",
        "snowluma_identity_cache_invalid",
        "snowluma_identity_cache_changed",
        "snowluma_identity_uid_unavailable",
        "snowluma_identity_uid_invalid",
        "snowluma_identity_uid_conflict",
        "snowluma_identity_ambiguous",
        "snowluma_identity_uin_invalid",
        "snowluma_identity_response_invalid",
    })

    def __init__(self, code: str):
        super().__init__(code if code in self._CODES else "snowluma_identity_cache_unavailable")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SnowLumaIdentityError("snowluma_identity_timeout")
    return remaining


def _canonical_uin(value: Any) -> str | None:
    if type(value) is str and _UIN.fullmatch(value):
        return value
    if type(value) is int and value > 0:
        value_text = str(value)
        return value_text if _UIN.fullmatch(value_text) else None
    return None


def _strict_response_uin(data: dict[str, Any]) -> str | None:
    present = [key for key in _UIN_FIELDS if key in data]
    if not present:
        return None
    values = [_canonical_uin(data[key]) for key in present]
    if any(value is None for value in values) or len(set(values)) != 1:
        return None
    return values[0]


def _existing_uid(data: dict[str, Any]) -> str | None:
    present = [key for key in _UID_FIELDS if key in data]
    if not present:
        return None
    values = [data[key] for key in present]
    if any(type(value) is not str or _UID.fullmatch(value) is None for value in values):
        raise SnowLumaIdentityError("snowluma_identity_uid_invalid")
    if len(set(values)) != 1:
        raise SnowLumaIdentityError("snowluma_identity_uid_conflict")
    return values[0]


def _is_dataline_virtual_friend(data: Any) -> bool:
    if type(data) is not dict or set(data) != _DATALINE_FRIEND_FIELDS:
        return False
    uin = _canonical_uin(data["user_id"])
    expected_name = _DATALINE_VIRTUAL_FRIENDS.get(uin) if uin is not None else None
    return (
        expected_name is not None
        and type(data["nickname"]) is str
        and type(data["remark"]) is str
        and data["nickname"] == expected_name
        and data["remark"] == expected_name
    )


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        raise SnowLumaIdentityError("snowluma_identity_cache_unavailable") from None
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _file_identity(path: Path, *, directory: bool, deadline: float) -> tuple[int, int, int]:
    _remaining(deadline)
    if _is_reparse_or_symlink(path):
        raise SnowLumaIdentityError("snowluma_identity_cache_invalid")
    try:
        metadata = path.lstat()
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode) or metadata.st_ino <= 0:
            raise SnowLumaIdentityError("snowluma_identity_cache_invalid")
        resolved = path.resolve(strict=True)
        if resolved != path:
            raise SnowLumaIdentityError("snowluma_identity_cache_changed")
    except SnowLumaIdentityError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise SnowLumaIdentityError("snowluma_identity_cache_unavailable") from None
    _remaining(deadline)
    return (int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_mode))


def _path_components(path: Path) -> list[Path]:
    components: list[Path] = []
    current = path
    while True:
        components.append(current)
        if current.parent == current:
            break
        current = current.parent
    return list(reversed(components))


class SnowLumaOneBotClient(OneBotClient):
    """OneBot client that fills only missing UIDs from SnowLuma's local cache."""

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        account_id: str,
        project_root: str | os.PathLike[str],
        timeout: float = 3.0,
    ):
        super().__init__(url, token, timeout=timeout)
        if _canonical_uin(account_id) != account_id:
            raise SnowLumaIdentityError("snowluma_identity_uin_invalid")
        self._account_id = account_id
        self._project_root = Path(project_root)
        self._resolved_root: Path | None = None
        self._root_identity: tuple[int, int, int] | None = None
        self._runtime_path: Path | None = None
        self._runtime_identity: tuple[int, int, int] | None = None
        self._data_identity: tuple[int, int, int] | None = None
        self._account_identity: tuple[int, int, int] | None = None
        self._database_path: Path | None = None
        self._database_identity: tuple[int, int, int] | None = None
        self._state_lock = threading.Lock()

    def resolve_self_uid(self, *, timeout: float | None = None) -> str:
        """Read this authenticated account's cached self UID without an RPC.

        The result is the existing local identity mapping, not a fresh remote
        UID assertion. The account is fixed at client construction so callers
        cannot redirect this lookup to an arbitrary peer.
        """
        if timeout is None:
            effective_timeout = self._timeout
        elif (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > 30
        ):
            raise OneBotError("onebot_timeout_invalid")
        else:
            effective_timeout = min(self._timeout, float(timeout))

        deadline = time.monotonic() + effective_timeout
        runtime_path = self._current_runtime(deadline)
        return self._cached_uid(self._account_id, runtime_path, deadline)

    def call(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        # Preserve the base client's complete behavior for every other action.
        if action != "get_stranger_info":
            return super().call(action, params, timeout=timeout)

        if timeout is None:
            effective_timeout = self._timeout
        elif (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > 30
        ):
            return super().call(action, params, timeout=timeout)
        else:
            effective_timeout = min(self._timeout, float(timeout))
        deadline = time.monotonic() + effective_timeout
        data = super().call(action, params, timeout=_remaining(deadline))
        requested_uin = _canonical_uin(params.get("user_id")) if type(params) is dict else None
        if requested_uin is None or _strict_response_uin(data) != requested_uin:
            return data

        existing_uid = _existing_uid(data)
        if existing_uid is not None:
            return data
        # Include runtime resolution, the network request, and cache lookup in
        # this one monotonic budget. Resolve only when the response needs cache
        # compatibility, leaving ordinary OneBot identity untouched.
        runtime_path = self._current_runtime(deadline)
        uid = self._cached_uid(requested_uin, runtime_path, deadline)
        return {**data, "uid": uid}

    def get_friend_list(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        friends = super().get_friend_list(timeout=timeout)
        return [friend for friend in friends if not _is_dataline_virtual_friend(friend)]

    def _trusted_resolver(self, root: Path, deadline: float):
        if not _LOCATOR_IMPORT_LOCK.acquire(timeout=_remaining(deadline)):
            raise SnowLumaIdentityError("snowluma_identity_timeout")
        root_text = str(root)
        old_index = sys.path.index(root_text) if root_text in sys.path else None
        if old_index is not None:
            sys.path.pop(old_index)
        sys.path.insert(0, root_text)
        try:
            module = importlib.import_module("webUI.snowluma_locator")
            module_path = Path(module.__file__).resolve(strict=True)
            expected_module = (root / "webUI" / "snowluma_locator.py").resolve(strict=True)
            if module_path != expected_module:
                raise SnowLumaIdentityError("snowluma_identity_runtime_unavailable")
            resolver = getattr(module, "resolve_snowluma_runtime", None)
            if not callable(resolver):
                raise SnowLumaIdentityError("snowluma_identity_runtime_unavailable")
            return resolver
        except SnowLumaIdentityError:
            raise
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError):
            raise SnowLumaIdentityError("snowluma_identity_runtime_unavailable") from None
        finally:
            try:
                sys.path.remove(root_text)
            except ValueError:
                pass
            if old_index is not None:
                sys.path.insert(min(old_index, len(sys.path)), root_text)
            _LOCATOR_IMPORT_LOCK.release()

    def _current_runtime(self, deadline: float) -> Path:
        try:
            root = self._project_root.resolve(strict=True)
            _remaining(deadline)
            if self._resolved_root is not None and root != self._resolved_root:
                raise SnowLumaIdentityError("snowluma_identity_runtime_changed")
            root_identity = _file_identity(root, directory=True, deadline=deadline)
            resolver = self._trusted_resolver(root, deadline)
            selected = resolver(root)
            _remaining(deadline)
            runtime_path = Path(selected.path).resolve(strict=True)
            runtime_path.relative_to(root)
            root_parts = _path_components(root)
            runtime_parts = _path_components(runtime_path)
            for component in (*root_parts, *runtime_parts[len(root_parts):]):
                _remaining(deadline)
                if _is_reparse_or_symlink(component):
                    raise SnowLumaIdentityError("snowluma_identity_runtime_changed")
            runtime_identity = _file_identity(runtime_path, directory=True, deadline=deadline)
        except SnowLumaIdentityError:
            raise
        except (OSError, RuntimeError, ValueError, AttributeError, TypeError):
            raise SnowLumaIdentityError("snowluma_identity_runtime_unavailable") from None

        if not self._state_lock.acquire(timeout=_remaining(deadline)):
            raise SnowLumaIdentityError("snowluma_identity_timeout")
        try:
            if self._resolved_root is None:
                self._resolved_root = root
                self._root_identity = root_identity
                self._runtime_path = runtime_path
                self._runtime_identity = runtime_identity
            elif (
                root_identity != self._root_identity
                or runtime_path != self._runtime_path
                or runtime_identity != self._runtime_identity
            ):
                raise SnowLumaIdentityError("snowluma_identity_runtime_changed")
        finally:
            self._state_lock.release()
        return runtime_path

    def _cached_uid(self, requested_uin: str, runtime_path: Path, deadline: float) -> str:
        try:
            numeric_uin = int(requested_uin)
        except (TypeError, ValueError, OverflowError):
            raise SnowLumaIdentityError("snowluma_identity_uin_invalid") from None
        if numeric_uin <= 0 or numeric_uin > _MAX_SQLITE_INTEGER:
            raise SnowLumaIdentityError("snowluma_identity_uin_invalid")

        # SnowLuma stores this identity map under the authenticated bot
        # account.  requested_uin is only the key into that shared account
        # cache; using it as a directory would switch to a per-peer cache.
        database_path = runtime_path / "data" / self._account_id / "snowluma_identity.db"
        try:
            resolved_database = database_path.resolve(strict=True)
            resolved_database.relative_to(runtime_path)
            data_path = runtime_path / "data"
            account_path = data_path / self._account_id
            components = (data_path, account_path, database_path)
            for component in components:
                _remaining(deadline)
                if _is_reparse_or_symlink(component):
                    raise SnowLumaIdentityError("snowluma_identity_cache_invalid")

            data_identity = _file_identity(data_path, directory=True, deadline=deadline)
            account_identity = _file_identity(account_path, directory=True, deadline=deadline)
            database_identity = _file_identity(database_path, directory=False, deadline=deadline)
        except SnowLumaIdentityError as exc:
            if exc.code != "snowluma_identity_timeout":
                self._pin_unavailable_database_path(database_path, deadline)
            raise
        except (OSError, RuntimeError, ValueError):
            self._pin_unavailable_database_path(database_path, deadline)
            raise SnowLumaIdentityError("snowluma_identity_cache_unavailable") from None
        if not self._state_lock.acquire(timeout=_remaining(deadline)):
            raise SnowLumaIdentityError("snowluma_identity_timeout")
        try:
            if self._database_path is None:
                self._database_path = database_path
                self._data_identity = data_identity
                self._account_identity = account_identity
                self._database_identity = database_identity
            elif (
                database_path != self._database_path
                or self._database_identity is None
                or data_identity != self._data_identity
                or account_identity != self._account_identity
                or database_identity != self._database_identity
            ):
                raise SnowLumaIdentityError("snowluma_identity_cache_changed")
        finally:
            self._state_lock.release()

        _remaining(deadline)
        connection: sqlite3.Connection | None = None
        try:
            uri = database_path.as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=_remaining(deadline))

            def interrupt_if_expired() -> int:
                return int(time.monotonic() >= deadline)

            connection.set_progress_handler(interrupt_if_expired, _PROGRESS_OPS)
            rows = connection.execute(
                "SELECT uid, uin FROM users WHERE uin = ? LIMIT 2", (numeric_uin,),
            ).fetchmany(2)
            _remaining(deadline)
            if len(rows) != 1:
                code = "snowluma_identity_ambiguous" if len(rows) > 1 else "snowluma_identity_uid_unavailable"
                raise SnowLumaIdentityError(code)
            raw_uid, raw_uin = rows[0]
            if type(raw_uin) is not int or raw_uin != numeric_uin:
                raise SnowLumaIdentityError("snowluma_identity_cache_invalid")
            if type(raw_uid) is not str or _UID.fullmatch(raw_uid) is None:
                raise SnowLumaIdentityError("snowluma_identity_uid_invalid")

            inverse = connection.execute(
                "SELECT uin FROM users WHERE uid = ? LIMIT 2", (raw_uid,),
            ).fetchmany(2)
            _remaining(deadline)
            if len(inverse) != 1:
                raise SnowLumaIdentityError("snowluma_identity_ambiguous")
            if type(inverse[0][0]) is not int or inverse[0][0] != numeric_uin:
                raise SnowLumaIdentityError("snowluma_identity_ambiguous")
        except SnowLumaIdentityError:
            raise
        except sqlite3.Error as exc:
            if time.monotonic() >= deadline or "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise SnowLumaIdentityError("snowluma_identity_timeout") from None
            raise SnowLumaIdentityError("snowluma_identity_cache_invalid") from None
        except (OSError, OverflowError, ValueError, TypeError):
            raise SnowLumaIdentityError("snowluma_identity_cache_unavailable") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

        _remaining(deadline)
        final_runtime = self._current_runtime(deadline)
        if final_runtime != runtime_path:
            raise SnowLumaIdentityError("snowluma_identity_runtime_changed")
        final_data_identity = _file_identity(data_path, directory=True, deadline=deadline)
        final_account_identity = _file_identity(account_path, directory=True, deadline=deadline)
        final_database_identity = _file_identity(database_path, directory=False, deadline=deadline)
        if (
            final_data_identity != data_identity
            or final_account_identity != account_identity
            or final_database_identity != database_identity
        ):
            raise SnowLumaIdentityError("snowluma_identity_cache_changed")
        return raw_uid

    def _pin_unavailable_database_path(self, database_path: Path, deadline: float) -> None:
        if not self._state_lock.acquire(timeout=_remaining(deadline)):
            raise SnowLumaIdentityError("snowluma_identity_timeout")
        try:
            if self._database_path is None:
                self._database_path = database_path
                self._data_identity = None
                self._account_identity = None
                self._database_identity = None
            elif self._database_path != database_path:
                raise SnowLumaIdentityError("snowluma_identity_cache_changed")
        finally:
            self._state_lock.release()


__all__ = ["SnowLumaIdentityError", "SnowLumaOneBotClient"]
