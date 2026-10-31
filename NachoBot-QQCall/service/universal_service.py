"""Bounded ownership and startup for the shared UniversalVC HTTP service.

This module only supervises the public HTTP service process. Speech capture,
recognition, rendering, and all session semantics stay behind its public API.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from service.private_files import (
    PrivateFileError,
    ProcessLock,
    atomic_private_json,
    is_reparse_point,
    read_private_json,
    read_or_create_private_token,
    secure_directory,
    secure_file,
)
from service.windows import WindowsServiceError, WindowsSystem

_TOKEN_MAX_BYTES = 1024
_CONFIG_MAX_BYTES = 256 * 1024
_HEALTH_MAX_BYTES = 64 * 1024
_HEALTH_TIMEOUT_SECONDS = 3.0
_START_TIMEOUT_SECONDS = 15.0
_CLEANUP_TIMEOUT_SECONDS = 5.0
_TOKEN_PLACEHOLDERS = {"REPLACE_WITH_A_RANDOM_SECRET", "CHANGE_ME"}
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_ALLOWED_CODES = frozenset({
    "universal_api_service_ready",
    "universal_api_service_unavailable",
    "universal_api_auth_failed",
    "universal_api_health_invalid",
    "universal_api_route_mismatch",
    "universal_api_url_unsupported",
    "universal_api_https_autostart_unsupported",
    "universal_api_token_invalid",
    "universal_api_token_mismatch",
    "universal_api_service_config_invalid",
    "universal_api_service_cli_unavailable",
    "universal_api_start_lock_unavailable",
    "universal_api_start_in_progress_timeout",
    "universal_api_start_process_exited",
    "universal_api_start_timeout",
    "universal_api_start_cancelled",
    "universal_api_process_cleanup_unconfirmed",
})


class UniversalServiceError(RuntimeError):
    """A fixed-code error suitable for private/public status projection."""

    def __init__(self, code: str):
        self.code = code if code in _ALLOWED_CODES else "universal_api_service_unavailable"
        super().__init__(self.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ProbeFailure(Exception):
    def __init__(self, code: str, *, connection_refused: bool = False):
        self.code = code
        self.connection_refused = connection_refused
        super().__init__(code)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _valid_token(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and len(value) <= 512
        and value == value.strip()
        and value.upper() not in _TOKEN_PLACEHOLDERS
        and all(0x21 <= ord(char) <= 0x7E for char in value)
    )


def _read_token(path: Path) -> str:
    try:
        if is_reparse_point(path):
            raise UniversalServiceError("universal_api_token_invalid")
        metadata = path.lstat()
        if not path.is_file() or metadata.st_nlink != 1 or metadata.st_size > _TOKEN_MAX_BYTES:
            raise UniversalServiceError("universal_api_token_invalid")
        raw = path.read_bytes()
        token = raw.decode("ascii").strip()
    except UniversalServiceError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise UniversalServiceError("universal_api_token_invalid") from None
    if not _valid_token(token):
        raise UniversalServiceError("universal_api_token_invalid")
    return token


def _write_token_if_absent(path: Path, token: str) -> str:
    """Publish a selected existing token without replacing an existing file."""
    secure_directory(path.parent)
    try:
        with path.open("x", encoding="ascii", newline="") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        secure_file(path)
        return token
    except FileExistsError:
        existing = _read_token(path)
        if existing != token:
            raise UniversalServiceError("universal_api_token_mismatch") from None
        return existing
    except PrivateFileError:
        raise UniversalServiceError("universal_api_token_invalid") from None
    except OSError:
        raise UniversalServiceError("universal_api_token_invalid") from None


def _write_text_if_absent(path: Path, text: str) -> None:
    secure_directory(path.parent)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            secure_file(path)
        except FileExistsError:
            pass
    except (OSError, PrivateFileError):
        raise UniversalServiceError("universal_api_service_config_invalid") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        if is_reparse_point(path):
            raise UniversalServiceError("universal_api_service_config_invalid")
        metadata = path.lstat()
        if not path.is_file() or metadata.st_nlink != 1 or metadata.st_size > _CONFIG_MAX_BYTES:
            raise UniversalServiceError("universal_api_service_config_invalid")
        with path.open("rb") as stream:
            value = tomllib.loads(stream.read(_CONFIG_MAX_BYTES + 1).decode("utf-8"))
    except UniversalServiceError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise UniversalServiceError("universal_api_service_config_invalid") from None
    if not isinstance(value, dict):
        raise UniversalServiceError("universal_api_service_config_invalid")
    return value


def _host_ip(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if type(value) is not str or not value or value.strip() != value:
        raise UniversalServiceError("universal_api_service_config_invalid")
    if value.lower() == "localhost":
        return ipaddress.ip_address("127.0.0.1")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise UniversalServiceError("universal_api_service_config_invalid") from None
    if not address.is_loopback:
        raise UniversalServiceError("universal_api_service_config_invalid")
    return address


def _url_host(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise UniversalServiceError("universal_api_url_unsupported") from None
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise UniversalServiceError("universal_api_url_unsupported")
    return _host_ip(host)


def _url_port(value: str) -> int:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise UniversalServiceError("universal_api_url_unsupported") from None
    return port if port is not None else (443 if parsed.scheme == "https" else 80)


def _connection_refused(exc: BaseException) -> bool:
    candidates = [exc]
    reason = getattr(exc, "reason", None)
    if isinstance(reason, BaseException):
        candidates.append(reason)
    for candidate in candidates:
        if isinstance(candidate, ConnectionRefusedError):
            return True
        if isinstance(candidate, OSError) and candidate.errno in (errno.ECONNREFUSED, 10061):
            return True
    return False


def _health_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/v1/health", "", ""))


def _health_document(base_url: str, token: str, *, timeout: float = _HEALTH_TIMEOUT_SECONDS) -> dict[str, Any]:
    request = urllib.request.Request(
        _health_url(base_url),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise _ProbeFailure("universal_api_service_unavailable")
            body = response.read(_HEALTH_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = "universal_api_auth_failed" if exc.code == 401 else "universal_api_service_unavailable"
        raise _ProbeFailure(code) from None
    except urllib.error.URLError as exc:
        if _connection_refused(exc):
            raise _ProbeFailure("universal_api_service_unavailable", connection_refused=True) from None
        raise _ProbeFailure("universal_api_service_unavailable") from None
    except OSError as exc:
        if _connection_refused(exc):
            raise _ProbeFailure("universal_api_service_unavailable", connection_refused=True) from None
        raise _ProbeFailure("universal_api_service_unavailable") from None
    if len(body) > _HEALTH_MAX_BYTES:
        raise _ProbeFailure("universal_api_health_invalid")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _ProbeFailure("universal_api_health_invalid") from None
    if not isinstance(value, dict):
        raise _ProbeFailure("universal_api_health_invalid")
    capabilities = value.get("capabilities")
    pipeline = capabilities.get("pipeline") if isinstance(capabilities, dict) else None
    instance_id = value.get("service_instance_id")
    if (
        type(value.get("protocol_version")) is not int
        or value["protocol_version"] != 1
        or value.get("ready") is not True
        or type(instance_id) is not str
        or not instance_id
        or len(instance_id) > 128
        or not isinstance(pipeline, dict)
        or pipeline.get("available") is not True
    ):
        raise _ProbeFailure("universal_api_health_invalid")
    return {"service_instance_id": instance_id}


def _process_binding(record: Any) -> dict[str, Any]:
    return {
        "pid": int(record.pid),
        "parentPid": int(record.parent_pid),
        "imagePath": str(record.image_path),
        "creationTime100ns": str(record.creation_time_100ns),
    }


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class UniversalServiceSupervisor:
    """Reuse a healthy UVC instance or start one bounded, verified local child."""

    def __init__(
        self,
        *,
        url: str,
        token_file: str | os.PathLike[str],
        token_file_defaulted: bool,
        runtime_dir: str | os.PathLike[str],
        universal_root: str | os.PathLike[str],
        system: Any | None = None,
        environ: Mapping[str, str] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.url = url
        self.token_file = Path(token_file)
        self.token_file_defaulted = bool(token_file_defaulted)
        self.runtime_dir = Path(runtime_dir)
        self.universal_root = Path(universal_root).resolve(strict=False)
        self.system = system if system is not None else WindowsSystem()
        self.environ = dict(os.environ if environ is None else environ)
        self.popen = popen
        self.monotonic = monotonic
        self.sleep = sleep
        self.last_receipt: dict[str, Any] | None = None
        self._process: Any | None = None
        self._owned_family: list[dict[str, Any]] = []
        self._root_binding: dict[str, Any] | None = None

    @property
    def receipt_path(self) -> Path:
        return self.runtime_dir / "universal-service.private.json"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "universal-service.lock"

    def _public(self, state: str, code: str, *, reused: bool = False) -> dict[str, Any]:
        if state not in ("ready", "unavailable", "not_checked"):
            state = "unavailable"
        if code and code not in _ALLOWED_CODES:
            code = "universal_api_service_unavailable"
        return {"state": state, "code": code, "reused": bool(reused)}

    def _store(self, state: str, code: str, *, reused: bool, instance: str = "", family: list[dict[str, Any]] | None = None, cleanup_confirmed: bool | None = None) -> dict[str, Any]:
        public = self._public(state, code, reused=reused)
        stored_family = list(family or [])
        if reused and not stored_family and instance:
            stored_family = self._prior_family_for_instance(instance)
        receipt: dict[str, Any] = {
            "schema": 1,
            "state": public["state"],
            "code": public["code"],
            "reused": public["reused"],
            "serviceInstanceId": instance,
            "processFamily": stored_family,
        }
        if cleanup_confirmed is not None:
            receipt["cleanupConfirmed"] = cleanup_confirmed
        try:
            atomic_private_json(self.receipt_path, receipt)
            self.last_receipt = receipt
        except PrivateFileError:
            # The service may be usable even when QQCall cannot write its
            # optional ownership receipt. Do not expose that local error.
            self.last_receipt = None
        return public

    def _prior_family_for_instance(self, instance: str) -> list[dict[str, Any]]:
        """Preserve an audit receipt for the same running instance, without adopting it."""
        try:
            prior = read_private_json(self.receipt_path, max_bytes=64 * 1024)
        except PrivateFileError:
            return []
        if (
            not isinstance(prior, dict)
            or prior.get("schema") != 1
            or prior.get("state") != "ready"
            or prior.get("code") != "universal_api_service_ready"
            or prior.get("serviceInstanceId") != instance
            or type(prior.get("processFamily")) is not list
            or len(prior["processFamily"]) > 512
        ):
            return []
        family: list[dict[str, Any]] = []
        seen: set[int] = set()
        for record in prior["processFamily"]:
            if (
                not isinstance(record, dict)
                or set(record) != {"pid", "parentPid", "imagePath", "creationTime100ns"}
                or type(record.get("pid")) is not int
                or not 0 < record["pid"] <= 0x7FFFFFFF
                or record["pid"] in seen
                or type(record.get("parentPid")) is not int
                or not 0 <= record["parentPid"] <= 0x7FFFFFFF
                or type(record.get("imagePath")) is not str
                or not record["imagePath"]
                or len(record["imagePath"]) > 32768
                or "\x00" in record["imagePath"]
                or type(record.get("creationTime100ns")) is not str
                or re.fullmatch(r"[1-9][0-9]{0,19}", record["creationTime100ns"]) is None
            ):
                return []
            seen.add(record["pid"])
            family.append(dict(record))
        return family

    def _probe(self, token: str) -> dict[str, Any]:
        return _health_document(self.url, token)

    def _load_service_settings(
        self, parsed_host: ipaddress._BaseAddress, parsed_port: int
    ) -> tuple[str, int, str, Path, dict[str, str]]:
        if urlsplit(self.url).scheme == "https":
            raise UniversalServiceError("universal_api_https_autostart_unsupported")
        configured_host = self.environ.get("NACHOBOT_UNIVERSALVC_HOST")
        configured_port = self.environ.get("NACHOBOT_UNIVERSALVC_PORT")
        configured_token = self.environ.get("NACHOBOT_UNIVERSALVC_TOKEN")
        config_path = self.universal_root / "service.toml"
        config_exists = config_path.exists() or config_path.is_symlink()
        server: dict[str, Any] = {}
        if config_exists:
            value = _read_toml(config_path)
            server_value = value.get("server", {})
            if not isinstance(server_value, dict):
                raise UniversalServiceError("universal_api_service_config_invalid")
            server = server_value

        if configured_host is not None:
            bind_host = _host_ip(configured_host)
        elif not config_exists and "host" not in server:
            bind_host = parsed_host
        else:
            bind_host = _host_ip(server.get("host", "127.0.0.1"))
        if configured_port is not None:
            if type(configured_port) is not str or re.fullmatch(r"[0-9]{1,5}", configured_port) is None:
                raise UniversalServiceError("universal_api_service_config_invalid") from None
            bind_port = int(configured_port)
        elif not config_exists and "port" not in server:
            bind_port = parsed_port
        else:
            raw_port = server.get("port", 8016)
            if type(raw_port) is not int:
                raise UniversalServiceError("universal_api_service_config_invalid")
            bind_port = raw_port
        if not 1 <= bind_port <= 65535:
            raise UniversalServiceError("universal_api_service_config_invalid")
        if bind_host != parsed_host or bind_port != parsed_port:
            raise UniversalServiceError("universal_api_route_mismatch")

        environment_token = configured_token if _valid_token(configured_token) else None
        server_token = server.get("token")
        default_path = self.token_file_defaulted
        if config_exists and default_path and environment_token is None and not _valid_token(server_token):
            raise UniversalServiceError("universal_api_token_invalid")
        if default_path:
            if environment_token is not None:
                selected = environment_token
            elif config_exists:
                selected = server_token
            elif self.token_file.exists():
                selected = _read_token(self.token_file)
            else:
                try:
                    selected = read_or_create_private_token(self.token_file)
                except PrivateFileError:
                    raise UniversalServiceError("universal_api_token_invalid") from None
            if not _valid_token(selected):
                raise UniversalServiceError("universal_api_token_invalid")
            if self.token_file.exists():
                existing = _read_token(self.token_file)
                if existing != selected:
                    raise UniversalServiceError("universal_api_token_mismatch")
            else:
                _write_token_if_absent(self.token_file, selected)
        else:
            selected = _read_token(self.token_file)
            if environment_token is not None and selected != environment_token:
                raise UniversalServiceError("universal_api_token_mismatch")

        # A valid environment value is the UVC service's documented override.
        # With no env override, pass the selected token explicitly so the
        # launched service and the QQ SDK always use the same credential.
        launch_env_token = environment_token or selected
        private_config_path = config_path
        if not config_exists:
            private_config_path = self.runtime_dir / "universal-service.config.private.toml"
            private_config = "# QQCall-managed empty service config; model defaults stay rooted at UniversalVC.\n"
            _write_text_if_absent(private_config_path, private_config)
            try:
                if is_reparse_point(private_config_path) or private_config_path.read_text(encoding="utf-8") != private_config:
                    raise UniversalServiceError("universal_api_service_config_invalid")
                secure_file(private_config_path)
            except (OSError, PrivateFileError):
                raise UniversalServiceError("universal_api_service_config_invalid") from None

        # Keep the Core endpoint/token environment intact; override only the
        # three documented UniversalVC bind/auth keys.
        launch_env = dict(self.environ)
        launch_env["NACHOBOT_UNIVERSALVC_HOST"] = str(bind_host)
        launch_env["NACHOBOT_UNIVERSALVC_PORT"] = str(bind_port)
        launch_env["NACHOBOT_UNIVERSALVC_TOKEN"] = launch_env_token
        return str(bind_host), bind_port, selected, private_config_path, launch_env

    def _command(self) -> tuple[list[str], dict[str, str]]:
        python = (self.universal_root / ".venv" / "Scripts" / "python.exe").resolve(strict=False)
        entry = (self.universal_root / "service_main.py").resolve(strict=False)
        if not python.is_file() or not entry.is_file():
            raise UniversalServiceError("universal_api_service_cli_unavailable")
        settings = _url_host(self.url)
        loaded = self._load_service_settings(settings, _url_port(self.url))
        config_argument = loaded[3]
        launch_env = loaded[4]
        return [str(python), str(entry), "--config", str(config_argument.resolve(strict=False))], launch_env

    def _candidate_token(self) -> str | None:
        # When the public QQCall config names an explicit token file, that is
        # the credential the SDK will read. Validate it before considering an
        # environment override so an unrelated valid environment token cannot
        # make an incompatible service look ready.
        if not self.token_file_defaulted:
            selected = _read_token(self.token_file)
            environment_token = self.environ.get("NACHOBOT_UNIVERSALVC_TOKEN")
            if _valid_token(environment_token) and selected != environment_token:
                raise UniversalServiceError("universal_api_token_mismatch")
            return selected

        environment_token = self.environ.get("NACHOBOT_UNIVERSALVC_TOKEN")
        if _valid_token(environment_token):
            return environment_token
        config_path = self.universal_root / "service.toml"
        try:
            config_exists = config_path.exists() or config_path.is_symlink()
        except OSError:
            raise UniversalServiceError("universal_api_service_config_invalid") from None
        if config_exists:
            value = _read_toml(config_path)
            server = value.get("server", {})
            if not isinstance(server, dict):
                raise UniversalServiceError("universal_api_service_config_invalid")
            token = server.get("token")
            if _valid_token(token):
                return token
            if self.token_file_defaulted:
                raise UniversalServiceError("universal_api_token_invalid")
        if self.token_file.exists():
            return _read_token(self.token_file)
        if self.token_file_defaulted and not config_exists:
            try:
                return read_or_create_private_token(self.token_file)
            except PrivateFileError:
                raise UniversalServiceError("universal_api_token_invalid") from None
        return None

    def _publish_reuse_token(self, token: str) -> None:
        if not self.token_file_defaulted:
            selected = _read_token(self.token_file)
            environment_token = self.environ.get("NACHOBOT_UNIVERSALVC_TOKEN")
            if selected != token or (_valid_token(environment_token) and selected != environment_token):
                raise UniversalServiceError("universal_api_token_mismatch")
            return
        if self.token_file.exists():
            if _read_token(self.token_file) != token:
                raise UniversalServiceError("universal_api_token_mismatch")
        else:
            _write_token_if_absent(self.token_file, token)

    def _listener_belongs_to(self, family: list[dict[str, Any]]) -> bool | None:
        owners = getattr(self.system, "listener_owners", None)
        if not callable(owners):
            return None
        try:
            port = _url_port(self.url)
            listening = owners(port)
        except (WindowsServiceError, OSError, ValueError):
            return None
        if not listening:
            return False
        return any(pid in {item["pid"] for item in family} for pid in listening)

    def _spawn(self, command: list[str], launch_env: dict[str, str]) -> Any:
        if os.name == "nt":
            breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", None)
            if type(breakaway_flag) is not int or breakaway_flag <= 0:
                raise UniversalServiceError("universal_api_service_cli_unavailable")
            flags = subprocess.CREATE_NEW_CONSOLE | breakaway_flag
        else:
            flags = 0
        try:
            process = self.popen(
                command,
                cwd=str(self.universal_root),
                env=launch_env,
                # Let Windows attach standard handles to the new visible
                # console, including when the supervisor has no console.
                stdin=None,
                stdout=None,
                stderr=None,
                shell=False,
                creationflags=flags,
            )
        except (OSError, subprocess.SubprocessError):
            raise UniversalServiceError("universal_api_service_cli_unavailable") from None
        self._pin_root(process)
        return process

    def _pin_root(self, process: Any) -> None:
        """Pin the exact Popen instance before it can exit or its PID can recycle."""
        if getattr(process, "poll", lambda: 0)() is not None:
            return
        identity_method = getattr(self.system, "process_identity", None)
        identity = None
        if callable(identity_method):
            try:
                identity = identity_method(int(process.pid))
            except (WindowsServiceError, OSError, ValueError):
                identity = None
        if identity is None or getattr(process, "poll", lambda: 0)() is not None:
            return
        filetime, image = identity
        expected = str((self.universal_root / ".venv" / "Scripts" / "python.exe").resolve(strict=False))
        if type(filetime) is not int or filetime <= 0 or _path_key(str(image)) != _path_key(expected):
            return
        self._root_binding = {
            "pid": int(process.pid),
            "creationTime100ns": str(filetime),
            "imagePath": str(image),
        }

    def _capture_family(self, process: Any) -> list[dict[str, Any]]:
        table_method = getattr(self.system, "process_table", None)
        if not callable(table_method):
            return []
        try:
            table = table_method()
        except (WindowsServiceError, OSError, ValueError):
            return []
        expected_python = str((self.universal_root / ".venv" / "Scripts" / "python.exe").resolve(strict=False))
        root = table.get(int(process.pid))
        if self._root_binding is None:
            # Only bind from a fresh table while the original Popen handle is
            # still live. A dead wrapper PID may already belong to another
            # process with the same executable path.
            if getattr(process, "poll", lambda: 0)() is not None or root is None:
                return list(self._owned_family)
            if _path_key(str(root.image_path)) != _path_key(expected_python):
                return list(self._owned_family)
            self._root_binding = {
                "pid": int(root.pid),
                "creationTime100ns": str(root.creation_time_100ns),
                "imagePath": str(root.image_path),
            }
            if getattr(process, "poll", lambda: 0)() is not None:
                self._root_binding = None
                return list(self._owned_family)
        if (
            self._root_binding["pid"] != int(process.pid)
            or root is None
            or self._root_binding["creationTime100ns"] != str(root.creation_time_100ns)
            or _path_key(self._root_binding["imagePath"]) != _path_key(str(root.image_path))
        ):
            return list(self._owned_family)
        records: dict[int, Any] = {int(root.pid): root}
        parents = {int(root.pid): 0}
        changed = True
        while changed:
            changed = False
            for pid, record in table.items():
                pid = int(pid)
                parent = int(record.parent_pid)
                if pid in records or parent not in records:
                    continue
                if int(record.creation_time_100ns) <= int(records[parent].creation_time_100ns):
                    continue
                records[pid] = record
                parents[pid] = parents[parent] + 1
                changed = True
        ordered = sorted(records.values(), key=lambda record: parents[int(record.pid)], reverse=True)
        previously_owned = {item["pid"]: item for item in self._owned_family}
        for record in ordered:
            binding = _process_binding(record)
            prior = previously_owned.get(binding["pid"])
            if prior is not None and (
                prior["creationTime100ns"] != binding["creationTime100ns"]
                or _path_key(prior["imagePath"]) != _path_key(binding["imagePath"])
            ):
                continue
            previously_owned[binding["pid"]] = binding
        self._owned_family = sorted(previously_owned.values(), key=lambda item: item["pid"] == int(process.pid))
        return list(self._owned_family)

    def _cleanup_failed_start(self, process: Any) -> tuple[bool, list[dict[str, Any]]]:
        family = self._capture_family(process)
        terminate = getattr(self.system, "terminate_owned", None)
        if family and callable(terminate):
            records = [
                {
                    "role": "launcher" if item["pid"] == int(process.pid) else "host_child",
                    "pid": item["pid"],
                    "creationTime100ns": item["creationTime100ns"],
                    "imagePath": item["imagePath"],
                    "profilePath": str(self.runtime_dir),
                }
                for item in family
            ]
            try:
                terminate(records)
            except (WindowsServiceError, OSError, ValueError):
                pass
        if getattr(process, "poll", lambda: None)() is None:
            try:
                process.terminate()
                process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)
            except (OSError, subprocess.SubprocessError, TypeError):
                pass
        deadline = self.monotonic() + _CLEANUP_TIMEOUT_SECONDS
        while True:
            process_exited = getattr(process, "poll", lambda: 0)() is not None
            family_gone = bool(family) and self._captured_family_gone(family)
            # Redirectors can exit before their children finish terminating.
            # Keep the bounded wait on every captured identity, not just Popen.
            if process_exited and (family_gone or not family):
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(0.05, remaining))
        # A false terminate_owned result may mean an identity exited while the
        # bounded tree cleanup was in progress. Re-query every exact recorded
        # identity; absence or a different PID identity proves the original is
        # gone without touching the replacement. Unknown/live identities fail
        # closed. Never infer ownership from a port or the reused receipt.
        return bool(process_exited and family_gone), family

    def _captured_family_gone(self, family: list[dict[str, Any]]) -> bool:
        identity_lookup = getattr(self.system, "process_identity", None)
        current_lookup = getattr(self.system, "process_is_current", None)
        for record in family:
            pid = record["pid"]
            expected_time = int(record["creationTime100ns"])
            try:
                current = identity_lookup(pid) if callable(identity_lookup) else None
                if current is not None:
                    actual_time, actual_image = current
                    if (
                        int(actual_time) == expected_time
                        and _path_key(str(actual_image)) == _path_key(record["imagePath"])
                    ):
                        return False
                    continue
                if not callable(current_lookup):
                    return False
                still_current = current_lookup(pid, expected_time)
                if still_current is not False:
                    return False
            except (WindowsServiceError, OSError, TypeError, ValueError, IndexError):
                return False
        return True

    def _checkpoint(self, callback: Callable[[], Any] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except BaseException as exc:
            raise UniversalServiceError("universal_api_start_cancelled") from exc

    def _unavailable(self, code: str, *, cleanup_confirmed: bool | None = None, family: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        safe = code if code in _ALLOWED_CODES else "universal_api_service_unavailable"
        return self._store("unavailable", safe, reused=False, family=family, cleanup_confirmed=cleanup_confirmed)

    def ensure_ready(self, *, checkpoint: Callable[[], Any] | None = None) -> dict[str, Any]:
        try:
            token = self._candidate_token()
            if token is None:
                return self._unavailable("universal_api_token_invalid")
            try:
                health = self._probe(token)
                self._publish_reuse_token(token)
                return self._store("ready", "universal_api_service_ready", reused=True, instance=health["service_instance_id"])
            except _ProbeFailure as failure:
                if not failure.connection_refused:
                    return self._unavailable(failure.code)

            parsed_host = _url_host(self.url)
            parsed = urlsplit(self.url)
            parsed_port = _url_port(self.url)
            if parsed.scheme != "http":
                return self._unavailable("universal_api_https_autostart_unsupported")
            secure_directory(self.runtime_dir)
        except UniversalServiceError as exc:
            return self._unavailable(exc.code)
        except PrivateFileError:
            return self._unavailable("universal_api_start_lock_unavailable")

        deadline = self.monotonic() + _START_TIMEOUT_SECONDS
        lock = ProcessLock(self.lock_path)
        acquired = False
        process: Any | None = None
        launch_env: dict[str, str] | None = None
        try:
            while self.monotonic() < deadline:
                try:
                    self._checkpoint(checkpoint)
                except UniversalServiceError as exc:
                    return self._unavailable(exc.code)
                try:
                    acquired = lock.acquire()
                except PrivateFileError:
                    return self._unavailable("universal_api_start_lock_unavailable")
                if acquired:
                    break
                try:
                    health = self._probe(token)
                    self._publish_reuse_token(token)
                    return self._store("ready", "universal_api_service_ready", reused=True, instance=health["service_instance_id"])
                except _ProbeFailure as failure:
                    if not failure.connection_refused:
                        return self._unavailable(failure.code)
                self.sleep(min(0.1, max(0.0, deadline - self.monotonic())))
            if not acquired:
                return self._unavailable("universal_api_start_in_progress_timeout")

            try:
                health = self._probe(token)
                self._publish_reuse_token(token)
                return self._store("ready", "universal_api_service_ready", reused=True, instance=health["service_instance_id"])
            except _ProbeFailure as failure:
                if not failure.connection_refused:
                    return self._unavailable(failure.code)

            command, launch_env = self._command()
            process = self._spawn(command, launch_env)
            self._process = process
            # Snapshot the exact owned family before a redirector can exit.
            # Further loop snapshots discover children that start slightly
            # after Popen returns.
            self._capture_family(process)
            while self.monotonic() < deadline:
                try:
                    self._checkpoint(checkpoint)
                except UniversalServiceError:
                    cleanup_ok, family = self._cleanup_failed_start(process)
                    code = "universal_api_start_cancelled" if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
                    return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)

                self._capture_family(process)
                if getattr(process, "poll", lambda: None)() is not None:
                    try:
                        health = self._probe(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"])
                        family = self._capture_family(process)
                        listener_owned = self._listener_belongs_to(family)
                        self._publish_reuse_token(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"])
                        return self._store(
                            "ready", "universal_api_service_ready",
                            reused=listener_owned is not True,
                            instance=health["service_instance_id"],
                            family=family if listener_owned is True else [],
                        )
                    except _ProbeFailure as failure:
                        cleanup_ok, family = self._cleanup_failed_start(process)
                        code = "universal_api_start_process_exited" if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
                        return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)

                try:
                    health = self._probe(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"])
                    family = self._capture_family(process)
                    listener_owned = self._listener_belongs_to(family)
                    if listener_owned is False:
                        cleanup_ok, family = self._cleanup_failed_start(process)
                        if not cleanup_ok:
                            return self._unavailable(
                                "universal_api_process_cleanup_unconfirmed",
                                cleanup_confirmed=False,
                                family=family,
                            )
                        self._publish_reuse_token(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"])
                        return self._store("ready", "universal_api_service_ready", reused=True, instance=health["service_instance_id"])
                    self._publish_reuse_token(launch_env["NACHOBOT_UNIVERSALVC_TOKEN"])
                    return self._store("ready", "universal_api_service_ready", reused=False, instance=health["service_instance_id"], family=family)
                except _ProbeFailure as failure:
                    if not failure.connection_refused:
                        cleanup_ok, family = self._cleanup_failed_start(process)
                        code = failure.code if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
                        return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)
                self.sleep(min(0.2, max(0.0, deadline - self.monotonic())))

            cleanup_ok, family = self._cleanup_failed_start(process)
            code = "universal_api_start_timeout" if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
            return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)
        except UniversalServiceError as exc:
            if process is not None:
                cleanup_ok, family = self._cleanup_failed_start(process)
                code = exc.code if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
                return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)
            return self._unavailable(exc.code)
        except PrivateFileError:
            if process is not None:
                cleanup_ok, family = self._cleanup_failed_start(process)
                code = "universal_api_service_config_invalid" if cleanup_ok else "universal_api_process_cleanup_unconfirmed"
                return self._unavailable(code, cleanup_confirmed=cleanup_ok, family=family)
            return self._unavailable("universal_api_service_config_invalid")
        finally:
            if acquired:
                lock.release()


__all__ = ["UniversalServiceError", "UniversalServiceSupervisor"]
