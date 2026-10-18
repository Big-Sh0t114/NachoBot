"""Transactional model deployment into the native MaiSelf plugin config."""

from __future__ import annotations

import hashlib
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version
import tomlkit
from tomlkit.items import InlineTable, Table
from tomlkit.toml_document import TOMLDocument

CORE_PATH = Path(__file__).resolve().parents[2]
if str(CORE_PATH) not in sys.path:
    sys.path.insert(0, str(CORE_PATH))

from plugins.mai_recog_self.backend.control import DataLock, _activate_locked, _worker_python  # noqa: E402
from plugins.mai_recog_self.backend.environment import worker_environment  # noqa: E402
from plugins.mai_recog_self.backend.images import MAX_BYTES, decode  # noqa: E402
from plugins.mai_recog_self.backend.labels import metadata_digest, validate_class_name, validate_multiclass_metadata  # noqa: E402
from plugins.mai_recog_self.backend.paths import CORE_ROOT, DEFAULT_DATA_ROOT  # noqa: E402
from plugins.mai_recog_self.backend.runtime import InferenceService  # noqa: E402
from plugins.mai_recog_self.backend.store import Store, digest  # noqa: E402


DEFAULT_PLUGIN_CONFIG = Path(__file__).resolve().with_name("config.toml")
PACKAGE_PROBE_TIMEOUT = 120.0
CONFIG_LOCK_ROOT = Path(tempfile.gettempdir()) / "nachobot-self-recognition-deploy-locks"
_WORKER_IMPORTS = {
    "torch": "torch",
    "torchvision": "torchvision",
    "Pillow": "PIL",
    "numpy": "numpy",
    "psutil": "psutil",
}
_SMALL_WORKER_PACKAGES = {"Pillow", "numpy", "psutil"}

_PACKAGE_PROBE = r"""
import importlib, json

imports = {"torch": "torch", "torchvision": "torchvision", "Pillow": "PIL", "numpy": "numpy", "psutil": "psutil"}
packages = {}
for distribution, module_name in imports.items():
    try:
        module = importlib.import_module(module_name)
        version = getattr(module, "__version__", "")
        packages[distribution] = {
            "version": str(version),
            "path": str(getattr(module, "__file__", "")),
            "missing": False,
            "error": "" if version else "module has no __version__",
        }
    except Exception as exc:
        missing = isinstance(exc, ModuleNotFoundError) and exc.name == module_name
        packages[distribution] = {
            "version": "",
            "path": "",
            "missing": missing,
            "missing_dependency": str(getattr(exc, "name", "") or ""),
            "error": f"{type(exc).__name__}: {exc}",
        }
print(json.dumps({"packages": packages}, ensure_ascii=False))
"""

_PACKAGE_DISCOVERY = r"""
import importlib.metadata, importlib.util, json

imports = {"torch": "torch", "torchvision": "torchvision", "Pillow": "PIL", "numpy": "numpy", "psutil": "psutil"}
packages = {}
for distribution, module_name in imports.items():
    try:
        spec = importlib.util.find_spec(module_name)
        present = spec is not None
        path = str(spec.origin or "") if spec is not None else ""
        error = ""
    except Exception as exc:
        present = False
        path = ""
        error = f"{type(exc).__name__}: {exc}"
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = ""
    except Exception as exc:
        version = ""
        error = error or f"{type(exc).__name__}: {exc}"
    packages[distribution] = {"present": present, "version": str(version), "path": path, "error": error}
print(json.dumps({"packages": packages}, ensure_ascii=False))
"""


class DeploymentError(RuntimeError):
    """A preflight or commit failure that must leave deployment state intact."""


