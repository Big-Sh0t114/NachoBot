"""Authoritative, credential-safe SnowLuma runtime discovery.

SnowLuma releases are unpacked directly below the NachoBot repository.  The
release archive name is not stable (the exact ``SnowLuma`` directory and
versioned names such as ``SnowLuma-v1.14.17-win-x64`` are both seen in the
wild), so callers must use this module instead of guessing a path.

The module intentionally uses only the Python standard library.  That keeps it
safe to invoke from the three top-level ``.bat`` launchers before any project
dependencies have been installed.  The default resolver never reads
credentials; its optional consistency check compares them in memory and
prints only a redacted status.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SUPPORTED_VERSION_PREFIX = (1, 14)
_PACKAGE_VERSION_RE = re.compile(r"^1\.14\.(\d+)$")
_DIRECTORY_NAME_RE = re.compile(
    r"^snowluma(?:[-_]?v?(?P<version>\d+\.\d+\.\d+)"
    r"(?:-(?P<suffix>[A-Za-z0-9][A-Za-z0-9._-]*))?)?$",
    re.IGNORECASE,
)
_REPARSE_POINT_ATTRIBUTE = 0x0400


class SnowLumaLocatorError(RuntimeError):
    """Sanitized runtime discovery failure.

    ``code`` is stable for Python/BAT callers; ``message`` contains no full
    paths and ambiguity diagnostics contain directory names only.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SnowLumaRuntime:
    """A validated, repository-local SnowLuma runtime candidate."""

    root: Path
    path: Path
    name: str
    version: str

    @property
    def directory_name(self) -> str:
        """Compatibility/readability alias used by status and launch code."""

        return self.name

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "directory_name": self.name,
            "path": str(self.path),
            "version": self.version,
        }


def _repository_root(value: Path | str | None) -> Path:
    root = Path(value) if value is not None else Path(__file__).resolve().parent.parent
    try:
        return root.resolve()
    except OSError as exc:
        raise SnowLumaLocatorError(
            "missing", "SnowLuma Runtime 项目根目录无效，请在项目根目录部署 1.14.x Runtime"
        ) from exc


def _is_reparse_or_symlink(path: Path) -> bool:
    """Reject symlink/junction candidates on both Windows and POSIX tests."""

    try:
        if path.is_symlink():
            return True
        # ``stat`` follows a junction on Windows.  ``lstat`` preserves the
        # reparse-point attributes of the candidate itself.
        stat_result = os.lstat(path)
    except OSError:
        return True
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _inside_root(root: Path, candidate: Path) -> Path | None:
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _directory_version(name: str) -> str | None:
    match = _DIRECTORY_NAME_RE.fullmatch(name)
    if not match:
        return None
    return match.group("version")


