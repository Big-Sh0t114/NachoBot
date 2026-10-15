"""Isolated Multimodal Adapter runtime environments managed by the WebUI."""

from __future__ import annotations

import asyncio
import json
import locale
import os
import shutil
from pathlib import Path
from typing import Any, Callable

try:
    from .setup_checks import ROOT_DIR
except ImportError:
    from setup_checks import ROOT_DIR


class MultimodalRuntimeManager:
    """Install and resolve the GPU and CPU Multimodal environments."""

    # Schema 1 could be written after merely finding python.exe, allowing a
    # half-created ~100 KB venv to be reported as installed. Schema 2 is only
    # written after _validate_runtime() succeeds.
    SCHEMA_VERSION = 2
    ADAPTER_DIR = ROOT_DIR / "NachoBot-Multimodal-Adapter"
    RUNTIME_DIR = ADAPTER_DIR / ".runtime"
    VALID_PROFILES = ("gpu", "cpu")

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
            "import torch, transformers, timm, sherpa_onnx; "
            "has_cuda_build = torch.version.cuda is not None; "
            f"assert has_cuda_build is {expected_cuda}, "
            "f'unexpected torch build: {torch.__version__}, cuda={torch.version.cuda}'; "
            "print(f'runtime-ok torch={torch.__version__} cuda={torch.version.cuda}')"
        )

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        try:
            import subprocess

            result = subprocess.run(
                [str(python), "-c", check],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            return False, str(exc)

        output = (result.stdout or "").strip()
        if result.returncode != 0:
            return False, output or f"验证进程退出码: {result.returncode}"
        return True, output or "runtime-ok"

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
        installed = python.exists() and local_payload and (marker_valid or legacy_gpu)
        return {
            "id": profile,
            "label": cls.PROFILE_META[profile]["label"],
            "installed": installed,
            "legacy": legacy_gpu,
            "path": str(cls.env_dir(profile)),
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
                f"Multimodal {status['label']} 环境尚未安装，请先在一键启动页面补齐依赖"
            )
        return cls.python_path(profile)

    @classmethod
    async def install(
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

        cls._marker_path(profile).unlink(missing_ok=True)
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

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
                env=env,
            )
            fallback_enc = locale.getpreferredencoding(False) or "gbk"
            if proc.stdout is not None:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    try:
                        text = line.decode("utf-8")
                    except UnicodeDecodeError:
                        text = line.decode(fallback_enc, errors="replace")
                    if callback:
                        await callback(text)
            await proc.wait()
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as exc:
            return {"status": "error", "message": f"安装出错: {exc}"}

        if proc.returncode != 0:
            return {
                "status": "error",
                "message": f"{cls.PROFILE_META[profile]['label']} 环境安装失败，uv sync 退出码: {proc.returncode}",
            }

        python = cls.python_path(profile)
        if not python.exists():
            return {
                "status": "error",
                "message": f"环境安装完成但未找到 Python: {python}",
            }

        valid, validation_message = await asyncio.to_thread(cls._validate_runtime, profile)
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
