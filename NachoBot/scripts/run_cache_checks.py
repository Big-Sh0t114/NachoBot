#!/usr/bin/env python3
"""Run cache-focused unittest modules from a disposable source-tree copy.

The child process uses the NachoBot virtual-environment interpreter, but its
working tree contains only copied source, tests, templates, and the artwork
plugin. It never reads the checkout's live config, data, logs, or credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


DEFAULT_MODULES = (
    "tests.test_graph_cache_revisions",
    "tests.test_media_conversion_cache",
    "tests.test_artwork_selection",
    "tests.test_media_cache_cleanup",
    "tests.test_image_cache_restore",
    "tests.test_image_memory_bounds",
    "tests.test_llm_cache_usage",
    "tests.test_llm_cache_diagnostics",
    "tests.test_result_cache_lifetime",
    "tests.test_prompt_prefix_order",
)
OPTIONAL_MODULES = ("tests.test_llm_cache_aggregation",)
CONFIG_TEMPLATE_COPIES = (
    ("bot_config_template.toml", "bot_config.toml"),
    ("topics_config_template.toml", "topics_config.toml"),
    ("model_config_template.toml", "model_config.toml"),
    ("mcp_config_template.toml", "mcp_config.toml"),
)
_MODULE_PATTERN = re.compile(r"tests(?:\.[A-Za-z_]\w*)+")
_IGNORED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data",
    "log",
    "logs",
    "node_modules",
    "runtime_data",
    "user_data",
}
_PLUGIN_IGNORED_DIR_NAMES = {
    "cache",
    "config",
    "configs",
    "credentials",
    "data",
    "log",
    "logs",
    "secrets",
}
_SENSITIVE_SUFFIXES = {
    ".db",
    ".env",
    ".key",
    ".log",
    ".p12",
    ".pfx",
    ".pem",
    ".secret",
    ".sqlite",
    ".sqlite3",
    ".token",
}
_SENSITIVE_NAMES = {
    "api_key",
    "api_key.json",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
    "token",
    "token.json",
}


def _is_within(path: Path, root: Path) -> bool:
    """Return true only when path resolves under root on the current platform."""
    try:
        path_text = os.path.normcase(str(path.resolve()))
        root_text = os.path.normcase(str(root.resolve()))
        return os.path.commonpath((path_text, root_text)) == root_text
    except (OSError, ValueError):
        return False


def _require_within(path: Path, root: Path, label: str) -> None:
    if not _is_within(path, root):
        raise RuntimeError(f"{label} escapes its allowed root: {path}")


def _is_symlink_or_junction(entry: os.DirEntry[str], path: Path) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _is_path_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _is_sensitive_file(name: str) -> bool:
    lowered = name.lower()
    suffix = Path(lowered).suffix
    return (
        lowered in _SENSITIVE_NAMES
        or lowered.startswith(".env")
        or suffix in _SENSITIVE_SUFFIXES
        or suffix == ".jsonl"
        or ".db" in lowered
        or ".sqlite" in lowered
        or ".log" in lowered
        or "credential" in lowered
        or "secret" in lowered
        or "private_key" in lowered
        or "_token." in lowered
    )


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    containment_root: Path,
    source_root: Path,
    plugin_fixture: bool = False,
) -> tuple[int, int]:
    """Copy regular files only, never following links or creating hardlinks."""
    if _is_path_link_or_junction(source):
        raise RuntimeError(f"Refusing to copy a linked source directory: {source}")
    source = source.resolve(strict=True)
    _require_within(source, source_root, "copy source")
    _require_within(destination, containment_root, "copy destination")
    if not source.is_dir():
        raise RuntimeError(f"Expected a real source directory: {source}")

    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for entry in sorted(os.scandir(source), key=lambda item: item.name.lower()):
        source_path = Path(entry.path)
        target_path = destination / entry.name
        _require_within(target_path, containment_root, "copy target")

        if _is_symlink_or_junction(entry, source_path):
            skipped += 1
            continue

        lowered = entry.name.lower()
        if entry.is_dir(follow_symlinks=False):
            if lowered in _IGNORED_DIR_NAMES or (
                plugin_fixture and lowered in _PLUGIN_IGNORED_DIR_NAMES
            ):
                skipped += 1
                continue
            child_copied, child_skipped = _copy_tree(
                source_path,
                target_path,
                containment_root=containment_root,
                source_root=source_root,
                plugin_fixture=plugin_fixture,
            )
            copied += child_copied
            skipped += child_skipped
            continue

        if not entry.is_file(follow_symlinks=False):
            skipped += 1
            continue
        if _is_sensitive_file(entry.name):
            skipped += 1
            continue
        if plugin_fixture and (
            Path(lowered).suffix in {".toml", ".ini", ".cfg"}
            or "config" in lowered
            or lowered.startswith(("settings", "secret", "credential"))
        ):
            skipped += 1
            continue

        shutil.copyfile(source_path, target_path)
        if os.path.samefile(source_path, target_path):
            target_path.unlink()
            raise RuntimeError(f"Shadow copy unexpectedly shares a file with source: {source_path}")
        copied += 1

    return copied, skipped


def _module_path(shadow_root: Path, module_name: str) -> Path:
    relative = Path(*module_name.split("."))
    module_file = shadow_root / relative.with_suffix(".py")
    package_file = shadow_root / relative / "__init__.py"
    return module_file if module_file.exists() else package_file


def _isolated_environment(
    shadow_root: Path,
    workspace_root: Path,
    venv_root: Path,
) -> dict[str, str]:
    """Keep only basic OS variables and redirect user/config/temp locations."""
    allowed_host_variables = {
        "COMSPEC",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATHEXT",
        "PATH",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
        "PROCESSOR_LEVEL",
        "PROCESSOR_REVISION",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TERM",
        "TZ",
        "WINDIR",
    }
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper not in allowed_host_variables:
            continue
        if upper == "PATH":
            retained = [
                item
                for item in value.split(os.pathsep)
                if item and (not _is_within(Path(item), workspace_root) or _is_within(Path(item), venv_root))
            ]
            venv_scripts = venv_root / "Scripts"
            if not any(os.path.normcase(os.path.realpath(item)) == os.path.normcase(str(venv_scripts)) for item in retained):
                retained.insert(0, str(venv_scripts))
            value = os.pathsep.join(retained)
        env[name] = value

    home = shadow_root / ".userprofile"
    appdata = home / "AppData" / "Roaming"
    local_appdata = home / "AppData" / "Local"
    temp_dir = shadow_root / ".tmp"
    config_dir = home / ".config"
    data_dir = home / ".local" / "share"
    cache_dir = home / ".cache"
    state_dir = home / ".local" / "state"
    for path in (
        home,
        appdata,
        local_appdata,
        temp_dir,
        config_dir,
        data_dir,
        cache_dir,
        state_dir,
        shadow_root / ".programdata",
    ):
        _require_within(path, shadow_root, "isolated user directory")
        path.mkdir(parents=True, exist_ok=True)

    env.update(
        {
            "APPDATA": str(appdata),
            "HOME": str(home),
            "LOCALAPPDATA": str(local_appdata),
            "NACHOBOT_CACHE_VENV_ROOT": str(venv_root),
            "PROGRAMDATA": str(shadow_root / ".programdata"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(shadow_root),
            "TEMP": str(temp_dir),
            "TMP": str(temp_dir),
            "TMPDIR": str(temp_dir),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(cache_dir),
            "XDG_CONFIG_HOME": str(config_dir),
            "XDG_DATA_HOME": str(data_dir),
            "XDG_STATE_HOME": str(state_dir),
        }
    )
    return env


def _child_bootstrap() -> str:
    """Run explicit unittest modules and require a nonempty successful result."""
    return r'''
import os
import sys
import unittest

shadow_root = os.path.realpath(os.environ["NACHOBOT_CACHE_SHADOW_ROOT"])
workspace_root = os.path.realpath(os.environ["NACHOBOT_CACHE_WORKSPACE_ROOT"])
venv_root = os.path.realpath(os.environ["NACHOBOT_CACHE_VENV_ROOT"])

def inside(path, root):
    try:
        path = os.path.normcase(os.path.realpath(path or os.getcwd()))
        root = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath((path, root)) == root
    except (OSError, ValueError):
        return False

sys.path[:] = [
    entry for entry in sys.path
    if not inside(entry, workspace_root) or inside(entry, venv_root)
]
sys.path.insert(0, shadow_root)
module_names = sys.argv[1:]
try:
    suite = unittest.defaultTestLoader.loadTestsFromNames(module_names)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
except SystemExit as exc:
    print(f"Cache check aborted during test import or execution: SystemExit({exc.code})", file=sys.stderr)
    raise SystemExit(2) from exc

if result.testsRun <= 0:
    print("Cache check failed: unittest ran no tests", file=sys.stderr)
    raise SystemExit(2)
if not result.wasSuccessful():
    raise SystemExit(1)
print(f"NACHOBOT_CACHE_CHECKS_OK: tests_run={result.testsRun}", flush=True)
'''


def _select_modules(args: argparse.Namespace) -> tuple[str, ...]:
    if args.modules:
        modules = list(args.modules)
    else:
        modules = list(DEFAULT_MODULES)
    if args.include_aggregation and OPTIONAL_MODULES[0] not in modules:
        modules.extend(OPTIONAL_MODULES)
    invalid = [name for name in modules if not _MODULE_PATTERN.fullmatch(name)]
    if invalid:
        raise ValueError(
            "Only dotted tests.* module names are accepted: " + ", ".join(invalid)
        )
    return tuple(modules)


def _venv_interpreter(repo_root: Path) -> Path:
    expected = repo_root / ".venv" / "Scripts" / "python.exe"
    if not expected.is_file():
        raise RuntimeError(f"Project interpreter is missing: {expected}")
    try:
        matches = os.path.samefile(sys.executable, expected)
    except OSError:
        matches = os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(
            os.path.realpath(expected)
        )
    if not matches:
        raise RuntimeError(
            "Run this script with the project interpreter: "
            f"{expected} (current: {sys.executable})"
        )
    return expected.resolve()


def _copy_shadow_tree(repo_root: Path, shadow_root: Path) -> tuple[int, int]:
    _require_within(shadow_root, shadow_root.parent, "shadow root")
    sources: Iterable[tuple[Path, Path, bool]] = (
        (repo_root / "src", shadow_root / "src", False),
        (repo_root / "tests", shadow_root / "tests", False),
        (repo_root / "template", shadow_root / "template", False),
        (
            repo_root / "plugins" / "artwork_plugin",
            shadow_root / "plugins" / "artwork_plugin",
            True,
        ),
    )

    copied = 0
    skipped = 0
    for source, destination, plugin_fixture in sources:
        if not source.is_dir():
            raise RuntimeError(f"Required source tree is missing: {source}")
        _require_within(destination, shadow_root, "shadow tree destination")
        added, ignored = _copy_tree(
            source,
            destination,
            containment_root=shadow_root,
            source_root=repo_root,
            plugin_fixture=plugin_fixture,
        )
        copied += added
        skipped += ignored

    tests_init = shadow_root / "tests" / "__init__.py"
    if not tests_init.exists():
        _require_within(tests_init, shadow_root, "generated test package file")
        tests_init.write_text('"""Disposable cache-check test package."""\n', encoding="utf-8")
        copied += 1

    return copied, skipped


def _seed_template_config(shadow_root: Path) -> int:
    """Copy only public config templates to isolated config paths for selected suites."""
    template_root = shadow_root / "template"
    config_root = shadow_root / "config"
    _require_within(template_root, shadow_root, "shadow template directory")
    _require_within(config_root, shadow_root, "shadow config directory")
    config_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    for template_name, config_name in CONFIG_TEMPLATE_COPIES:
        source = template_root / template_name
        destination = config_root / config_name
        _require_within(source, shadow_root, "shadow config template")
        _require_within(destination, shadow_root, "shadow config destination")
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"Required public config template is missing or linked: {source}")
        shutil.copyfile(source, destination)
        copied += 1
    return copied


@contextlib.contextmanager
def _verified_temporary_root(temp_base: Path, workspace_root: Path):
    """Keep temp creation and cleanup scoped to a verified external directory."""
    with tempfile.TemporaryDirectory(
        prefix="nachobot-cache-checks-",
        dir=str(temp_base),
    ) as temporary_directory:
        temporary_root = Path(temporary_directory).resolve()
        _require_within(temporary_root, temp_base, "temporary root")
        if _is_within(temporary_root, workspace_root):
            raise RuntimeError(f"Temporary shadow path is inside the checkout: {temporary_root}")
        try:
            yield temporary_root
        finally:
            _require_within(temporary_root, temp_base, "temporary root before cleanup")
            if _is_within(temporary_root, workspace_root):
                raise RuntimeError(f"Temporary shadow path moved into the checkout: {temporary_root}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "modules",
        nargs="*",
        help="explicit dotted unittest modules (default: the cache-focused suite)",
    )
    parser.add_argument(
        "--include-aggregation",
        action="store_true",
        help="also run tests.test_llm_cache_aggregation with the default suite",
    )
    parser.add_argument(
        "--with-template-config",
        action="store_true",
        help="seed shadow/config from only the four copied public TOML config templates",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        metavar="SECONDS",
        help="maximum child process duration (default: 120)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list the default modules without creating a shadow tree",
    )
    args = parser.parse_args(argv)

    if args.list:
        for module_name in DEFAULT_MODULES:
            print(module_name)
        for module_name in OPTIONAL_MODULES:
            print(f"optional: {module_name}")
        return 0
    if args.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds")

    try:
        modules = _select_modules(args)
        repo_root = Path(__file__).resolve().parents[1]
        workspace_root = repo_root.parent.resolve()
        interpreter = _venv_interpreter(repo_root)
        temp_base = Path(tempfile.gettempdir()).resolve()
        if _is_within(temp_base, workspace_root):
            raise RuntimeError(
                f"System temp directory is inside the checkout; refusing to create a shadow tree: {temp_base}"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Cache check runner: {exc}", file=sys.stderr)
        return 2

    try:
        with _verified_temporary_root(temp_base, workspace_root) as temporary_root:
            shadow_root = temporary_root / "NachoBot"
            _require_within(shadow_root, temporary_root, "shadow root")
            shadow_root.mkdir()
            copied_files, skipped_files = _copy_shadow_tree(repo_root, shadow_root)
            seeded_configs = _seed_template_config(shadow_root) if args.with_template_config else 0

            missing = [name for name in modules if not _module_path(shadow_root, name).is_file()]
            env = _isolated_environment(
                shadow_root,
                workspace_root,
                repo_root / ".venv",
            )
            env["PYTHONIOENCODING"] = "utf-8"
            env["NACHOBOT_CACHE_SHADOW_ROOT"] = str(shadow_root)
            env["NACHOBOT_CACHE_WORKSPACE_ROOT"] = str(workspace_root)

            command = [
                str(interpreter),
                "-P",
                "-s",
                "-c",
                _child_bootstrap(),
                *modules,
            ]
            print(f"Shadow tree: {shadow_root}", flush=True)
            print(f"Requested modules: {len(modules)}", flush=True)
            print(f"Copied files: {copied_files}; skipped files/directories: {skipped_files}", flush=True)
            if args.with_template_config:
                print(f"Seeded shadow config templates: {seeded_configs}", flush=True)
            if missing:
                print(
                    "Requested modules missing from this checkout: " + ", ".join(missing),
                    file=sys.stderr,
                    flush=True,
                )
            try:
                result = subprocess.run(
                    command,
                    cwd=shadow_root,
                    env=env,
                    timeout=args.timeout,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except subprocess.TimeoutExpired:
                print(
                    f"Cache check subprocess exceeded {args.timeout} seconds.",
                    file=sys.stderr,
                )
                return 124
            except OSError as exc:
                print(f"Could not launch cache check subprocess: {exc}", file=sys.stderr)
                return 2
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", file=sys.stderr)
            success_marker = re.search(r"(?m)^NACHOBOT_CACHE_CHECKS_OK: tests_run=([1-9]\d*)$", result.stdout or "")
            if result.returncode != 0:
                return result.returncode
            if success_marker is None:
                print(
                    "Cache check failed: child exited without a successful test-count sentinel",
                    file=sys.stderr,
                )
                return 2
            print(f"Verified unittest count: {success_marker.group(1)}", flush=True)
            return 0
    except (OSError, RuntimeError) as exc:
        print(f"Cache check runner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
