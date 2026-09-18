"""Secure, request-local persistence for the SnowLuma WebUI password.

The deployed SnowLuma WebUI stores only a password hash, so NachoBot cannot
recover a password from its ``config/webui.json``.  On Windows this module
uses the CurrentUser DPAPI boundary exposed by ``crypt32.dll``.  The only
bytes written to disk are the DPAPI ciphertext; unsupported platforms and
all DPAPI/file failures fail closed without replacing an existing ciphertext.

The protector callables are intentionally injectable for tests.  Production
uses only ``ctypes`` and the Python standard library.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable


SECRET_RELATIVE_PATH = Path(".runtime") / "secrets" / "snowluma_webui_password.dpapi"
MAX_CIPHERTEXT_BYTES = 1_048_576


class SnowLumaSecretStoreUnavailable(RuntimeError):
    """The local password store cannot safely read or replace its value."""

    code = "SECRET_STORE_UNAVAILABLE"


def _root(value: Path | str | None) -> Path:
    # Keep this module independent from snowluma_manager to avoid import
    # cycles.  Callers normally pass the configured repository root.
    return Path(value or Path(__file__).resolve().parent.parent).resolve()


def secret_path(root: Path | str | None = None) -> Path:
    """Return the private store path without exposing it through API results."""

    return _root(root) / SECRET_RELATIVE_PATH


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
    if not data:
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        source = ctypes.create_string_buffer(data)
        input_blob = _DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
        output_blob = _DATA_BLOB()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if not crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        ):
            raise OSError
        try:
            if not output_blob.pbData or not output_blob.cbData:
                raise OSError
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)
    except Exception as exc:
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable") from exc


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
    if not data:
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        source = ctypes.create_string_buffer(data)
        input_blob = _DATA_BLOB(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
        output_blob = _DATA_BLOB()
        flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
        if not crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        ):
            raise OSError
        try:
            if not output_blob.pbData or not output_blob.cbData:
                raise OSError
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(output_blob.pbData)
    except Exception as exc:
        raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable") from exc


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace *path* atomically; the temporary file contains ciphertext only."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


class SnowLumaPasswordStore:
    """CurrentUser-DPAPI backed password store with a test-injectable boundary."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        protector: object | None = None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
    ) -> None:
        self.root = _root(root)
        self.path = secret_path(self.root)
        if protector is not None:
            protect = protect or getattr(protector, "protect", None)
            unprotect = unprotect or getattr(protector, "unprotect", None)
        self._protect = protect or _dpapi_protect
        self._unprotect = unprotect or _dpapi_unprotect
        if not callable(self._protect) or not callable(self._unprotect):
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")

    def prepare(self, password: str) -> bytes:
        """Encrypt a password without touching the existing store."""

        if not isinstance(password, str) or not password:
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
        try:
            ciphertext = self._protect(password.encode("utf-8"))
        except SnowLumaSecretStoreUnavailable:
            raise
        except Exception as exc:
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable") from exc
        if not isinstance(ciphertext, bytes) or not ciphertext or len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
        # A fake protector must not accidentally make plaintext-at-rest look
        # like a valid production value.  Tests may use any deterministic
        # bytes, while production always reaches _dpapi_protect above.
        return ciphertext

    def commit(self, ciphertext: bytes) -> None:
        """Atomically commit already-encrypted bytes."""

        if not isinstance(ciphertext, bytes) or not ciphertext or len(ciphertext) > MAX_CIPHERTEXT_BYTES:
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable")
        try:
            _atomic_write(self.path, ciphertext)
        except Exception as exc:
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable") from exc

    def save(self, password: str) -> None:
        """Encrypt then replace the saved password, preserving old data on error."""

        self.commit(self.prepare(password))

    # Names used by transaction callers and focused tests.
    stage = prepare
    write = commit
    persist = save

    def load(self) -> str | None:
        """Return the saved password, or ``None`` when no store exists."""

        try:
            if not self.path.exists():
                return None
            ciphertext = self.path.read_bytes()
            if not ciphertext or len(ciphertext) > MAX_CIPHERTEXT_BYTES:
                raise OSError
            plaintext = self._unprotect(ciphertext)
            if not isinstance(plaintext, bytes) or not plaintext:
                raise OSError
            password = plaintext.decode("utf-8")
            if not password:
                raise OSError
            return password
        except SnowLumaSecretStoreUnavailable:
            raise
        except Exception as exc:
            # Never delete or overwrite a corrupt value: an operator may
            # recover it under the original Windows account later.
            raise SnowLumaSecretStoreUnavailable("secure password storage is unavailable") from exc

    load_password = load
    save_password = save


# Compatibility aliases keep the small boundary discoverable to older callers.
SnowLumaCredentialStore = SnowLumaPasswordStore
SnowLumaSecretStoreError = SnowLumaSecretStoreUnavailable


__all__ = [
    "MAX_CIPHERTEXT_BYTES",
    "SECRET_RELATIVE_PATH",
    "SnowLumaCredentialStore",
    "SnowLumaPasswordStore",
    "SnowLumaSecretStoreError",
    "SnowLumaSecretStoreUnavailable",
    "secret_path",
]
