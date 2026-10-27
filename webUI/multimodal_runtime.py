"""Isolated Multimodal Adapter runtime environments managed by the WebUI."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .setup_checks import ROOT_DIR
    from .install_output import run_install_command
except ImportError:
    from setup_checks import ROOT_DIR
    from install_output import run_install_command


class MultimodalRuntimeManager:
    """Install and resolve the GPU and CPU Multimodal environments."""

    # Schema 1 could be written after merely finding python.exe, allowing a
    # half-created ~100 KB venv to be reported as installed. Schema 2 is only
    # written after _validate_runtime() succeeds.
    SCHEMA_VERSION = 2
    ADAPTER_DIR = ROOT_DIR / "NachoBot-Multimodal-Adapter"
    RUNTIME_DIR = ADAPTER_DIR / ".runtime"
    VALID_PROFILES = ("gpu", "cpu")
    _installing: set[str] = set()
    VALIDATION_TIMEOUT = 300
    VALIDATION_PROGRESS_INTERVAL = 10

    PROFILE_META: dict[str, dict[str, str]] = {
        "gpu": {
            "label": "GPU / CUDA",
            # Keep the historical default environment for CUDA so an existing
            # multi-gigabyte installation is reused instead of duplicated.
            "venv": ".venv",
        },
        "cpu": {
            "label": "CPU",
            "venv": ".venv-cpu",
        },
    }

    @classmethod
    def normalize_profile(cls, profile: str | None) -> str:
        value = str(profile or "").strip().lower()
        if value not in cls.VALID_PROFILES:
            raise ValueError(f"未知 Multimodal 环境: {profile}")
        return value

    @classmethod
    def env_dir(cls, profile: str) -> Path:
        profile = cls.normalize_profile(profile)
        return cls.ADAPTER_DIR / cls.PROFILE_META[profile]["venv"]

    @classmethod
    def python_path(cls, profile: str) -> Path:
        env_dir = cls.env_dir(profile)
        if os.name == "nt":
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    @classmethod
    def _marker_path(cls, profile: str) -> Path:
        return cls.env_dir(profile) / ".nachobot-runtime.json"

    @classmethod
    def _marker_valid(cls, profile: str) -> bool:
        marker = cls._marker_path(profile)
        if not marker.exists():
            return False
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            return (
                data.get("schema") == cls.SCHEMA_VERSION
                and data.get("profile") == profile
                and data.get("state", "ready") == "ready"
            )
        except Exception:
            return False

    @classmethod
    def _local_payload_present(cls, profile: str) -> bool:
        """Cheaply reject empty or damaged GPU/CPU venvs without importing models."""
        profile = cls.normalize_profile(profile)
        if profile not in {"gpu", "cpu"}:
            return False

        env_dir = cls.env_dir(profile)
        windows_site = env_dir / "Lib" / "site-packages"
        posix_lib = env_dir / "lib"

        site_packages: list[Path] = []
        if windows_site.is_dir():
            site_packages.append(windows_site)
        if posix_lib.is_dir():
            site_packages.extend(posix_lib.glob("python*/site-packages"))

        required = ("torch", "transformers", "timm", "sherpa_onnx")
        return any(
            all((site / package).exists() for package in required)
            for site in site_packages
        )

    @classmethod
    def _validate_runtime(cls, profile: str) -> tuple[bool, str]:
        """Import critical dependencies from the selected venv and verify Torch flavor."""
        profile = cls.normalize_profile(profile)
        python = cls.python_path(profile)
        if not python.exists():
            return False, f"未找到 Python: {python}"

        expected_cuda = "True" if profile == "gpu" else "False"
        check = (
            "import importlib; "
            "[(print('runtime-check: ' + name, flush=True), importlib.import_module(name)) "
            "for name in ('torch', 'transformers', 'timm', 'sherpa_onnx')]; "
            "import torch; "
            "has_cuda_build = torch.version.cuda is not None; "
            f"assert has_cuda_build is {expected_cuda}, "
            "f'unexpected torch build: {torch.__version__}, cuda={torch.version.cuda}'; "
            "print(f'runtime-ok torch={torch.__version__} cuda={torch.version.cuda}')"
        )

        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        env["PYTHONNOUSERSITE"] = "1"
        try:
            result = subprocess.run(
                [str(python), "-I", "-u", "-c", check],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(cls.ADAPTER_DIR),
                timeout=cls.VALIDATION_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            stages = [line for line in output.splitlines() if line.startswith("runtime-check: ")]
            stage = stages[-1] if stages else "Python 启动"
            return False, f"依赖导入验证超时（{cls.VALIDATION_TIMEOUT} 秒），最后阶段: {stage}"
        except Exception as exc:
            return False, str(exc)

        output = (result.stdout or "").strip()
        if result.returncode != 0:
            return False, output or f"验证进程退出码: {result.returncode}"
        return True, output.splitlines()[-1] if output else "runtime-ok"

    @classmethod
    async def _validate_after_install(cls, profile, callback) -> tuple[bool, str]:
        """Give cold imports time to finish while keeping the wizard informed."""
        if callback:
            await callback(
                f"[Runtime] 正在导入并验证 {cls.PROFILE_META[profile]['label']} 依赖；"
                f"首次加载可能较慢，最多等待 {cls.VALIDATION_TIMEOUT} 秒...\n"
            )
        started = time.monotonic()
        validation = asyncio.create_task(asyncio.to_thread(cls._validate_runtime, profile))
        while True:
            done, _ = await asyncio.wait({validation}, timeout=cls.VALIDATION_PROGRESS_INTERVAL)
            if done:
                return validation.result()
            if callback:
                await callback(f"[Runtime] 依赖导入验证仍在进行，已等待 {int(time.monotonic() - started)} 秒...\n")

    @classmethod
    def _check_dependencies(cls, profile: str) -> tuple[bool, str]:
        """Check the whole locked environment without changing it or using the network."""
        project = cls.ADAPTER_DIR if profile == "gpu" else cls.RUNTIME_DIR / f"webui-{profile}"
        if not (project / "pyproject.toml").is_file() or not (project / "uv.lock").is_file():
            return False, "缺少依赖清单或锁文件，请验证 / 补齐"
        if profile == "cpu":
            try:
                if (project / "pyproject.toml").read_bytes() != (cls.ADAPTER_DIR / "pyproject.toml.cpu").read_bytes():
                    return False, "CPU 依赖清单已变化，请验证 / 补齐"
            except OSError:
                return False, "无法读取 CPU 依赖清单，请验证 / 补齐"
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["UV_PROJECT_ENVIRONMENT"] = str(cls.env_dir(profile))
        env["PYTHONNOUSERSITE"] = "1"
        command = [shutil.which("uv") or "uv", "sync", "--check", "--locked", "--offline",
                   "--no-python-downloads", "--python", str(cls.python_path(profile))]
        if profile == "cpu":
            command.append("--no-install-project")
        try:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, "依赖完整性检查未通过，请验证 / 补齐"
        if result.returncode != 0:
            return False, "依赖不完整或与锁文件不一致，请验证 / 补齐"
        return True, "依赖完整性检查通过"

    @classmethod
    def get_status(cls, profile: str) -> dict[str, Any]:
        profile = cls.normalize_profile(profile)
        python = cls.python_path(profile)
        marker_path = cls._marker_path(profile)
        marker_valid = cls._marker_valid(profile)

        # Local runtimes must contain the actual model stack even when a valid
        # marker exists. This cheaply catches empty/damaged venvs without running
        # imports during the launcher's frequent status polling.
        local_payload = cls._local_payload_present(profile)
        legacy_gpu = (
            profile == "gpu"
            and python.exists()
            and not marker_path.exists()
            and local_payload
        )
        eligible = python.exists() and local_payload and (marker_valid or legacy_gpu)
        if profile in cls._installing:
            installed, message = False, "正在验证 / 补齐依赖"
        elif eligible:
            installed, message = cls._check_dependencies(profile)
        else:
            installed, message = False, "环境缺失、上次安装未完成或依赖不完整，请安装 / 补齐"
        return {
            "id": profile,
            "label": cls.PROFILE_META[profile]["label"],
            "installed": installed,
            "legacy": legacy_gpu,
            "path": str(cls.env_dir(profile)),
            "message": message,
        }

    @classmethod
    def get_all_statuses(cls) -> dict[str, dict[str, Any]]:
        return {profile: cls.get_status(profile) for profile in cls.VALID_PROFILES}

    @classmethod
    def require_python(cls, profile: str) -> Path:
        profile = cls.normalize_profile(profile)
        status = cls.get_status(profile)
        if not status["installed"]:
            raise RuntimeError(
                f"Multimodal {status['label']}: {status['message']}，请先在一键启动页面补齐依赖"
            )
        return cls.python_path(profile)

    @classmethod
    async def install(
        cls,
        profile: str,
        callback: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Always sync on retry; retain a failed marker instead of accepting leftovers."""
        try:
            profile = cls.normalize_profile(profile)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if profile in cls._installing:
            return {"status": "error", "message": "该环境正在安装，请等待当前任务完成"}
        cls._installing.add(profile)
        succeeded = False
        try:
            result = await cls._install(profile, callback)
            succeeded = result.get("status") == "ok"
            return result
        finally:
            cls._installing.discard(profile)
            marker = cls._marker_path(profile)
            if not succeeded and marker.parent.is_dir():
                marker.write_text(json.dumps({
                    "schema": cls.SCHEMA_VERSION, "profile": profile, "state": "failed",
                }), encoding="utf-8")

    @classmethod
    async def _install(
        cls,
        profile: str,
        callback: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Install one isolated runtime without mutating another profile."""
        try:
            profile = cls.normalize_profile(profile)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

        if not cls.ADAPTER_DIR.exists():
            return {"status": "error", "message": f"目录不存在: {cls.ADAPTER_DIR}"}

        marker = cls._marker_path(profile)
        if marker.parent.is_dir():
            marker.write_text(json.dumps({
                "schema": cls.SCHEMA_VERSION, "profile": profile, "state": "installing",
            }), encoding="utf-8")
        if callback:
            await callback(
                f"[Runtime] 正在准备 {cls.PROFILE_META[profile]['label']} 环境...\n"
            )

        try:
            project_dir = cls._prepare_project(profile)
        except Exception as exc:
            return {"status": "error", "message": f"准备 runtime 项目失败: {exc}"}

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["UV_PROJECT_ENVIRONMENT"] = str(cls.env_dir(profile))

        uv = shutil.which("uv") or "uv"
        command = [uv, "sync", "--python", ">=3.11,<3.13"]
        if profile != "gpu":
            # CPU runtime projects exist only to materialize dependencies;
            # application source is executed from ADAPTER_DIR at launch time.
            command.append("--no-install-project")

        if callback:
            await callback("[Runtime] 正在执行 uv sync 验证并补齐全部依赖（已有环境也会重新检查）...\n")

        try:
            returncode = await run_install_command(command, cwd=project_dir, env=env, callback=callback)
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as exc:
            return {"status": "error", "message": f"安装出错: {exc}"}

        if returncode != 0:
            return {
                "status": "error",
                "message": f"{cls.PROFILE_META[profile]['label']} 环境安装失败，uv sync 退出码: {returncode}",
            }

        python = cls.python_path(profile)
        if not python.exists():
            return {
                "status": "error",
                "message": f"环境安装完成但未找到 Python: {python}",
            }

        valid, validation_message = await cls._validate_after_install(profile, callback)
        if not valid:
            return {
                "status": "error",
                "message": (
                    f"{cls.PROFILE_META[profile]['label']} 环境依赖验证失败，"
                    f"未写入安装标记: {validation_message}"
                ),
            }
        if callback:
            await callback(f"[Runtime] 依赖验证通过: {validation_message}\n")

        cls._marker_path(profile).write_text(
            json.dumps(
                {"schema": cls.SCHEMA_VERSION, "profile": profile},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        if callback:
            await callback(f"[Runtime] {cls.PROFILE_META[profile]['label']} 环境已就绪。\n")
        message = f"{cls.PROFILE_META[profile]['label']} 环境安装完成"
        return {
            "status": "ok",
            "message": message,
        }

    @classmethod
    def _prepare_project(cls, profile: str) -> Path:
        profile = cls.normalize_profile(profile)
        if profile == "gpu":
            # Main pyproject.toml + uv.lock own the CUDA environment.
            return cls.ADAPTER_DIR

        cls.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        project_dir = cls.RUNTIME_DIR / f"webui-{profile}"
        project_dir.mkdir(parents=True, exist_ok=True)
        target = project_dir / "pyproject.toml"

        source = cls.ADAPTER_DIR / "pyproject.toml.cpu"
        if not source.exists():
            raise FileNotFoundError("缺少 pyproject.toml.cpu")
        content = source.read_text(encoding="utf-8")

        if not target.exists() or target.read_text(encoding="utf-8") != content:
            target.write_text(content, encoding="utf-8")
            # Let uv regenerate a lock compatible with the new runtime spec.
            (project_dir / "uv.lock").unlink(missing_ok=True)
        return project_dir