def _load_requirements() -> dict[str, Requirement]:
    requirements: dict[str, Requirement] = {}
    path = Path(__file__).resolve().parent / "backend" / "requirements-worker.txt"
    for line in path.read_text("utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            requirement = Requirement(value)
            requirements[requirement.name] = requirement
    missing = set(_WORKER_IMPORTS) - set(requirements)
    if missing:
        raise DeploymentError(f"worker依赖清单缺少包定义：{', '.join(sorted(missing))}")
    return requirements


def _run_process(command: list[str], *, root: Path, timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        return subprocess.run(
            command,
            cwd=CORE_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=flags,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DeploymentError(f"子进程超时：{command[0]}") from exc


def _probe_worker(worker_python: str, root: Path) -> dict[str, dict]:
    result = _run_process(
        [worker_python, "-X", "utf8", "-c", _PACKAGE_PROBE],
        root=root,
        timeout=PACKAGE_PROBE_TIMEOUT,
        env=worker_environment(root),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit={result.returncode}")[-3000:]
        raise DeploymentError(f"worker依赖探测进程失败：{detail}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        packages = payload["packages"]
        if set(packages) != set(_WORKER_IMPORTS):
            raise ValueError("package list mismatch")
        return packages
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentError("worker依赖探测没有返回完整JSON") from exc


def _discover_worker_packages(worker_python: str, root: Path) -> dict[str, dict]:
    result = _run_process(
        [worker_python, "-X", "utf8", "-c", _PACKAGE_DISCOVERY],
        root=root,
        timeout=PACKAGE_PROBE_TIMEOUT,
        env=worker_environment(root),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or f"exit={result.returncode}")[-3000:]
        raise DeploymentError(f"worker依赖发现进程失败：{detail}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        packages = payload["packages"]
        if set(packages) != set(_WORKER_IMPORTS):
            raise ValueError("package list mismatch")
        return packages
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise DeploymentError("worker依赖发现没有返回完整JSON") from exc


def _validate_package_version(distribution: str, version_text: str, requirements: dict[str, Requirement]) -> Version:
    try:
        version = Version(version_text)
    except InvalidVersion as exc:
        raise DeploymentError(f"worker中的{distribution}版本无法解析：{version_text!r}。请修复该环境后重试。") from exc
    requirement = requirements[distribution]
    if requirement.specifier and version not in requirement.specifier:
        raise DeploymentError(
            f"worker中的{distribution}版本{version_text}不满足{requirement.specifier}。请切换到兼容worker环境；部署不会升级现有包。"
        )
    return version


def _check_discovered_packages(
    packages: dict[str, dict], requirements: dict[str, Requirement]
) -> list[str]:
    missing: list[str] = []
    for distribution in _WORKER_IMPORTS:
        value = packages.get(distribution) or {}
        if value.get("error"):
            raise DeploymentError(f"无法发现worker中的{distribution}：{value['error']}。请修复该环境后重试。")
        version_text = value.get("version") or ""
        if version_text:
            _validate_package_version(distribution, version_text, requirements)
        if not value.get("present"):
            if version_text:
                raise DeploymentError(
                    f"worker已安装{distribution} {version_text}但找不到其模块；请修复所选worker环境，部署不会覆盖损坏的已安装包。"
                )
            if distribution in {"torch", "torchvision"}:
                raise DeploymentError(
                    f"worker缺少{distribution}。请先准备已有兼容的Torch/torchvision环境并用--worker-python指向它；部署不会下载或重建大型框架。"
                )
            missing.append(distribution)
            continue
    return missing


def _check_preinstall_worker_packages(
    packages: dict[str, dict],
    discovered: dict[str, dict],
    requirements: dict[str, Requirement],
    missing_small: list[str],
) -> None:
    """Reject real import/version failures; defer only failures caused by known absent small packages."""
    missing_small_set = set(missing_small)
    for distribution in _WORKER_IMPORTS:
        value = packages.get(distribution) or {}
        discovery = discovered.get(distribution) or {}
        error = value.get("error") or ""
        if not discovery.get("present"):
            if distribution not in _SMALL_WORKER_PACKAGES or not value.get("missing"):
                raise DeploymentError(f"worker中的{distribution}不可用；请先修复所选worker环境。")
            continue
        if error:
            missing_module = value.get("missing_dependency") or ""
            causing_dependency = next(
                (
                    candidate
                    for candidate, candidate_module in _WORKER_IMPORTS.items()
                    if candidate in _SMALL_WORKER_PACKAGES
                    and (missing_module == candidate_module or missing_module.startswith(candidate_module + "."))
                ),
                None,
            )
            if causing_dependency not in missing_small_set:
                raise DeploymentError(
                    f"worker中的{distribution}导入失败（{error}）。请修复所选worker环境后重试；部署不会升级已存在的包。"
                )
        version_text = value.get("version") or discovery.get("version") or ""
        if not version_text:
            raise DeploymentError(
                f"worker中的{distribution}没有可验证的版本信息。请修复所选worker环境后重试；部署不会升级已存在的包。"
            )
        _validate_package_version(distribution, version_text, requirements)


def _check_worker_packages(packages: dict[str, dict], requirements: dict[str, Requirement], root: Path) -> tuple[list[str], dict[str, str]]:
    missing: list[str] = []
    versions: dict[str, str] = {}
    overlay = (root.resolve() / "worker_deps").resolve()
    for distribution in _WORKER_IMPORTS:
        value = packages.get(distribution) or {}
        if value.get("missing"):
            missing.append(distribution)
            continue
        error = value.get("error")
        version_text = value.get("version")
        if error or not version_text:
            raise DeploymentError(
                f"worker中的{distribution}导入失败（{error or '没有版本信息'}）。请修复所选worker环境后重试；部署不会升级已存在的包。"
            )
        _validate_package_version(distribution, version_text, requirements)
        versions[distribution] = version_text
        module_path = value.get("path") or ""
        if module_path:
            try:
                if Path(module_path).resolve().is_relative_to(overlay):
                    versions[f"{distribution}_source"] = str(Path(module_path).resolve())
            except (OSError, ValueError):
                pass
    return missing, versions


def _install_missing_packages(
    missing: list[str], worker_python: str, root: Path, requirements: dict[str, Requirement], changes: list[dict]
) -> list[dict]:
    heavy = sorted(set(missing) - _SMALL_WORKER_PACKAGES)
    if heavy:
        names = ", ".join(heavy)
        raise DeploymentError(
            f"worker缺少{names}。请先准备已有兼容的Torch/torchvision环境并用--worker-python指向它；部署不会下载或重建大型框架。"
        )
    installer = shutil.which("uv")
    env = worker_environment(root)
    if installer:
        prefix = [installer, "pip", "install", "--python", worker_python, "--target", str(root.resolve() / "worker_deps"), "--no-deps"]
        installer_name = "uv"
    else:
        pip_check = _run_process(
            [worker_python, "-m", "pip", "--version"], root=root, timeout=20, env=env
        )
        if pip_check.returncode:
            raise DeploymentError("PATH中没有uv，且所选worker没有pip；请安装uv或在worker中提供pip后重试。")
        prefix = [worker_python, "-m", "pip", "install", "--target", str(root.resolve() / "worker_deps"), "--no-deps"]
        installer_name = "pip"

    installed: list[dict] = []
    for distribution in missing:
        requirement = str(requirements[distribution])
        command = [*prefix, requirement]
        result = _run_process(command, root=root, timeout=300, env=env)
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit={result.returncode}")[-2500:]
            raise DeploymentError(f"安装缺失的{distribution}到worker_deps失败：{detail}")
        change = {
            "package": distribution,
            "requirement": requirement,
            "target": str(root.resolve() / "worker_deps"),
            "installer": installer_name,
        }
        changes.append(change)
        installed.append(change)
    return installed


def _ensure_worker_dependencies(
    worker_python: str, root: Path, install_missing: bool, changes: list[dict]
) -> dict[str, str]:
    requirements = _load_requirements()
    discovered = _discover_worker_packages(worker_python, root)
    missing = _check_discovered_packages(discovered, requirements)
    packages = _probe_worker(worker_python, root)
    if not missing:
        missing_from_import, versions = _check_worker_packages(packages, requirements, root)
        if missing_from_import:
            names = ", ".join(missing_from_import)
            raise DeploymentError(f"worker已发现{names}但导入时缺失；请修复所选worker环境后重试。")
        return versions

    _check_preinstall_worker_packages(packages, discovered, requirements, missing)
    if not install_missing:
        raise DeploymentError(
            f"worker缺少{', '.join(missing)}。确认允许时请加--install-missing，将只把缺失的小依赖安装到{root.resolve() / 'worker_deps'}。"
        )
    _install_missing_packages(missing, worker_python, root, requirements, changes)
    packages = _probe_worker(worker_python, root)
    missing_after, versions = _check_worker_packages(packages, requirements, root)
    if missing_after:
        raise DeploymentError(f"worker_deps安装后仍缺少：{', '.join(missing_after)}")
    return versions


def _select_worker(store: Store, explicit: str) -> str:
    try:
        return _worker_python(store, explicit)
    except ValueError as exc:
        raise DeploymentError(str(exc)) from exc


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise DeploymentError(f"{label}无效：{path}") from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"{label}必须是JSON对象：{path}")
    return value


def _preflight_model(root: Path, version: str, self_character_id: str, store: Store | None = None) -> tuple[Store, dict, dict]:
    if not isinstance(version, str) or len(version) != 32 or any(char not in "0123456789abcdef" for char in version):
        raise DeploymentError("version必须是32位小写十六进制模型ID")
    if not isinstance(self_character_id, str):
        raise DeploymentError("self-character-id必须是文本类别ID")
    try:
        validated_id = validate_class_name(self_character_id)
    except ValueError as exc:
        raise DeploymentError(f"self-character-id无效：{exc}") from exc
    if validated_id != self_character_id or self_character_id == "other":
        raise DeploymentError("self-character-id必须是精确匹配的角色类别ID，且不能是other")

    store = store or Store(root, initialize=False, readonly=True)
    from plugins.mai_recog_self.backend.worker import _version_artifact

    weights_path, model_sha = _version_artifact(store, version)
    record = next(row for row in store.models() if row["id"] == version)
    job_id = record.get("job_id") or ""
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise DeploymentError("模型关联的训练任务ID无效")
    snapshot = _read_json(store.root / "jobs" / job_id / "snapshot.json", "冻结训练快照")
    audit = _read_json(weights_path.parent / "audit.json", "模型审计")
    try:
        job = store.job(job_id)
        model_metrics = json.loads(record["metrics"])
    except (ValueError, TypeError) as exc:
        raise DeploymentError("模型数据库记录或任务记录无效") from exc
    if digest(snapshot) != job.get("snapshot_sha") or audit.get("snapshot_sha") != job.get("snapshot_sha"):
        raise DeploymentError("冻结训练快照SHA与任务/审计记录不一致")

    metadata_values = {
        "format_version": snapshot.get("format_version"),
        "task": snapshot.get("task"),
        "class_names": snapshot.get("class_names"),
        "score_threshold": snapshot.get("score_threshold"),
        "margin_threshold": snapshot.get("margin_threshold"),
    }
    try:
        metadata = validate_multiclass_metadata(metadata_values)
    except ValueError as exc:
        raise DeploymentError(f"目标模型不是有效的v2多角色模型：{exc}") from exc
    input_size = snapshot.get("input_size")
    if type(input_size) is not int or input_size not in {224, 320, 448}:
        raise DeploymentError("冻结训练快照的图片尺寸无效")
    metadata_sha = metadata_digest({**metadata, "input_size": input_size})
    if snapshot.get("metadata_sha256") != metadata_sha:
        raise DeploymentError("冻结训练快照的类别元数据摘要无效")
    expected_fields = {
        "task": "multiclass",
        "format_version": 2,
        "class_names": metadata["class_names"],
        "score_threshold": metadata["score_threshold"],
        "margin_threshold": metadata["margin_threshold"],
        "metadata_sha256": metadata_sha,
    }
    for field, expected in expected_fields.items():
        if audit.get(field) != expected or model_metrics.get(field) != expected:
            raise DeploymentError(f"模型快照/审计/数据库的类别映射不一致：{field}")
    if model_metrics.get("input_size") != input_size:
        raise DeploymentError("模型数据库记录的图片尺寸与冻结快照不一致")
    if self_character_id not in metadata["class_names"]:
        raise DeploymentError(
            f"self-character-id大小写敏感，{self_character_id!r}不在模型类别中：{metadata['class_names']}"
        )
    return store, {
        "version": version,
        "sha256": model_sha,
        "job_id": job_id,
        "metadata": metadata,
        "snapshot": snapshot,
    }, record


def _read_probe(root: Path, model: dict, self_character_id: str, probe_image: Path | None) -> tuple[bytes, dict]:
    snapshot = model["snapshot"]
    if probe_image is not None:
        path = Path(probe_image).expanduser().resolve()
        source = "user"
        expected_sha = None
    else:
        candidates = [
            row
            for row in snapshot.get("images", [])
            if isinstance(row, dict) and row.get("label") == self_character_id and row.get("split") == "train"
        ]
        candidates.sort(key=lambda row: str(row.get("id", "")))
        if not candidates:
            raise DeploymentError(f"冻结训练快照中没有{self_character_id!r}类别的TRAIN探针图片")
        row = candidates[0]
        relative = Path(str(row.get("path", "")))
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise DeploymentError("冻结探针图片路径越出数据目录") from exc
        expected_sha = row.get("sha")
        source = "snapshot_train"
    if not path.is_file():
        raise DeploymentError(f"推理探针图片不存在：{path}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise DeploymentError("推理探针图片为空或超过20MiB")
    actual_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise DeploymentError("冻结TRAIN探针图片SHA与快照不符")
    try:
        decode(raw)
    except (OSError, ValueError) as exc:
        raise DeploymentError(f"推理探针图片不符合后端图片限制：{exc}") from exc
    return raw, {"path": str(path), "source": source, "sha256": actual_sha}


def _config_lock_root(path: Path) -> Path:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return CONFIG_LOCK_ROOT / key


def _current_bytes(path: Path) -> bytes | None:
    try:
        if not path.exists():
            return None
        if not path.is_file():
            raise DeploymentError(f"插件配置目标不是文件：{path}")
        return path.read_bytes()
    except OSError as exc:
        raise DeploymentError(f"无法读取插件配置：{path}: {exc}") from exc


def _load_config(path: Path) -> dict:
    original = _current_bytes(path)
    existed = original is not None
    bom = bool(original and original.startswith(b"\xef\xbb\xbf"))
    raw_text = original.decode("utf-8-sig") if original is not None else ""
    newline = "\r\n" if "\r\n" in raw_text else "\n"
    trailing_newline = raw_text.endswith(("\n", "\r"))
    try:
        document = tomlkit.parse(raw_text) if existed else tomlkit.document()
    except Exception as exc:
        raise DeploymentError(f"插件配置TOML格式错误，拒绝覆盖：{path}: {exc}") from exc
    if not isinstance(document, TOMLDocument):
        raise DeploymentError(f"插件配置顶层必须是TOML表：{path}")
    for table_name in ("plugin", "recognition"):
        if table_name in document and not isinstance(document[table_name], (Table, InlineTable)):
            raise DeploymentError(f"插件配置中的[{table_name}]必须是TOML表：{path}")
    return {
        "path": path,
        "original": original,
        "document": document,
        "bom": bom,
        "newline": newline,
        "trailing_newline": trailing_newline,
    }


def _render_config(snapshot: dict, root: Path, self_character_id: str, worker_python: str) -> bytes:
    document = snapshot["document"]
    if "plugin" not in document:
        document["plugin"] = tomlkit.table()
    if "recognition" not in document:
        document["recognition"] = tomlkit.table()
    plugin = document["plugin"]
    recognition = document["recognition"]
    plugin["enabled"] = True
    recognition["data_dir"] = str(root.resolve())
    recognition["self_character_id"] = self_character_id
    if worker_python:
        recognition["worker_python"] = worker_python
    rendered = tomlkit.dumps(document).replace("\r\n", "\n").encode("utf-8")
    if snapshot["newline"] == "\r\n":
        rendered = rendered.replace(b"\n", b"\r\n")
    if not snapshot["trailing_newline"]:
        rendered = rendered.rstrip(b"\r\n")
    if snapshot["bom"]:
        rendered = b"\xef\xbb\xbf" + rendered
    return rendered


def _configured_worker_override(snapshot: dict) -> str:
    recognition = snapshot["document"].get("recognition")
    if recognition is None:
        return ""
    value = recognition.get("worker_python", "")
    if not isinstance(value, str):
        raise DeploymentError("插件配置中的recognition.worker_python必须是文本")
    return value.strip()


def _resolve_worker_override(snapshot: dict, explicit: str) -> str:
    return explicit if explicit else _configured_worker_override(snapshot)


def _backup_config(path: Path, original: bytes) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name(f"{path.name}.bak.{stamp}.{uuid.uuid4().hex[:8]}")
    with backup.open("xb") as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    return backup


def _atomic_write_if_unchanged(path: Path, expected: bytes | None, payload: bytes) -> tuple[int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _current_bytes(path) != expected:
            raise DeploymentError("插件配置在部署期间被修改；保留了并发编辑")
        os.replace(temporary, path)
        stat = path.stat()
        return stat.st_dev, stat.st_ino
    finally:
        temporary.unlink(missing_ok=True)


def _commit_config(snapshot: dict, payload: bytes) -> tuple[bool, str | None, tuple[int, int] | None]:
    path = snapshot["path"]
    original = snapshot["original"]
    if _current_bytes(path) != original:
        raise DeploymentError("插件配置在部署期间被修改；保留了并发编辑")
    if payload == original:
        return False, None, None
    backup = _backup_config(path, original) if original is not None else None
    try:
        identity = _atomic_write_if_unchanged(path, original, payload)
    except Exception:
        if backup is not None and _current_bytes(path) == original:
            backup.unlink(missing_ok=True)
        raise
    return True, str(backup) if backup else None, identity


def _restore_config_if_owned(snapshot: dict, payload: bytes, identity: tuple[int, int] | None) -> bool:
    if identity is None:
        return False
    path = snapshot["path"]
    try:
        stat = path.stat()
        if (stat.st_dev, stat.st_ino) != identity or _current_bytes(path) != payload:
            return False
        original = snapshot["original"]
        if original is None:
            path.unlink()
        else:
            _atomic_write_if_unchanged(path, payload, original)
        return True
    except (OSError, DeploymentError):
        return False


def _read_setting_state(store: Store) -> dict[str, str | None]:
    with store.db() as db:
        rows = {row["key"]: row["value"] for row in db.execute(
            "SELECT key,value FROM settings WHERE key IN ('active','previous','worker_python')"
        )}
    return {key: rows.get(key) for key in ("active", "previous", "worker_python")}


def _restore_setting_state(store: Store, before: dict[str, str | None], version: str, worker_python: str) -> list[str]:
    expected = {
        "active": version,
        "previous": before["active"] if before["active"] and before["active"] != version else before["previous"],
        "worker_python": worker_python,
    }
    conflicts: list[str] = []
    with store.db() as db:
        db.execute("BEGIN IMMEDIATE")
        for key, old_value in before.items():
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            current = row[0] if row else None
            if current != expected[key]:
                if current != old_value:
                    conflicts.append(key)
                continue
            if old_value is None:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
            else:
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, old_value))
    return conflicts


def _run_inference_probe(root: Path, worker_python: str, version: str, expected_classes: list[str], raw: bytes, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    service = InferenceService(root, worker_python)
    try:
        while True:
            status = service.status()
            if status.get("desired_version") != version:
                raise DeploymentError(f"推理服务期望版本不符：{status.get('desired_version')!r}")
            if status.get("ready"):
                if status.get("loaded_version") != version:
                    raise DeploymentError(f"推理服务加载版本不符：{status.get('loaded_version')!r}")
                break
            if status.get("last_error") and not status.get("warming"):
                raise DeploymentError(f"推理服务预热失败：{status['last_error']}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"推理worker预热超过{timeout:g}秒")
            time.sleep(0.1)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"推理探针超过{timeout:g}秒")
            result = service.infer(raw, timeout=remaining)
            if result.get("status") == "busy":
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                continue
            break
        if result.get("status") != "ok":
            raise DeploymentError(f"推理探针失败：{json.dumps(result, ensure_ascii=False)}")
        if result.get("version") != version or result.get("loaded_version") != version or result.get("desired_version") != version:
            raise DeploymentError("推理探针返回了意外模型版本")
        if result.get("task") != "multiclass" or result.get("format_version") != 2:
            raise DeploymentError("推理探针没有返回v2多类别结果")
        if result.get("class_names") != expected_classes:
            raise DeploymentError("推理探针类别映射与冻结模型类别不一致")
        scores = result.get("scores")
        if not isinstance(scores, dict) or list(scores) != expected_classes:
            raise DeploymentError("推理探针分数键与v2类别映射不一致")
        predicted = result.get("predicted_class")
        character_id = result.get("character_id")
        if predicted not in expected_classes or character_id is not None and character_id not in expected_classes:
            raise DeploymentError("推理探针返回了未知类别ID")
        return result
    finally:
        service.close()


def deploy(
    root: Path,
    version: str,
    self_character_id: str,
    *,
    worker_python: str = "",
    plugin_config: Path | None = None,
    probe_image: Path | None = None,
    install_missing: bool = False,
    timeout: float = 60.0,
) -> dict:
    """Validate, activate, inference-test, then atomically configure one model."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise DeploymentError("timeout必须是有限且大于0的秒数")
    data_root = Path(root).expanduser().resolve()
    config_path = Path(plugin_config or DEFAULT_PLUGIN_CONFIG).expanduser().resolve()
    config = _load_config(config_path)
    store, model, _record = _preflight_model(data_root, version, self_character_id)
    worker_override = _resolve_worker_override(config, worker_python)
    selected_worker = _select_worker(store, worker_override)
    configured_worker = selected_worker if worker_override else ""
    raw_probe, probe = _read_probe(data_root, model, self_character_id, probe_image)

    dependency_changes: list[dict] = []
    try:
        # The data lock serializes creation of this catalog's durable worker_deps.
        with DataLock(data_root):
            dependency_versions = _ensure_worker_dependencies(selected_worker, data_root, install_missing, dependency_changes)

        with DataLock(_config_lock_root(config_path)):
            with DataLock(data_root):
                if _current_bytes(config_path) != config["original"]:
                    raise DeploymentError("插件配置在部署期间被修改；保留了并发编辑")
                store = Store(data_root, initialize=False)
                _store, locked_model, _locked_record = _preflight_model(data_root, version, self_character_id, store)
                if locked_model["sha256"] != model["sha256"]:
                    raise DeploymentError("目标模型在部署期间发生变化")
                dependency_versions = _ensure_worker_dependencies(selected_worker, data_root, install_missing=False, changes=dependency_changes)
                settings_before = _read_setting_state(store)
                rendered_config = _render_config(config, data_root, self_character_id, configured_worker)
                config_identity: tuple[int, int] | None = None
                config_changed = False
                config_backup_path: str | None = None
                try:
                    activation = _activate_locked(store, version, selected_worker)
                    inference = _run_inference_probe(
                        data_root,
                        selected_worker,
                        version,
                        locked_model["metadata"]["class_names"],
                        raw_probe,
                        float(timeout),
                    )
                    config_changed, config_backup_path, config_identity = _commit_config(config, rendered_config)
                except Exception as exc:
                    _restore_config_if_owned(config, rendered_config, config_identity)
                    conflicts = _restore_setting_state(store, settings_before, version, selected_worker)
                    if isinstance(exc, DeploymentError):
                        error = str(exc)
                    else:
                        error = f"{type(exc).__name__}: {exc}"
                    if conflicts:
                        error += f"; 检测到部署期间并发修改settings：{', '.join(conflicts)}，已保留这些值"
                    if dependency_changes:
                        packages = ", ".join(change["package"] for change in dependency_changes)
                        error += f"; 已安装到worker_deps的依赖保留不回滚：{packages}"
                    raise DeploymentError(error) from exc

        return {
            "status": "deployed",
            "version": version,
            "worker_python": selected_worker,
            "plugin_config": str(config_path),
            "config_changed": config_changed,
            "config_backup_path": config_backup_path,
            "dependency_changes": dependency_changes,
            "worker_dependencies": dependency_versions,
            "probe": probe,
            "activation": activation,
            "inference": inference,
            "restart_required": config_changed,
        }
    except Exception as exc:
        if dependency_changes and "已安装到worker_deps的依赖保留不回滚" not in str(exc):
            packages = ", ".join(change["package"] for change in dependency_changes)
            raise DeploymentError(f"{exc}; 已安装到worker_deps的依赖保留不回滚：{packages}") from exc
        raise


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self._print_message(json.dumps({"ok": False, "error": message}, ensure_ascii=False) + "\n", sys.stderr)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="验证、启用并配置 MaiSelf 插件模型")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_ROOT, help="模型目录")
    parser.add_argument("--worker-python", default="", help="显式 worker Python；省略时使用插件配置路径（若有），再检查同级 Multimodal .venv，最后回退当前 Core Python；忽略目录库保存的旧路径")
    parser.add_argument("--version", required=True, help="要启用的32位模型版本ID")
    parser.add_argument("--self-character-id", required=True, help="区分大小写的模型角色类别ID，不能是other")
    parser.add_argument("--plugin-config", type=Path, default=None, help="插件config.toml；默认使用本目录配置")
    parser.add_argument("--probe-image", type=Path, default=None, help="可选推理探针；默认使用冻结快照中的自身TRAIN图片")
    parser.add_argument("--install-missing", action="store_true", help="只将缺失的小依赖安装到数据目录worker_deps")
    parser.add_argument("--timeout", type=float, default=60.0, help="真实推理探针总预算秒数")
    return parser


def _emit(value: dict, stream=sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str), file=stream, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = deploy(
            args.data_dir.expanduser().resolve(),
            args.version,
            args.self_character_id,
            worker_python=args.worker_python,
            plugin_config=args.plugin_config,
            probe_image=args.probe_image,
            install_missing=args.install_missing,
            timeout=args.timeout,
        )
        _emit({"ok": True, **result})
        return 0
    except Exception as exc:
        _emit({"ok": False, "error": str(exc)}, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
