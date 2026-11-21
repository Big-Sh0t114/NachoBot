"""Isolated Multimodal Adapter runtime environments managed by the WebUI."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
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
    """Install and resolve the Multimodal local and GenericHTTP environments."""

    # Schema 1 could be written after merely finding python.exe, allowing a
    # half-created ~100 KB venv to be reported as installed. Schema 2 is only
    # written after _validate_runtime() succeeds.
    SCHEMA_VERSION = 2
    ADAPTER_DIR = ROOT_DIR / "NachoBot-Multimodal-Adapter"
    RUNTIME_DIR = ADAPTER_DIR / ".runtime"
    VALID_PROFILES = ("gpu", "cpu", "http")
    LOCAL_PROFILES = ("gpu", "cpu")
    HTTP_PACKAGES = ("fastapi", "loguru", "numpy", "pydantic", "scipy", "toml", "uvicorn")
    CUDA_INDEX = "https://download.pytorch.org/whl/cu130"
    CPU_INDEX = "https://download.pytorch.org/whl/cpu"
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
        "http": {
            "label": "GenericHTTP HTTP",
            "venv": ".venv-http",
        },
    }

    @classmethod
    def normalize_profile(cls, profile: str | None) -> str:
        value = str(profile or "").strip().lower()
        if value not in cls.VALID_PROFILES:
            raise ValueError(f"未知 Multimodal 环境: {profile}")
        return value

    @classmethod
    def normalize_local_profile(cls, profile: str | None) -> str:
        """Normalize a selected local model environment; HTTP is not a model device."""
        value = str(profile or "").strip().lower()
        if value not in cls.LOCAL_PROFILES:
            raise ValueError(f"本地模型环境必须为 GPU 或 CPU: {profile}")
        return value

    @classmethod
    def _adapter_dir_for_root(cls, root: Path | str | None = None) -> Path:
        if root is None:
            return Path(cls.ADAPTER_DIR)
        path = Path(root)
        if path.name.casefold() == "nachobot-multimodal-adapter":
            return path
        return path / "NachoBot-Multimodal-Adapter"

    @classmethod
    def _runtime_dir_for_root(cls, root: Path | str | None = None) -> Path:
        return cls.RUNTIME_DIR if root is None else cls._adapter_dir_for_root(root) / ".runtime"

    @classmethod
    def resolve_tts_engine(
        cls,
        root: Path | str | None = None,
        *,
        environ: dict[str, str] | None = None,
    ) -> str:
        """Use the adapter's canonical resolver, including its env override and aliases."""
        adapter_dir = cls._adapter_dir_for_root(root)
        if str(adapter_dir) not in sys.path:
            sys.path.insert(0, str(adapter_dir))
        resolver = importlib.import_module("scripts.tts_runtime_manager").resolve_engine
        env = os.environ if environ is None else environ
        return resolver(adapter_dir / "configs" / "base.toml", env.get("NACHOBOT_TTS_ENGINE", ""))

    @classmethod
    def resolve_tts_profile(
        cls,
        root: Path | str | None = None,
        *,
        environ: dict[str, str] | None = None,
    ) -> str | None:
        """Return the remote HTTP profile for GenericHTTP; local backends return None."""
        engine = cls.resolve_tts_engine(root, environ=environ)
        return "http" if engine == "generic-http" else None

    @classmethod
    def resolve_required_profiles(
        cls,
        runtime: str,
        root: Path | str | None = None,
        *,
        lite: bool = False,
        environ: dict[str, str] | None = None,
    ) -> tuple[str, ...]:
        """Resolve setup/start gates while keeping HTTP TTS separate from local perception."""
        local_profile = cls.normalize_local_profile(runtime)
        if cls.resolve_tts_profile(root, environ=environ) != "http":
            return (local_profile,)
        if lite:
            return ("http",)
        return (local_profile, "http")

    @classmethod
    def env_dir(cls, profile: str, root: Path | str | None = None) -> Path:
        profile = cls.normalize_profile(profile)
        return cls._adapter_dir_for_root(root) / cls.PROFILE_META[profile]["venv"]

    @classmethod
    def python_path(cls, profile: str, root: Path | str | None = None) -> Path:
        env_dir = cls.env_dir(profile, root)
        if os.name == "nt":
            return env_dir / "Scripts" / "python.exe"
        return env_dir / "bin" / "python"

    @classmethod
    def torch_index_url(cls, profile: str) -> str:
        """Return the unified PyTorch index used by the selected environment."""
        profile = cls.normalize_profile(profile)
        return cls.CPU_INDEX if profile in {"cpu", "http"} else cls.CUDA_INDEX

    @classmethod
    def _marker_path(cls, profile: str, root: Path | str | None = None) -> Path:
        return cls.env_dir(profile, root) / ".nachobot-runtime.json"

    @classmethod
    def _marker_valid(cls, profile: str, root: Path | str | None = None) -> bool:
        marker = cls._marker_path(profile, root)
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
    def _local_payload_present(cls, profile: str, root: Path | str | None = None) -> bool:
        """Cheaply reject empty/damaged model or HTTP venvs without importing packages."""
        profile = cls.normalize_profile(profile)
        env_dir = cls.env_dir(profile, root)
        windows_site = env_dir / "Lib" / "site-packages"
        posix_lib = env_dir / "lib"

        site_packages: list[Path] = []
        if windows_site.is_dir():
            site_packages.append(windows_site)
        if posix_lib.is_dir():
            site_packages.extend(posix_lib.glob("python*/site-packages"))

        required = cls.HTTP_PACKAGES if profile == "http" else ("torch", "transformers", "timm", "sherpa_onnx")
        return any(
            all((site / package).exists() for package in required)
            for site in site_packages
        )

    @classmethod
    def _validate_runtime(cls, profile: str, root: Path | str | None = None) -> tuple[bool, str]:
        """Import critical dependencies from the selected venv and verify Torch flavor."""
        profile = cls.normalize_profile(profile)
        python = cls.python_path(profile, root)
        if not python.exists():
            return False, f"未找到 Python: {python}"

        if profile == "http":
            packages = ", ".join(repr(name) for name in cls.HTTP_PACKAGES)
            check = (
                "import importlib; "
                f"[(print('runtime-check: ' + name, flush=True), importlib.import_module(name)) for name in ({packages},)]; "
                "print('runtime-ok GenericHTTP')"
            )
        else:
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
                cwd=str(cls._adapter_dir_for_root(root)),
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
    async def _validate_after_install(cls, profile, callback, root=None) -> tuple[bool, str]:
        """Give cold imports time to finish while keeping the wizard informed."""
        if callback:
            await callback(
                f"[Runtime] 正在导入并验证 {cls.PROFILE_META[profile]['label']} 依赖；"
                f"首次加载可能较慢，最多等待 {cls.VALIDATION_TIMEOUT} 秒...\n"
            )
        started = time.monotonic()
        if root is None:
            validation = asyncio.create_task(asyncio.to_thread(cls._validate_runtime, profile))
        else:
            validation = asyncio.create_task(asyncio.to_thread(cls._validate_runtime, profile, root))
        while True:
            done, _ = await asyncio.wait({validation}, timeout=cls.VALIDATION_PROGRESS_INTERVAL)
            if done:
                return validation.result()
            if callback:
                await callback(f"[Runtime] 依赖导入验证仍在进行，已等待 {int(time.monotonic() - started)} 秒...\n")

    @classmethod
    def _check_dependencies(cls, profile: str, root: Path | str | None = None) -> tuple[bool, str]:
        """Check the whole locked environment without changing it or using the network."""
        profile = cls.normalize_profile(profile)
        adapter_dir = cls._adapter_dir_for_root(root)
        runtime_dir = cls._runtime_dir_for_root(root)
        project = adapter_dir if profile == "gpu" else runtime_dir / ("bat-http" if profile == "http" else "webui-cpu")
        if not (project / "pyproject.toml").is_file() or not (project / "uv.lock").is_file():
            return False, "缺少依赖清单或锁文件，请验证 / 补齐"
        if profile in {"cpu", "http"}:
            spec_name = "pyproject.toml.http" if profile == "http" else "pyproject.toml.cpu"
            try:
                if (project / "pyproject.toml").read_bytes() != (adapter_dir / spec_name).read_bytes():
                    return False, f"{cls.PROFILE_META[profile]['label']} 依赖清单已变化，请验证 / 补齐"
            except OSError:
                return False, f"无法读取 {cls.PROFILE_META[profile]['label']} 依赖清单，请验证 / 补齐"
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["UV_PROJECT_ENVIRONMENT"] = str(cls.env_dir(profile, root))
        env["PYTHONNOUSERSITE"] = "1"
        command = [shutil.which("uv") or "uv", "sync", "--check", "--locked", "--offline",
                   "--no-python-downloads", "--python", str(cls.python_path(profile, root))]
        if profile in cls.LOCAL_PROFILES:
            command.insert(5, "--inexact")
        if profile != "gpu":
            command.append("--no-install-project")
        try:
            result = subprocess.run(command, cwd=project, env=env, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return False, "依赖完整性检查未通过，请验证 / 补齐"
        if result.returncode != 0:
            return False, "依赖不完整或与锁文件不一致，请验证 / 补齐"
        return True, "依赖完整性检查通过"

    @classmethod
    def get_status(cls, profile: str, root: Path | str | None = None) -> dict[str, Any]:
        profile = cls.normalize_profile(profile)
        python = cls.python_path(profile, root)
        marker_path = cls._marker_path(profile, root)
        marker_valid = cls._marker_valid(profile, root)

        # Local runtimes must contain the actual model stack even when a valid
        # marker exists. This cheaply catches empty/damaged venvs without running
        # imports during the launcher's frequent status polling.
        local_payload = cls._local_payload_present(profile, root)
        legacy_runtime = (
            profile in {"gpu", "http"}
            and python.exists()
            and not marker_path.exists()
            and local_payload
        )
        eligible = python.exists() and local_payload and (marker_valid or legacy_runtime)
        if profile in cls._installing:
            installed, message = False, "正在验证 / 补齐依赖"
        elif eligible:
            installed, message = cls._check_dependencies(profile, root)
        else:
            installed, message = False, "环境缺失、上次安装未完成或依赖不完整，请安装 / 补齐"
        return {
            "id": profile,
            "label": cls.PROFILE_META[profile]["label"],
            "installed": installed,
            "legacy": legacy_runtime,
            "path": str(cls.env_dir(profile, root)),
            "message": message,
        }

    @classmethod
    def get_all_statuses(cls, root: Path | str | None = None) -> dict[str, dict[str, Any]]:
        return {profile: cls.get_status(profile, root) for profile in cls.VALID_PROFILES}

    @classmethod
    def require_python(cls, profile: str, root: Path | str | None = None) -> Path:
        profile = cls.normalize_profile(profile)
        status = cls.get_status(profile, root)
        if not status["installed"]:
            raise RuntimeError(
                f"Multimodal {status['label']}: {status['message']}，请先在一键启动页面补齐依赖"
            )
        return cls.python_path(profile, root)

    @classmethod
    async def install(
        cls,
        profile: str,
        callback: Callable[[str], Any] | None = None,
        *,
        root: Path | str | None = None,
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
            result = await cls._install(profile, callback, root=root)
            succeeded = result.get("status") == "ok"
            return result
        finally:
            cls._installing.discard(profile)
            marker = cls._marker_path(profile, root)
            if not succeeded and marker.parent.is_dir():
                marker.write_text(json.dumps({
                    "schema": cls.SCHEMA_VERSION, "profile": profile, "state": "failed",
                }), encoding="utf-8")

    @classmethod
    async def _install(
        cls,
        profile: str,
        callback: Callable[[str], Any] | None = None,
        *,
        root: Path | str | None = None,
    ) -> dict[str, Any]:
        """Install one isolated runtime without mutating another profile."""
        try:
            profile = cls.normalize_profile(profile)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}

        adapter_dir = cls._adapter_dir_for_root(root)
        if not adapter_dir.exists():
            return {"status": "error", "message": f"目录不存在: {adapter_dir}"}

        marker = cls._marker_path(profile, root)
        if marker.parent.is_dir():
            marker.write_text(json.dumps({
                "schema": cls.SCHEMA_VERSION, "profile": profile, "state": "installing",
            }), encoding="utf-8")
        if callback:
            await callback(
                f"[Runtime] 正在准备 {cls.PROFILE_META[profile]['label']} 环境...\n"
            )

        try:
            project_dir = cls._prepare_project(profile, root)
        except Exception as exc:
            return {"status": "error", "message": f"准备 runtime 项目失败: {exc}"}

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["UV_PROJECT_ENVIRONMENT"] = str(cls.env_dir(profile, root))

        uv = shutil.which("uv") or "uv"
        # GPU and CPU profiles retain backend-managed inference packages that
        # are outside the core lock. HTTP remains an exact, lightweight sync.
        command = [uv, "sync", "--python", ">=3.11,<3.13"]
        if profile in cls.LOCAL_PROFILES:
            command.append("--inexact")
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

        python = cls.python_path(profile, root)
        if not python.exists():
            return {
                "status": "error",
                "message": f"环境安装完成但未找到 Python: {python}",
            }

        valid, validation_message = await cls._validate_after_install(profile, callback, root)
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

        cls._marker_path(profile, root).write_text(
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
    def _prepare_project(cls, profile: str, root: Path | str | None = None) -> Path:
        profile = cls.normalize_profile(profile)
        if profile == "gpu":
            # Main pyproject.toml + uv.lock own the CUDA environment.
            return cls._adapter_dir_for_root(root)

        runtime_dir = cls._runtime_dir_for_root(root)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        project_dir = runtime_dir / ("bat-http" if profile == "http" else "webui-cpu")
        project_dir.mkdir(parents=True, exist_ok=True)
        target = project_dir / "pyproject.toml"

        adapter_dir = cls._adapter_dir_for_root(root)
        source = adapter_dir / ("pyproject.toml.http" if profile == "http" else "pyproject.toml.cpu")
        if not source.exists():
            raise FileNotFoundError(f"缺少 {source.name}")
        content = source.read_bytes()

        if not target.exists() or target.read_bytes() != content:
            target.write_bytes(content)
            # Let uv regenerate a lock compatible with the new runtime spec.
            (project_dir / "uv.lock").unlink(missing_ok=True)
        return project_dir