def _package_version(path: Path) -> str | None:
    try:
        document = json.loads((path / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    version = document.get("version")
    if not isinstance(version, str):
        return None
    version = version.strip()
    if not _PACKAGE_VERSION_RE.fullmatch(version):
        return None
    return version


def _candidate_directories(root: Path) -> Iterable[tuple[str, Path, str]]:
    """Yield only valid name/package/version candidates below ``root``."""

    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise SnowLumaLocatorError(
            "missing", "SnowLuma Runtime 项目根目录无法读取，请在项目根目录部署 1.14.x Runtime"
        ) from exc

    for entry in entries:
        if not entry.is_dir() or _is_reparse_or_symlink(entry):
            continue
        directory_version = _directory_version(entry.name)
        if not re.fullmatch(r"snowluma", entry.name, re.IGNORECASE) and directory_version is None:
            continue
        resolved = _inside_root(root, entry)
        if resolved is None or resolved == root:
            continue
        package_version = _package_version(resolved)
        if package_version is None:
            continue
        # An exact SnowLuma directory gets its version from package.json.  A
        # release archive name must agree exactly with that package version;
        # otherwise it is not a genuine candidate and must not create a false
        # ambiguity with a valid release.
        if directory_version is not None and directory_version != package_version:
            continue
        yield entry.name, resolved, package_version


def resolve_snowluma_runtime(root: Path | str | None = None) -> SnowLumaRuntime:
    """Resolve exactly one valid SnowLuma 1.14.x runtime below ``root``."""

    repository = _repository_root(root)
    candidates = sorted(_candidate_directories(repository), key=lambda item: item[0].casefold())
    if not candidates:
        raise SnowLumaLocatorError(
            "missing", "SnowLuma Runtime 未找到有效的 1.14.x 目录，请部署 SnowLuma Runtime"
        )
    if len(candidates) > 1:
        names = ", ".join(item[0] for item in candidates)
        raise SnowLumaLocatorError(
            "ambiguous", f"SnowLuma Runtime 候选不唯一：{names}；请只保留一个目录"
        )
    name, path, version = candidates[0]
    return SnowLumaRuntime(repository, path, name, version)


# Concise aliases make the resolver easy to consume from older callers and
# hidden/third-party checks without creating another discovery implementation.
resolve_runtime = resolve_snowluma_runtime
locate_snowluma_runtime = resolve_snowluma_runtime
discover_snowluma_runtime = resolve_snowluma_runtime
resolve_runtime_directory = resolve_snowluma_runtime
RuntimeResolution = SnowLumaRuntime


def resolve_runtime_path(root: Path | str | None = None) -> Path:
    return resolve_snowluma_runtime(root).path


def resolve_runtime_name(root: Path | str | None = None) -> str:
    return resolve_snowluma_runtime(root).name


def _normalized_endpoint(value: object) -> tuple[str, int, str] | None:
    if not isinstance(value, dict):
        return None
    host = str(value.get("host") or "").strip().casefold()
    if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        return None
    try:
        port = int(value.get("port"))
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    path = str(value.get("path") or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return ("127.0.0.1", port, path.rstrip("/") or "/")


def _credential_qq_account(value: object) -> str | None:
    """Return a safe, non-secret account authority from adapter metadata."""

    if not isinstance(value, str):
        return None
    account = value.strip()
    return account if re.fullmatch(r"\d{5,20}", account) else None


def credential_consistency(
    root: Path | str | None = None,
    *,
    runtime: SnowLumaRuntime | None = None,
) -> dict[str, object]:
    """Compare deployed adapter/OneBot tokens without returning secret data.

    This small standard-library check is also used by the top-level BAT files,
    which run before the WebUI's dependency environment is guaranteed to be
    available.  Only a redacted status and endpoint metadata are returned.
    """

    try:
        selected = runtime or resolve_snowluma_runtime(root)
    except SnowLumaLocatorError as exc:
        return {
            "status": "missing",
            "consistent": False,
            "message": exc.message,
            "endpoint": None,
        }

    repository = selected.root
    adapter_config = repository / "NachoBot-SnowLuma-Adapter" / "config.toml"
    try:
        document = tomllib.loads(adapter_config.read_text(encoding="utf-8"))
        section = document.get("snowluma")
        if not isinstance(section, dict):
            raise ValueError
        token = section.get("token")
        authority_value = section.get("qq_account")
        authority_account = _credential_qq_account(authority_value)
        if authority_value is not None and authority_account is None:
            raise ValueError
        endpoint = _normalized_endpoint(section)
        if not isinstance(token, str) or not token or endpoint is None:
            raise ValueError
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError):
        return {
            "status": "missing",
            "consistent": False,
            "message": "SnowLuma 凭据缺失或配置无效，请重新部署 SnowLuma",
            "endpoint": None,
        }

    matched = 0
    missing = False
    mismatch = False
    correct = False
    config_dir = selected.path / "config"
    if authority_account is not None:
        config_files = (config_dir / f"onebot_{authority_account}.json",)
    else:
        try:
            config_files = sorted(
                config_dir.glob("onebot_*.json"), key=lambda item: item.name.casefold()
            )
        except OSError:
            config_files = []
    for config_path in config_files:
        try:
            onebot = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(onebot, dict):
            continue
        networks = onebot.get("networks")
        servers = networks.get("wsServers") if isinstance(networks, dict) else None
        if not isinstance(servers, list):
            continue
        for server in servers:
            if (
                not isinstance(server, dict)
                # SnowLuma disables an adapter only for literal false;
                # omitted/legacy values are enabled by the runtime.
                or server.get("enabled") is False
                or _normalized_endpoint(server) != endpoint
            ):
                continue
            matched += 1
            candidate = server.get("accessToken")
            if not isinstance(candidate, str) or not candidate:
                missing = True
            elif candidate == token:
                correct = True
            elif candidate != token:
                mismatch = True

    if mismatch and authority_account is not None:
        status = "mismatch"
        message = "SnowLuma 适配器与 OneBot 凭据不一致，请重新部署 SnowLuma"
    elif authority_account is not None and (missing or matched == 0):
        status = "missing"
        message = "SnowLuma OneBot 凭据缺失，请重新部署 SnowLuma"
    elif authority_account is not None and correct:
        status = "ok"
        message = "ok"
    elif authority_account is None and correct:
        status = "ok"
        message = "ok"
    elif authority_account is None and mismatch:
        status = "mismatch"
        message = "SnowLuma 适配器与 OneBot 凭据不一致，请重新部署 SnowLuma"
    else:
        status = "missing"
        message = "SnowLuma OneBot 凭据缺失，请重新部署 SnowLuma"
    return {
        "status": status,
        "consistent": status == "ok",
        "message": message,
        "endpoint": {"host": endpoint[0], "port": endpoint[1], "path": endpoint[2]},
        "matched_servers": matched,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve the repository-local SnowLuma 1.14.x runtime")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--field",
        choices=("path", "name", "version", "json"),
        default="path",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--check-credentials",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--runtime-path",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        runtime = resolve_snowluma_runtime(args.root)
    except SnowLumaLocatorError as exc:
        print(exc.message, file=sys.stderr)
        return 2
    if args.check_credentials:
        if args.runtime_path:
            try:
                supplied = Path(args.runtime_path).resolve()
            except OSError:
                print("SnowLuma Runtime 路径无效，请重新部署 SnowLuma", file=sys.stderr)
                return 2
            if supplied != runtime.path:
                print("SnowLuma Runtime 路径与权威解析不一致，请重新部署 SnowLuma", file=sys.stderr)
                return 2
        status = credential_consistency(args.root, runtime=runtime)
        if not status.get("consistent"):
            print(str(status.get("message") or "SnowLuma 凭据缺失，请重新部署 SnowLuma"), file=sys.stderr)
            return 3
        print("ok")
        return 0
    if args.field == "path":
        print(runtime.path)
    elif args.field == "name":
        print(runtime.name)
    elif args.field == "version":
        print(runtime.version)
    else:
        print(json.dumps(runtime.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by BAT launchers
    raise SystemExit(main())


__all__ = [
    "SUPPORTED_VERSION_PREFIX",
    "SnowLumaLocatorError",
    "SnowLumaRuntime",
    "RuntimeResolution",
    "credential_consistency",
    "discover_snowluma_runtime",
    "locate_snowluma_runtime",
    "main",
    "resolve_runtime",
    "resolve_runtime_name",
    "resolve_runtime_path",
    "resolve_snowluma_runtime",
    "resolve_runtime_directory",
]
