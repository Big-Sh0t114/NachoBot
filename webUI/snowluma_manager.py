"""SnowLuma runtime, credential synchronization, and local API helpers.

The WebUI deliberately keeps this module independent from the process manager
and the FastAPI application.  It owns the small, security-sensitive contract
around the managed SnowLuma distribution: component discovery, offline
credential synchronization, and the authenticated loopback process proxy.

No credential is persisted by this module except in the configuration files
that SnowLuma itself requires.  Passwords and access tokens are accepted only
as call-local values and are never included in exceptions, logs, or returned
status objects.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

import tomlkit

try:
    from .qq_adapter_selector import (
        QQAdapterSelectorError,
        read_qq_adapter,
        selector_value_from_request,
    )
    from .snowluma_locator import (
        SnowLumaLocatorError,
        SnowLumaRuntime,
        resolve_snowluma_runtime,
    )
except ImportError:  # pragma: no cover - direct module imports in older callers
    from qq_adapter_selector import (  # type: ignore
        QQAdapterSelectorError,
        read_qq_adapter,
        selector_value_from_request,
    )
    from snowluma_locator import (  # type: ignore
        SnowLumaLocatorError,
        SnowLumaRuntime,
        resolve_snowluma_runtime,
    )


ROOT_DIR = Path(__file__).resolve().parent.parent
SNOWLUMA_RELEASE_URL = "https://github.com/SnowLuma/SnowLuma/releases/latest"
# Kept as a compatibility export for older integrations.  Runtime discovery
# must go through snowluma_locator; there is intentionally no fixed directory
# path here.
SNOWLUMA_RUNTIME_RELATIVE: str | None = None
SNOWLUMA_ADAPTER_RELATIVE = "NachoBot-SnowLuma-Adapter"
SNOWLUMA_DEFAULT_WEBUI_PORT = 5099
SNOWLUMA_DEFAULT_ONEBOT_PORT = 3001
SNOWLUMA_DEFAULT_ONEBOT_PATH = "/"
SNOWLUMA_VERSION_PREFIX = (1, 14)
MAX_API_RESPONSE_BYTES = 1_048_576
HTTP_TIMEOUT_SECONDS = 4.0

REQUIRED_SNOWLUMA_RUNTIME_FILES: tuple[str, ...] = (
    "package.json",
    "index.mjs",
    # These chunks are imported unconditionally by index.mjs.  Treating only
    # the native binaries as mandatory lets a partial/unpacked distribution
    # pass the setup gate and fail later with an opaque Node import error.
    "utils-tSVKpzEf.js",
    "logger-BAozzyTt.js",
    "config-GJCFWjtq.js",
    "server-CLw7fwOG.js",
    "launcher.bat",
    "client/index.html",
    "native/snowluma-win32-x64.dll",
    "native/snowluma-win32-x64.node",
    "native/websocket-win32-x64.node",
)
REQUIRED_SNOWLUMA_ADAPTER_FILES: tuple[str, ...] = ("main.py", "pyproject.toml")


class SnowLumaError(ValueError):
    """Sanitized, user-facing SnowLuma setup/proxy error."""


class SnowLumaSynchronizationError(SnowLumaError):
    """Raised when the offline three-file synchronization cannot commit safely."""


class SnowLumaApiError(SnowLumaError):
    """Raised for generic, sanitized upstream API failures."""


def _root(value: Path | str | None) -> Path:
    return Path(value or ROOT_DIR).resolve()


def _resolve_runtime(
    root: Path | str | None = None,
    resolution: SnowLumaRuntime | None = None,
) -> SnowLumaRuntime:
    """Return the authoritative runtime selection for one manager operation."""

    if resolution is not None:
        return resolution
    try:
        return resolve_snowluma_runtime(_root(root))
    except SnowLumaLocatorError as exc:
        # Keep the public manager error type stable while preserving the
        # locator's sanitized missing/ambiguity wording.
        raise SnowLumaError(exc.message) from exc


def _bounded_text(value: object, max_length: int = 240) -> str:
    text = str(value or "")
    text = "".join(ch if ch >= " " and ch != "\x7f" else "?" for ch in text)
    return text[:max_length]


def _safe_relative(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SnowLumaError("SnowLuma 路径无效") from exc
    return candidate


def _valid_port(value: object, default: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _normalized_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "/"
    if not text.startswith("/"):
        text = "/" + text
    return "/" if text == "//" else text.rstrip("/") or "/"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态时间字段无效")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态时间字段无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态必须使用 UTC 时间")
    return parsed.astimezone(timezone.utc)


def _atomic_write(path: Path, data: bytes) -> None:
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


def _backup_file(root: Path, path: Path, backup_dir: Path | None = None) -> str | None:
    if not path.exists():
        return None
    target_dir = backup_dir or (root / "config-save" / "setup_backups")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "__", str(path.relative_to(root)))
    backup = target_dir / f"snowluma__{safe_name}.{stamp}.bak"
    _atomic_write(backup, path.read_bytes())
    return backup.name


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SnowLumaSynchronizationError(f"{label} 配置无法解析，已拒绝同步") from exc
    if not isinstance(value, dict):
        raise SnowLumaSynchronizationError(f"{label} 配置顶层必须是对象，已拒绝同步")
    return value


def _read_toml_document(path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return tomlkit.document()
    except (OSError, Exception) as exc:  # tomlkit exposes several parse exceptions
        raise SnowLumaSynchronizationError("SnowLuma 适配器配置无法解析，已拒绝同步") from exc


def _is_loopback_host(value: object) -> bool:
    host = str(value or "").strip().casefold()
    return host in {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "::", "[::]"}


def _is_strict_loopback_host(value: object) -> bool:
    host = str(value or "").strip().casefold()
    # All managed API/readiness probes intentionally target IPv4 127.0.0.1.
    # Do not accept aliases or IPv6 here: they can pass validation while the
    # actual proxy and launcher probe a different socket family.
    return host == "127.0.0.1"


def _loopback_ws_endpoint(entry: Mapping[str, Any]) -> tuple[str, int, str] | None:
    if not isinstance(entry, Mapping) or not _is_loopback_host(entry.get("host")):
        return None
    port = _valid_port(entry.get("port"), 0)
    if not port:
        return None
    return ("127.0.0.1", port, _normalized_path(entry.get("path")))


def _load_runtime_webui_port(runtime_dir: Path) -> int:
    runtime_config = runtime_dir / "config" / "runtime.json"
    try:
        document = json.loads(runtime_config.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        return _valid_port(document.get("webuiPort"), SNOWLUMA_DEFAULT_WEBUI_PORT)
    except FileNotFoundError:
        return SNOWLUMA_DEFAULT_WEBUI_PORT
    except Exception as exc:
        raise SnowLumaSynchronizationError("SnowLuma runtime.json 无法解析，已拒绝同步") from exc


def _load_runtime_webui_host(runtime_dir: Path) -> str:
    runtime_config = runtime_dir / "config" / "runtime.json"
    try:
        document = json.loads(runtime_config.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        return str(document.get("webuiHost", "127.0.0.1")).strip()
    except FileNotFoundError:
        return "127.0.0.1"
    except Exception as exc:
        raise SnowLumaApiError("SnowLuma runtime.json 无法解析") from exc


def _load_runtime_version(runtime_dir: Path) -> str:
    package_path = runtime_dir / "package.json"
    try:
        document = json.loads(package_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SnowLumaSynchronizationError("SnowLuma package.json 无法解析，已拒绝同步") from exc
    if not isinstance(document, dict) or not isinstance(document.get("version"), str):
        raise SnowLumaSynchronizationError("无法识别 SnowLuma 版本，请升级到 1.14.x")
    version = document["version"].strip()
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?", version)
    if not match or tuple(int(match.group(i)) for i in range(1, 3)) != SNOWLUMA_VERSION_PREFIX:
        raise SnowLumaSynchronizationError("SnowLuma 版本不兼容，请升级到 1.14.x 后重试")
    return version


def _validate_token(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) < 16:
        raise SnowLumaSynchronizationError("SnowLuma access token 不能为空且至少需要 16 个字符")
    if value != value.strip() or any(ch.isspace() for ch in value):
        raise SnowLumaSynchronizationError("SnowLuma access token 不能包含空白字符")
    return value


def _validate_password(value: object) -> str:
    if not isinstance(value, str) or len(value) < 10:
        raise SnowLumaSynchronizationError("SnowLuma WebUI 密码至少需要 10 个字符")
    if any(ch.isspace() for ch in value):
        raise SnowLumaSynchronizationError("SnowLuma WebUI 密码不能包含空白字符")
    if not re.search(r"[a-z]", value):
        raise SnowLumaSynchronizationError("SnowLuma WebUI 密码必须包含小写字母")
    if not re.search(r"[A-Z]", value):
        raise SnowLumaSynchronizationError("SnowLuma WebUI 密码必须包含大写字母")
    if not re.search(r"[^A-Za-z0-9]", value):
        raise SnowLumaSynchronizationError("SnowLuma WebUI 密码必须包含特殊字符")
    return value


def _validate_account(value: object) -> str:
    account = str(value or "").strip()
    if not re.fullmatch(r"\d{5,20}", account):
        raise SnowLumaSynchronizationError("QQ 账号格式无效")
    return account


def _snowluma_runtime_busy(root: Path, process_manager: object | None = None) -> bool:
    """Check manager-owned runtime state without importing it at module load."""
    manager = process_manager
    if manager is None:
        try:
            try:
                from . import process_manager as pm  # type: ignore
            except ImportError:  # pragma: no cover - direct script context
                import process_manager as pm  # type: ignore

            manager = getattr(pm, "_LATEST_PROCESS_MANAGER", None)
            retain = getattr(pm, "service_state_retains_runtime", None)
        except Exception:
            manager = None
            retain = None
    else:
        retain = None
    if manager is None:
        return False
    states = getattr(manager, "states", {}) or {}
    state = states.get("snowluma_runtime")
    status = getattr(state, "status", None)
    status_value = getattr(status, "value", status)
    if status_value in {"starting", "running", "stopping"}:
        return True
    if callable(retain):
        try:
            return bool(retain(state))
        except Exception:
            return True
    return bool(
        state
        and (
            getattr(state, "process", None) is not None
            or getattr(state, "windows_job", None) is not None
            or getattr(state, "process_group_id", None) is not None
            or getattr(state, "windows_owned_processes", None)
        )
    )


def _port_listening(port: int, host: str | None = None) -> bool:
    """Check a loopback listener without treating IPv4/IPv6 separately.

    With no explicit host, probe both loopback families so a configured
    ``::1`` listener cannot evade the pre-start port gate.  Callers that need
    one family can still pass it explicitly.
    """
    hosts = (host,) if host else ("127.0.0.1", "::1")
    for candidate in hosts:
        try:
            with socket.create_connection((candidate, port), timeout=0.25):
                return True
        except (OSError, socket.timeout):
            continue
    return False


def _synchronize_adapter_document(
    path: Path,
    onebot_port: int,
    access_token: str,
) -> bytes:
    if not path.is_file():
        raise SnowLumaSynchronizationError(
            "SnowLuma 适配器 config.toml 不存在或未部署，已拒绝同步"
        )
    document = _read_toml_document(path)
    required_tables = ("snowluma", "nachobot_server", "voice")
    for table_name in required_tables:
        section = document.get(table_name)
        if not isinstance(section, Mapping):
            raise SnowLumaSynchronizationError(
                f"SnowLuma 适配器缺少 [{table_name}] 配置表，已拒绝同步"
            )
    section = document["snowluma"]
    section["host"] = "127.0.0.1"
    section["port"] = onebot_port
    section["path"] = SNOWLUMA_DEFAULT_ONEBOT_PATH
    # The adapter is a local client of SnowLuma's plain WebSocket server.
    # Stage this together with the endpoint/token fields so a stale wss value
    # can never survive a successful deployment.
    section["scheme"] = "ws"
    section["token"] = access_token
    return tomlkit.dumps(document).encode("utf-8")


def _synchronize_onebot_document(path: Path, qq_account: str, port: int, token: str) -> bytes:
    creating = not path.exists()
    document = _read_json_object(path, "SnowLuma OneBot")
    networks = document.get("networks")
    if networks is None:
        # Match SnowLuma's v1.14.x snapshot schema when creating a per-UIN
        # file.  Keeping all four network arrays and the auxiliary sections
        # avoids producing a file that the native runtime cannot restore.
        networks = {
            "httpServers": [],
            "httpClients": [],
            "wsServers": [],
            "wsClients": [],
        }
        document["networks"] = networks
    if creating:
        document.setdefault("mode", "snapshot")
        document.setdefault(
            "statusCommand",
            {
                "enabled": True,
                "swallow": False,
                "cooldownSeconds": 5,
                "trigger": "#snowluma",
            },
        )
        document.setdefault("historySync", {"enabled": False})
        document.setdefault("notifications", {"channelIds": []})
    if not isinstance(networks, dict):
        raise SnowLumaSynchronizationError("SnowLuma OneBot networks 必须是对象")
    servers = networks.get("wsServers")
    if servers is None:
        servers = []
        networks["wsServers"] = servers
    if not isinstance(servers, list) or any(not isinstance(item, dict) for item in servers):
        raise SnowLumaSynchronizationError("SnowLuma OneBot wsServers 必须是对象数组")

    normalized_target = ("127.0.0.1", port, SNOWLUMA_DEFAULT_ONEBOT_PATH)
    named = [index for index, item in enumerate(servers) if item.get("name") == "NachoBot"]
    exact = [
        index
        for index, item in enumerate(servers)
        if _loopback_ws_endpoint(item) == normalized_target
    ]
    candidate_index = named[0] if named else (exact[0] if exact else None)

    duplicate_indexes: set[int] = set(named[1:]) | set(exact)
    if candidate_index is not None:
        duplicate_indexes.discard(candidate_index)
    for index, item in enumerate(servers):
        if index == candidate_index or index in duplicate_indexes:
            continue
        endpoint = _loopback_ws_endpoint(item)
        if endpoint and endpoint[1] == port:
            raise SnowLumaSynchronizationError("SnowLuma OneBot 本机 WS 端口存在冲突")

    desired = {
        "enabled": True,
        "name": "NachoBot",
        "messageFormat": "array",
        "reportSelfMessage": False,
        "host": "127.0.0.1",
        "port": port,
        "path": SNOWLUMA_DEFAULT_ONEBOT_PATH,
        "role": "Universal",
        "accessToken": token,
    }
    if candidate_index is None:
        servers.append(desired)
    else:
        current = servers[candidate_index]
        # ``enable`` was used by an older integration and is ignored by
        # SnowLuma.  Remove it from the managed server node while preserving every
        # unrelated field in the surrounding document.
        current.pop("enable", None)
        current.update(desired)
        if duplicate_indexes:
            document["networks"]["wsServers"] = [
                item for index, item in enumerate(servers) if index not in duplicate_indexes
            ]
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _synchronize_webui_document(path: Path, password: str) -> bytes:
    if path.exists():
        document = _read_json_object(path, "SnowLuma WebUI")
        allowed = {"passwordHash", "passwordSalt", "mustChangePassword", "generatedAt", "updatedAt", "totp"}
        if set(document) - allowed:
            raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态包含未知字段，已拒绝同步")
        if "totp" in document:
            raise SnowLumaSynchronizationError(
                "SnowLuma WebUI 已启用 TOTP；请在 SnowLuma 中完成凭据迁移后再部署"
            )
        required = {"passwordHash", "passwordSalt", "mustChangePassword", "generatedAt", "updatedAt"}
        if set(document) != required:
            raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态不完整，已拒绝同步")
        if (
            not isinstance(document["passwordHash"], str)
            or not re.fullmatch(r"[0-9a-f]{128}", document["passwordHash"])
            or not isinstance(document["passwordSalt"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", document["passwordSalt"])
            or not isinstance(document["mustChangePassword"], bool)
        ):
            raise SnowLumaSynchronizationError("SnowLuma WebUI 凭据状态格式无效，已拒绝同步")
        _parse_utc_timestamp(document["generatedAt"])
        _parse_utc_timestamp(document["updatedAt"])
        generated_at = document["generatedAt"]
    else:
        generated_at = _utc_now()

    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=64
    ).hex()
    document = {
        "passwordHash": password_hash,
        "passwordSalt": salt.hex(),
        "mustChangePassword": False,
        "generatedAt": generated_at,
        "updatedAt": _utc_now(),
    }
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SnowLumaSyncResult:
    """Safe synchronization result containing filenames/status only."""

    status: str
    files: tuple[str, ...]
    backups: tuple[str, ...]
    version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "files": list(self.files),
            "backups": list(self.backups),
            "version": self.version,
        }


class SnowLumaManager:
    """Discovery and offline synchronization facade used by setup/server code."""

    release_url = SNOWLUMA_RELEASE_URL

    @staticmethod
    def runtime_info(
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> SnowLumaRuntime:
        return _resolve_runtime(root, resolution)

    @staticmethod
    def runtime_path(
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> Path:
        return SnowLumaManager.runtime_info(root, resolution=resolution).path

    @staticmethod
    def adapter_path(root: Path | str | None = None) -> Path:
        return _safe_relative(_root(root), SNOWLUMA_ADAPTER_RELATIVE)

    @classmethod
    def required_components(
        cls,
        root: Path | str | None = None,
        selected: str = "snowluma",
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> list[str]:
        base = _root(root)
        selected = str(selected or "").strip().casefold()
        missing: list[str] = []
        if selected == "snowluma":
            adapter = cls.adapter_path(base)
            try:
                runtime_info = cls.runtime_info(base, resolution=resolution)
            except SnowLumaError as exc:
                return [str(exc)]
            runtime = runtime_info.path
            for relative in REQUIRED_SNOWLUMA_ADAPTER_FILES:
                if not (adapter / relative).is_file():
                    missing.append(f"NachoBot-SnowLuma-Adapter/{relative}")
            for relative in REQUIRED_SNOWLUMA_RUNTIME_FILES:
                if not (runtime / relative).is_file():
                    missing.append(f"{runtime_info.name}/{relative}")
        elif selected == "napcat":
            adapter = _safe_relative(base, "NachoBot-Napcat-Adapter")
            shell = _safe_relative(base, "NapCat.Shell")
            for relative in ("main.py", "pyproject.toml"):
                if not (adapter / relative).is_file():
                    missing.append(f"NachoBot-Napcat-Adapter/{relative}")
            if not ((shell / "launcher-user.bat").is_file() or (shell / "napcat.bat").is_file()):
                missing.append("NapCat.Shell/launcher-user.bat 或 NapCat.Shell/napcat.bat")
        else:
            missing.append("未知 QQ 运行时")
        return missing

    @classmethod
    def installation_status(
        cls,
        root: Path | str | None = None,
        selected: str = "snowluma",
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, Any]:
        selected = str(selected or "").strip().casefold()
        runtime_info: SnowLumaRuntime | None = None
        runtime_error = ""
        if selected == "snowluma":
            try:
                runtime_info = cls.runtime_info(root, resolution=resolution)
            except SnowLumaError as exc:
                runtime_error = str(exc)
        missing = cls.required_components(root, selected, resolution=runtime_info)
        status: dict[str, Any] = {
            "selected": selected,
            "installed": not missing,
            "missing": missing,
            "download_url": SNOWLUMA_RELEASE_URL if selected == "snowluma" else "",
        }
        if selected == "snowluma":
            status.update(
                {
                    "runtime_name": runtime_info.name if runtime_info else None,
                    "runtime_path": str(runtime_info.path) if runtime_info else None,
                    "runtime_version": runtime_info.version if runtime_info else None,
                    "runtime_error": runtime_error,
                }
            )
            if runtime_info is not None:
                status["credential_consistency"] = cls.credential_consistency(
                    root, resolution=runtime_info
                )
        return status

    check_installation = installation_status
    check_required_components = installation_status

    @classmethod
    def configured_ports(
        cls,
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, int]:
        base = _root(root)
        try:
            runtime = cls.runtime_path(base, resolution=resolution)
        except SnowLumaError:
            runtime = None
        webui_port = (
            _load_runtime_webui_port(runtime)
            if runtime is not None and (runtime / "config" / "runtime.json").exists()
            else SNOWLUMA_DEFAULT_WEBUI_PORT
        )
        adapter_config = cls.adapter_path(base) / "config.toml"
        onebot_port = SNOWLUMA_DEFAULT_ONEBOT_PORT
        try:
            doc = tomlkit.parse(adapter_config.read_text(encoding="utf-8"))
            section = doc.get("snowluma", {})
            onebot_port = _valid_port(section.get("port"), onebot_port)
        except Exception:
            pass
        return {"webui": webui_port, "onebot": onebot_port}

    @classmethod
    def credential_consistency(
        cls,
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, Any]:
        """Return a redacted adapter/OneBot credential consistency status.

        Only endpoint metadata and a boolean/status leave this method.  Token
        contents, lengths, hashes, and raw configuration text are deliberately
        excluded so the result is safe for setup/API responses and logs.
        """

        base = _root(root)
        try:
            runtime = cls.runtime_info(base, resolution=resolution)
        except SnowLumaError as exc:
            return {
                "status": "missing",
                "consistent": False,
                "message": str(exc),
                "endpoint": None,
            }

        adapter_path = cls.adapter_path(base) / "config.toml"
        endpoint: dict[str, Any] | None = None
        adapter_token = ""
        try:
            adapter_document = tomlkit.parse(adapter_path.read_text(encoding="utf-8"))
            section = adapter_document.get("snowluma", {})
            if not isinstance(section, Mapping):
                raise ValueError
            adapter_token = section.get("token") if isinstance(section.get("token"), str) else ""
            endpoint_tuple = _loopback_ws_endpoint(
                {
                    "host": section.get("host"),
                    "port": section.get("port"),
                    "path": section.get("path"),
                }
            )
            if endpoint_tuple is None:
                raise ValueError
            endpoint = {
                "host": endpoint_tuple[0],
                "port": endpoint_tuple[1],
                "path": endpoint_tuple[2],
            }
        except Exception:
            return {
                "status": "missing",
                "consistent": False,
                "message": "SnowLuma 凭据缺失或配置无效，请重新部署 SnowLuma",
                "endpoint": None,
            }

        if not adapter_token or endpoint is None:
            return {
                "status": "missing",
                "consistent": False,
                "message": "SnowLuma 凭据缺失，请重新部署 SnowLuma",
                "endpoint": endpoint,
            }

        expected = (endpoint["host"], endpoint["port"], endpoint["path"])
        matched = 0
        missing = False
        mismatch = False
        config_dir = runtime.path / "config"
        try:
            config_files = sorted(config_dir.glob("onebot_*.json"), key=lambda path: path.name.casefold())
        except OSError:
            config_files = []
        for config_path in config_files:
            try:
                document = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(document, dict):
                continue
            networks = document.get("networks")
            servers = networks.get("wsServers") if isinstance(networks, dict) else None
            if not isinstance(servers, list):
                continue
            for server in servers:
                if not isinstance(server, dict) or _loopback_ws_endpoint(server) != expected:
                    continue
                matched += 1
                token = server.get("accessToken")
                if not isinstance(token, str) or not token:
                    missing = True
                elif token != adapter_token:
                    mismatch = True

        if mismatch:
            status = "mismatch"
            message = "SnowLuma 适配器与 OneBot 凭据不一致，请重新部署 SnowLuma"
        elif missing or matched == 0:
            status = "missing"
            message = "SnowLuma OneBot 凭据缺失，请重新部署 SnowLuma"
        else:
            status = "ok"
            message = "SnowLuma 凭据一致"
        return {
            "status": status,
            "consistent": status == "ok",
            "message": message,
            "endpoint": endpoint,
            "matched_servers": matched,
        }

    credential_status = credential_consistency

    @classmethod
    def validate_credential_consistency(
        cls,
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, Any]:
        result = cls.credential_consistency(root, resolution=resolution)
        if not result.get("consistent"):
            raise SnowLumaError(str(result.get("message") or "SnowLuma 凭据缺失，请重新部署 SnowLuma"))
        return result

    @classmethod
    def validate_launch_boundary(
        cls,
        root: Path | str | None = None,
        *,
        require_free_ports: bool = True,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, Any]:
        """Validate the local-only, pre-start SnowLuma launch boundary.

        SnowLuma owns both the WebUI listener and its OneBot WebSocket
        listener.  Both sockets must be free before its bundled runtime is
        spawned; checking only the WebUI port can otherwise leave a runtime
        that starts half-way and cannot expose QQ events.  The configured
        WebUI host is intentionally stricter than a wildcard bind: the WebUI
        proxy is loopback-only and must fail closed for a remotely reachable
        host.
        """
        base = _root(root)
        runtime_info = cls.runtime_info(base, resolution=resolution)
        runtime = runtime_info.path
        webui_host = _load_runtime_webui_host(runtime)
        if not _is_strict_loopback_host(webui_host):
            raise SnowLumaError("SnowLuma WebUI webuiHost 必须是本机回环地址")

        credential = cls.validate_credential_consistency(base, resolution=runtime_info)
        ports = cls.configured_ports(base, resolution=runtime_info)
        occupied: list[str] = []
        if require_free_ports:
            for label, port in (("WebUI", ports["webui"]), ("OneBot", ports["onebot"])):
                if _port_listening(port):
                    occupied.append(f"{label} :{port}")
        if occupied:
            raise SnowLumaError(
                "SnowLuma 启动前端口已被占用，请停止占用进程后重试："
                + ", ".join(occupied)
            )
        return {
            "webui_host": webui_host,
            "runtime_name": runtime_info.name,
            "runtime_path": str(runtime_info.path),
            "runtime_version": runtime_info.version,
            "credential_consistency": credential,
            **ports,
        }

    @classmethod
    def transport_status(
        cls,
        root: Path | str | None = None,
        *,
        resolution: SnowLumaRuntime | None = None,
    ) -> dict[str, Any]:
        """Report socket transport separately from credential/auth state."""

        ports = cls.configured_ports(root, resolution=resolution)
        webui_listening = _port_listening(ports["webui"])
        onebot_listening = _port_listening(ports["onebot"])
        credential = cls.credential_consistency(root, resolution=resolution)
        if not webui_listening:
            transport = "webui_not_listening"
        elif not onebot_listening:
            transport = "onebot_not_listening"
        else:
            transport = "listening"
        return {
            "transport": transport,
            "webui": "listening" if webui_listening else "not_listening",
            "onebot": "listening" if onebot_listening else "not_listening",
            "ports": ports,
            "credential_consistency": credential,
        }

    @classmethod
    def synchronize(
        cls,
        root: Path | str | None,
        qq_account: object,
        access_token: object,
        webui_password: object,
        *,
        process_manager: object | None = None,
        expected_adapter: str = "snowluma",
        backup_dir: Path | None = None,
    ) -> SnowLumaSyncResult:
        base = _root(root)
        try:
            selected = selector_value_from_request(expected_adapter)
        except QQAdapterSelectorError as exc:
            raise SnowLumaSynchronizationError("QQ 适配器选择无效") from exc
        if selected != "snowluma":
            raise SnowLumaSynchronizationError("只有选中的 SnowLuma 后端可以同步 SnowLuma 凭据")
        account = _validate_account(qq_account)
        token = _validate_token(access_token)
        password = _validate_password(webui_password)

        try:
            runtime_info = cls.runtime_info(base)
        except SnowLumaError as exc:
            # Synchronization callers already handle the transaction-specific
            # error type.  Preserve the locator's sanitized missing/
            # ambiguity/version message without exposing a filesystem path.
            raise SnowLumaSynchronizationError(str(exc)) from exc
        runtime = runtime_info.path
        adapter = cls.adapter_path(base)
        version = runtime_info.version
        ports = cls.configured_ports(base, resolution=runtime_info)
        if _port_listening(ports["webui"]):
            raise SnowLumaSynchronizationError("SnowLuma WebUI 正在运行，请停止后再同步磁盘凭据")
        if _port_listening(ports["onebot"]):
            raise SnowLumaSynchronizationError("SnowLuma OneBot WS 端口正在使用，请停止后再同步磁盘凭据")
        if _snowluma_runtime_busy(base, process_manager):
            raise SnowLumaSynchronizationError("SnowLuma 运行时正在运行或仍保留进程句柄，请停止后再同步")

        env_path = base / "NachoBot" / ".env"
        try:
            if read_qq_adapter(env_path) != "snowluma":
                raise SnowLumaSynchronizationError("当前 qq_adapter 选择已变化，请重新加载向导")
        except QQAdapterSelectorError as exc:
            raise SnowLumaSynchronizationError("当前 qq_adapter 配置无效，请重新选择 SnowLuma") from exc

        adapter_path = adapter / "config.toml"
        onebot_path = runtime / "config" / f"onebot_{account}.json"
        webui_path = runtime / "config" / "webui.json"
        originals: dict[Path, bytes | None] = {}
        staged: dict[Path, bytes] = {}
        backups: list[str] = []
        try:
            for path in (adapter_path, onebot_path, webui_path):
                originals[path] = path.read_bytes() if path.exists() else None
            staged[adapter_path] = _synchronize_adapter_document(adapter_path, ports["onebot"], token)
            staged[onebot_path] = _synchronize_onebot_document(onebot_path, account, ports["onebot"], token)
            staged[webui_path] = _synchronize_webui_document(webui_path, password)

            # The selector is the authoritative live boundary immediately
            # before backups and replacements begin.
            if read_qq_adapter(env_path) != "snowluma":
                raise SnowLumaSynchronizationError("当前 qq_adapter 选择已变化，请重新加载向导")
            for path in (adapter_path, onebot_path, webui_path):
                backup = _backup_file(base, path, backup_dir)
                if backup:
                    backups.append(backup)
            # Add each target before attempting its commit.  A replacement
            # helper may raise after moving bytes into place; that target must
            # still be restored during rollback.
            attempted: list[Path] = []
            rollback_failed = False
            try:
                for path, payload in staged.items():
                    if read_qq_adapter(env_path) != "snowluma":
                        raise SnowLumaSynchronizationError(
                            "当前 qq_adapter 选择已变化，请重新加载向导"
                        )
                    attempted.append(path)
                    _atomic_write(path, payload)
            except Exception as exc:
                for path in reversed(attempted):
                    original = originals[path]
                    try:
                        if original is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_write(path, original)
                    except Exception:
                        rollback_failed = True
                if rollback_failed:
                    raise SnowLumaSynchronizationError(
                        "SnowLuma 凭据事务提交失败，回滚失败，请使用备份恢复"
                    ) from exc
                raise SnowLumaSynchronizationError(
                    "SnowLuma 凭据事务提交失败，已回滚"
                ) from exc
        except SnowLumaSynchronizationError:
            raise
        except Exception as exc:
            raise SnowLumaSynchronizationError("SnowLuma 凭据同步失败，未完成提交") from exc
        return SnowLumaSyncResult(
            status="ok",
            files=tuple(path.relative_to(base).as_posix() for path in staged),
            backups=tuple(backups),
            version=version,
        )

    synchronize_credentials = synchronize
    configure = synchronize


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        return None


def _validate_pid(pid: object) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 4_194_304:
        raise SnowLumaApiError("PID 无效")
    return pid


class SnowLumaAPIClient:
    """Small authenticated client constrained to local SnowLuma WebUI APIs."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        port: int | None = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.root = _root(root)
        try:
            self.runtime = SnowLumaManager.runtime_info(self.root)
        except SnowLumaError as exc:
            raise SnowLumaApiError(str(exc)) from exc
        runtime = self.runtime.path
        webui_host = _load_runtime_webui_host(runtime)
        if not _is_strict_loopback_host(webui_host):
            raise SnowLumaApiError("SnowLuma WebUI 仅允许配置为本机回环地址")
        self.port = _valid_port(port, _load_runtime_webui_port(runtime)) if port is not None else _load_runtime_webui_port(runtime)
        self.timeout = max(0.2, min(float(timeout), 10.0))
        self._token = ""
        self._cookie = ""
        self._opener = build_opener(_NoRedirectHandler())

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or "//" in path or not path.startswith("/api/"):
            raise SnowLumaApiError("SnowLuma API 路径无效")
        url = f"{self.base_url}{path}"
        parsed = urlsplit(url)
        if parsed.hostname != "127.0.0.1" or parsed.port != self.port:
            raise SnowLumaApiError("SnowLuma API 仅允许本机访问")
        data = None if body is None else json.dumps(dict(body), ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._cookie:
            headers["Cookie"] = self._cookie
        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise SnowLumaApiError("SnowLuma API 响应过大")
                set_cookie = response.headers.get("Set-Cookie", "")
                if set_cookie:
                    self._cookie = set_cookie.split(";", 1)[0]
                if not raw:
                    return {}
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise SnowLumaApiError("SnowLuma API 返回格式无效") from exc
                if not isinstance(payload, (dict, list)):
                    raise SnowLumaApiError("SnowLuma API 返回格式无效")
                if isinstance(payload, dict) and payload.get("success") is False:
                    raise SnowLumaApiError("SnowLuma API 操作失败")
                return payload
        except SnowLumaApiError:
            raise
        except HTTPError as exc:
            # Do not include upstream response bodies, URLs, or request data.
            raise SnowLumaApiError(f"SnowLuma API 请求失败（HTTP {exc.code}）") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SnowLumaApiError("无法连接 SnowLuma WebUI") from exc

    def login(self, password: str) -> dict[str, Any]:
        if not isinstance(password, str) or not password:
            raise SnowLumaApiError("SnowLuma WebUI 登录凭据无效")
        payload = self._request("POST", "/api/login", {"password": password})
        if not isinstance(payload, dict) or payload.get("ok") is False:
            raise SnowLumaApiError("SnowLuma WebUI 登录失败")
        for key in ("token", "accessToken", "access_token"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate:
                self._token = candidate
                break
        # The proxy must fail closed if the login response did not establish a
        # bearer session.  Keeping a password-only response as "authenticated"
        # would cause subsequent calls to run without the required auth header.
        if not self._token:
            raise SnowLumaApiError("SnowLuma WebUI 登录失败")
        return {"status": "ok"}

    def logout(self) -> dict[str, Any]:
        """End the local WebUI session and always clear local credentials.

        SnowLuma's bundled client uses an empty-body ``POST /api/logout``.
        Keep that exact transport shape; the cleanup caller may deliberately
        ignore a network/logout error after an already-failed operation.
        """
        try:
            payload = self._request("POST", "/api/logout")
            if isinstance(payload, dict):
                return payload
            return {"status": "ok"}
        finally:
            self._token = ""
            self._cookie = ""

    def close(self) -> dict[str, Any]:
        """Close this short-lived proxy session through SnowLuma logout."""
        try:
            return self.logout()
        finally:
            # Keep this redundant clear so a future logout override or
            # transport failure can never retain bearer/cookie state.
            self._token = ""
            self._cookie = ""

    @staticmethod
    def _process_value(item: Mapping[str, Any], key: str, *aliases: str) -> Any:
        for candidate in (key, *aliases):
            if candidate in item:
                return item[candidate]
        return None

    def probe_login(self, pid: object) -> dict[str, Any]:
        valid_pid = _validate_pid(pid)
        payload = self._request("GET", f"/api/processes/{valid_pid}/probe-login")
        if not isinstance(payload, dict):
            return {}
        info = payload.get("info")
        if isinstance(info, Mapping):
            return dict(info)
        return payload

    def list_processes(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/processes")
        if isinstance(payload, dict):
            raw_items = payload.get("list", payload.get("processes", payload.get("items", [])))
        else:
            raw_items = payload
        if not isinstance(raw_items, list):
            raise SnowLumaApiError("SnowLuma 进程列表格式无效")
        sanitized: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            candidate_pid = self._process_value(raw, "pid", "processId", "process_id")
            try:
                pid = _validate_pid(int(candidate_pid))
            except (TypeError, ValueError, SnowLumaApiError):
                continue
            # The normal process-list response already carries the account
            # and lifecycle fields.  Do not perform a blocking probe for
            # every process during a refresh; the explicit probe endpoint is
            # available for a user-requested detail check.
            info = raw.get("info")
            details: Mapping[str, Any] = info if isinstance(info, Mapping) else {}

            def value(key: str, *aliases: str) -> Any:
                candidate = self._process_value(raw, key, *aliases)
                return candidate if candidate is not None else self._process_value(details, key, *aliases)

            uin = value("uin", "account", "qq", "userId")
            path = value("path", "executablePath", "exePath")
            injected = value("injected", "isInjected")
            connected = value("connected", "isConnected")
            logged_in = value("loggedIn", "logged_in", "isLoggedIn")
            status = value("status", "state")
            error = value("error", "message")
            method = value("method", "injectionMethod")
            sanitized.append(
                {
                    "pid": pid,
                    "name": _bounded_text(value("name", "processName"), 120),
                    "path": _bounded_text(path, 260),
                    "injected": bool(injected),
                    "connected": bool(connected),
                    "loggedIn": bool(logged_in),
                    "uin": _bounded_text(uin, 40) if uin not in (None, "", 0, "0") else "",
                    "status": _bounded_text(status, 120),
                    "error": _bounded_text(error, 240),
                    "method": _bounded_text(method, 80),
                }
            )
        return sanitized

    def process_action(self, pid: object, action: str) -> dict[str, Any]:
        valid_pid = _validate_pid(pid)
        if action not in {"load", "unload", "refresh"}:
            raise SnowLumaApiError("SnowLuma 进程操作无效")
        payload = self._request("POST", f"/api/processes/{valid_pid}/{action}", {})
        return {"status": "ok", "pid": valid_pid, "action": action}

    load = lambda self, pid: self.process_action(pid, "load")
    unload = lambda self, pid: self.process_action(pid, "unload")
    refresh = lambda self, pid: self.process_action(pid, "refresh")


# Compatibility aliases make the focused proxy/synchronizer easy to consume
# from tests and from older WebUI route code without exposing implementation
# details in the HTTP API.
SnowLumaProcessClient = SnowLumaAPIClient
SnowLumaApiClient = SnowLumaAPIClient
SnowLumaCredentialSynchronizer = SnowLumaManager


__all__ = [
    "HTTP_TIMEOUT_SECONDS",
    "MAX_API_RESPONSE_BYTES",
    "REQUIRED_SNOWLUMA_ADAPTER_FILES",
    "REQUIRED_SNOWLUMA_RUNTIME_FILES",
    "SNOWLUMA_DEFAULT_ONEBOT_PATH",
    "SNOWLUMA_DEFAULT_ONEBOT_PORT",
    "SNOWLUMA_DEFAULT_WEBUI_PORT",
    "SNOWLUMA_RELEASE_URL",
    "SNOWLUMA_RUNTIME_RELATIVE",
    "SnowLumaLocatorError",
    "SnowLumaRuntime",
    "SnowLumaAPIClient",
    "SnowLumaApiClient",
    "SnowLumaApiError",
    "SnowLumaCredentialSynchronizer",
    "SnowLumaError",
    "SnowLumaManager",
    "SnowLumaProcessClient",
    "SnowLumaSynchronizationError",
    "SnowLumaSyncResult",
]
