"""Private atomic state files and a cross-process service lock."""

from __future__ import annotations

import csv
import errno
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


class PrivateFileError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _retryable_windows_file_error(error: OSError) -> bool:
    """Windows sharing/access failures can occur during atomic replacement."""
    if os.name != "nt":
        return False
    native = getattr(error, "winerror", None)
    return native in {5, 32, 33} or (
        native is None and error.errno in {errno.EACCES, errno.EPERM}
    )


def read_bounded_bytes(path: str | os.PathLike[str], maximum: int) -> bytes:
    """Read one bounded snapshot; never retry missing files or invalid JSON."""
    for attempt in range(5):
        try:
            with Path(path).open("rb") as stream:
                return stream.read(maximum + 1)
        except OSError as exc:
            if attempt == 4 or not _retryable_windows_file_error(exc):
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def _replace_private_file(source: Path, destination: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if attempt == 4 or not _retryable_windows_file_error(exc):
                raise
            time.sleep(0.02)


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """Detect symlinks and Windows junction/other reparse-point directories."""
    target = Path(path)
    try:
        if target.is_symlink():
            return True
        is_junction = getattr(target, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(target.lstat(), "st_file_attributes", 0)
        return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except FileNotFoundError:
        return False
    except OSError:
        raise PrivateFileError("private_directory_invalid") from None


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateFileError("private_json_duplicate_key")
        result[key] = value
    return result


def reject_constant(_value: str) -> None:
    raise PrivateFileError("private_json_nonfinite")


def read_private_json(path: str | os.PathLike[str], *, max_bytes: int = 64 * 1024) -> Any:
    try:
        raw = read_bounded_bytes(path, max_bytes)
    except OSError:
        raise PrivateFileError("private_file_unavailable") from None
    if len(raw) > max_bytes:
        raise PrivateFileError("private_file_too_large")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except PrivateFileError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise PrivateFileError("private_json_invalid") from None


def _windows_user_sid() -> str:
    return_code = 1
    sid = ""
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
            encoding="utf-8",
            errors="strict",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return_code = result.returncode
        row = next(csv.reader(result.stdout.splitlines()), [])
        sid = row[-1].strip() if row else ""
    except (OSError, subprocess.TimeoutExpired, UnicodeError, csv.Error):
        sid = ""
    if return_code != 0 or not sid.startswith("S-1-") or len(sid) > 184:
        raise PrivateFileError("private_acl_unavailable")
    return sid


def _icacls(path: Path, *, directory: bool) -> None:
    sid = _windows_user_sid()
    permissions = "(OI)(CI)F" if directory else "F"
    try:
        result = subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"*{sid}:{permissions}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PrivateFileError("private_acl_unavailable") from None
    if result.returncode != 0:
        raise PrivateFileError("private_acl_unavailable")


def secure_directory(path: str | os.PathLike[str]) -> Path:
    target = Path(os.path.abspath(path))
    try:
        current = target
        while True:
            if is_reparse_point(current):
                raise PrivateFileError("private_directory_invalid")
            if current.parent == current:
                break
            current = current.parent
        target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            raise PrivateFileError("private_directory_invalid")
        if os.name == "nt":
            _icacls(target, directory=True)
        else:
            target.chmod(0o700)
    except PrivateFileError:
        raise
    except OSError:
        raise PrivateFileError("private_directory_unavailable") from None
    return target


def secure_file(path: str | os.PathLike[str]) -> None:
    target = Path(path)
    try:
        if target.is_symlink() or not target.is_file():
            raise PrivateFileError("private_file_invalid")
        if os.name == "nt":
            _icacls(target, directory=False)
        else:
            target.chmod(0o600)
    except PrivateFileError:
        raise
    except OSError:
        raise PrivateFileError("private_file_unavailable") from None


def atomic_private_json(path: str | os.PathLike[str], value: Any) -> None:
    destination = Path(path)
    secure_directory(destination.parent)
    try:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            _replace_private_file(temporary, destination)
            secure_file(destination)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except PrivateFileError:
        raise
    except (OSError, TypeError, ValueError):
        raise PrivateFileError("private_write_failed") from None


def atomic_private_json_if_absent(path: str | os.PathLike[str], value: Any) -> bool:
    """Atomically publish a private JSON file without replacing an existing name."""
    destination = Path(path)
    secure_directory(destination.parent)
    temporary: Path | None = None
    try:
        encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
        temporary = Path(temporary_name)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and create-only on NTFS and the
            # local filesystems used by the test fixtures.
            os.link(temporary, destination)
        except FileExistsError:
            return False
        if os.name != "nt":
            destination.chmod(0o600)
        return True
    except PrivateFileError:
        raise
    except OSError:
        raise PrivateFileError("private_write_failed") from None
    except (TypeError, ValueError):
        raise PrivateFileError("private_write_failed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def read_or_create_private_token(path: str | os.PathLike[str]) -> str:
    target = Path(path)
    secure_directory(target.parent)
    try:
        with target.open("x", encoding="ascii", newline="") as stream:
            token = secrets.token_urlsafe(32)
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        secure_file(target)
        return token
    except FileExistsError:
        try:
            if target.is_symlink() or not target.is_file() or target.stat().st_size > 512:
                raise PrivateFileError("control_token_invalid")
            token = target.read_text(encoding="ascii").strip()
        except PrivateFileError:
            raise
        except (OSError, UnicodeError):
            raise PrivateFileError("control_token_invalid") from None
        if not 43 <= len(token) <= 128 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in token):
            raise PrivateFileError("control_token_invalid")
        secure_file(target)
        return token
    except PrivateFileError:
        raise
    except (OSError, UnicodeError):
        raise PrivateFileError("control_token_unavailable") from None


class ProcessLock:
    """Nonblocking single-instance file lock held for the manager lifetime."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._stream = None
        self._locked = False

    def acquire(self) -> bool:
        secure_directory(self.path.parent)
        try:
            if self.path.is_symlink():
                raise PrivateFileError("process_lock_unavailable")
            self._stream = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    self._stream.close()
                    self._stream = None
                    return False
            else:
                import fcntl

                try:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    self._stream.close()
                    self._stream = None
                    return False
            self._locked = True
            secure_file(self.path)
            return True
        except PrivateFileError:
            self.release()
            raise
        except OSError:
            self.release()
            raise PrivateFileError("process_lock_unavailable") from None

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._stream.close()
        except OSError:
            pass
        self._stream = None
        self._locked = False

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise PrivateFileError("service_already_running")
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> None:
        self.release()


__all__ = [
    "PrivateFileError", "ProcessLock", "atomic_private_json", "read_or_create_private_token",
    "read_private_json", "secure_directory", "secure_file", "unique_object",
    "read_bounded_bytes",
]
