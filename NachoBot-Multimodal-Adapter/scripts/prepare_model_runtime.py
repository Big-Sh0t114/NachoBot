"""Plan and prepare the root BAT launcher's model-specific environments.

This bootstrap is intentionally standard-library-only until it imports the
adapter's standard-library device and TTS selectors. It must run before any
model package is imported or any environment is synchronized.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid


ADAPTER_ROOT = Path(__file__).resolve().parents[1]
if str(ADAPTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ADAPTER_ROOT))

from nachobot_multimodal.utils import model_devices  # noqa: E402
from nachobot_multimodal.utils.model_devices import ModelDeviceConfigError  # noqa: E402
from scripts.tts_runtime_manager import resolve_engine  # noqa: E402


CPU_INDEX = "https://download.pytorch.org/whl/cpu"
GPU_INDEX = "https://download.pytorch.org/whl/cu130"
SUPPORTED_VARIANTS = {"full", "lite"}
SUPPORTED_PROFILES = {"cpu", "gpu", "http"}
_VLM_DEVICE_FOR_BATCH = re.compile(r"^(?:cpu|cuda(?::[0-9]+)?)$")


@dataclass(frozen=True)
class RuntimePlan:
    adapter_root: Path
    variant: str
    engine: str
    tts_profile: str
    perception_profile: str | None
    vlm_device: str | None
    tts_choice: str | None
    vlm_choice: str | None

    @property
    def profiles(self) -> tuple[str, ...]:
        active = {self.tts_profile}
        if self.perception_profile is not None:
            active.add(self.perception_profile)
        return tuple(profile for profile in ("gpu", "cpu", "http") if profile in active)

    def venv_dir(self, profile: str) -> Path:
        if profile not in SUPPORTED_PROFILES:
            raise ValueError(f"unsupported runtime profile: {profile}")
        return self.adapter_root / {"gpu": ".venv", "cpu": ".venv-cpu", "http": ".venv-http"}[profile]

    def python_path(self, profile: str) -> Path:
        venv = self.venv_dir(profile)
        return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def to_dict(self) -> dict[str, object]:
        tts_python_profile = "cpu" if self.tts_profile == "http" else self.tts_profile
        return {
            "variant": self.variant,
            "engine": self.engine,
            "tts_choice": self.tts_choice,
            "tts_profile": self.tts_profile,
            "tts_python": str(self.python_path(self.tts_profile)),
            "tts_runtime_profile": tts_python_profile,
            "tts_torch_index": GPU_INDEX if tts_python_profile == "gpu" else CPU_INDEX,
            "perception_choice": self.vlm_choice,
            "perception_profile": self.perception_profile,
            "perception_python": (
                str(self.python_path(self.perception_profile))
                if self.perception_profile is not None
                else None
            ),
            "vlm_device": self.vlm_device,
            "profiles_to_prepare": list(self.profiles),
        }


def _profile_for_local_choice(model: str, choice: str, auto_gpu: bool) -> tuple[str, str | None]:
    """Resolve the environment profile while retaining the configured device semantics."""
    if choice == "cpu":
        return "cpu", "cpu" if model == "vlm" else None
    if model == "vlm" and model_devices.CUDA_DEVICE_PATTERN.fullmatch(choice):
        # Keep legacy logical CUDA selectors intact. The Florence loader still
        # falls back to CPU when Torch reports that CUDA is unavailable.
        return ("gpu" if auto_gpu else "cpu"), choice
    if model_devices.GPU_UUID_PATTERN.fullmatch(choice):
        return "gpu", "cuda:0" if model == "vlm" else None
    if choice == "auto":
        return ("gpu" if auto_gpu else "cpu"), ("cuda" if model == "vlm" and auto_gpu else "cpu" if model == "vlm" else None)
    raise ModelDeviceConfigError(f"unsupported {model.upper()} device choice: {choice}")


def build_runtime_plan(
    variant: str,
    adapter_root: Path | str = ADAPTER_ROOT,
    *,
    environ: dict[str, str] | None = None,
    detected_uuids: list[str] | None = None,
) -> RuntimePlan:
    """Validate active service selections and derive independent profiles."""
    normalized_variant = str(variant).strip().lower()
    if normalized_variant not in SUPPORTED_VARIANTS:
        raise ValueError("variant must be full or lite")

    root = Path(adapter_root).expanduser().resolve(strict=False)
    config_dir = root / "configs"
    base_path = config_dir / "base.toml"
    env = os.environ if environ is None else environ

    # Reuse the supervisor resolver so NACHOBOT_TTS_ENGINE aliases and
    # precedence stay exactly the same as the actual TTS entrypoint.
    engine = resolve_engine(base_path, env.get("NACHOBOT_TTS_ENGINE", ""))

    if engine == "generic-http":
        # Device fields for a remote facade are inactive and intentionally do
        # not affect either validation or its lightweight environment.
        tts_choice = None
        tts_profile = "http"
    else:
        tts_choice = model_devices.read_device_choice("tts", config_dir=config_dir)
        tts_profile, _ = _profile_for_local_choice("tts", tts_choice, auto_gpu=False)

    vlm_choice = None
    perception_profile = None
    vlm_device = None
    if normalized_variant == "full":
        perception_config_dir = Path(
            env.get("NACHOBOT_MULTIMODAL_CONFIG_DIR", config_dir)
        ).expanduser().resolve(strict=False)
        vlm_choice = model_devices.read_device_choice("vlm", config_dir=perception_config_dir)

    inherited_vlm_override = ""
    if vlm_choice == "auto":
        inherited_vlm_override = str(env.get("NACHOBOT_VLM_DEVICE", "")).strip()
        if inherited_vlm_override != "cpu" and not model_devices.CUDA_DEVICE_PATTERN.fullmatch(inherited_vlm_override):
            inherited_vlm_override = ""

    explicit_uuids = {
        choice
        for choice in (tts_choice, vlm_choice)
        if choice and model_devices.GPU_UUID_PATTERN.fullmatch(choice)
    }
    needs_auto_detection = any(
        choice == "auto" or (choice and model_devices.CUDA_DEVICE_PATTERN.fullmatch(choice))
        for choice in (tts_choice, vlm_choice)
    )
    if vlm_choice == "auto" and inherited_vlm_override == "cpu":
        needs_auto_detection = bool(tts_choice == "auto")
    if explicit_uuids:
        # One strict probe validates every configured UUID before any sync.
        present = model_devices.nvidia_gpu_uuids()
        missing = sorted(explicit_uuids - set(present))
        if missing:
            raise ModelDeviceConfigError(
                f"configured NVIDIA GPU {missing[0]} is unavailable; choose another GPU or set the model to auto/CPU"
            )
        available_uuids = present
    elif detected_uuids is not None:
        available_uuids = list(detected_uuids)
    elif needs_auto_detection:
        available_uuids = model_devices.detected_nvidia_gpu_uuids()
    else:
        available_uuids = []

    auto_gpu = model_devices.auto_cuda_available(environ=env, gpu_uuids=available_uuids)

    # Local TTS auto is resolved independently from perception. The HTTP
    # engine remains on its minimal facade environment regardless of this.
    if engine != "generic-http" and tts_choice == "auto":
        tts_profile = "gpu" if auto_gpu else "cpu"

    if vlm_choice is not None:
        if vlm_choice == "auto" and inherited_vlm_override == "cpu":
            perception_profile, vlm_device = "cpu", "cpu"
        elif vlm_choice == "auto" and inherited_vlm_override:
            perception_profile = "gpu" if auto_gpu else "cpu"
            vlm_device = inherited_vlm_override
        else:
            perception_profile, vlm_device = _profile_for_local_choice("vlm", vlm_choice, auto_gpu)

    return RuntimePlan(
        adapter_root=root,
        variant=normalized_variant,
        engine=engine,
        tts_profile=tts_profile,
        perception_profile=perception_profile,
        vlm_device=vlm_device,
        tts_choice=tts_choice,
        vlm_choice=vlm_choice,
    )


def _launch_dir(adapter_root: Path) -> Path:
    root = adapter_root.resolve(strict=False)
    launch = (root / ".runtime" / "launch").resolve(strict=False)
    if not launch.is_relative_to(root):
        raise ValueError("runtime transport directory must remain under the adapter")
    return launch


def validate_transport_path(adapter_root: Path, transport_path: Path | str) -> Path:
    """Accept only a BAT-named .cmd directly inside the adapter launch folder."""
    launch = _launch_dir(adapter_root)
    target = Path(transport_path).expanduser().resolve(strict=False)
    if target.parent != launch or not re.fullmatch(r"model-runtime-[A-Za-z0-9_-]+\.cmd", target.name, re.IGNORECASE):
        raise ValueError("transport file must be a generated model-runtime-*.cmd under .runtime/launch")
    return target


def _manifest_for_profile(plan: RuntimePlan, profile: str) -> tuple[Path, Path, bool]:
    if profile == "gpu":
        return plan.adapter_root / "pyproject.toml", plan.adapter_root, False
    if profile == "cpu":
        return plan.adapter_root / "pyproject.toml.cpu", plan.adapter_root / ".runtime" / "bat-cpu", True
    if profile == "http":
        return plan.adapter_root / "pyproject.toml.http", plan.adapter_root / ".runtime" / "bat-http", True
    raise ValueError(f"unsupported runtime profile: {profile}")


def _validate_manifests(plan: RuntimePlan) -> None:
    # Validate all distinct profile specs before preparing the first one.
    for profile in plan.profiles:
        manifest, _project, _copy = _manifest_for_profile(plan, profile)
        if not manifest.is_file():
            raise FileNotFoundError(f"runtime dependency spec not found for {profile}: {manifest}")


def _sync_profile(
    plan: RuntimePlan,
    profile: str,
    *,
    uv_executable: str,
    runner=None,
) -> None:
    manifest, project, copied_spec = _manifest_for_profile(plan, profile)
    if copied_spec:
        project.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, project / "pyproject.toml")
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(plan.venv_dir(profile))
    env["PYTHONNOUSERSITE"] = "1"
    command = [uv_executable, "sync", "--project", str(project), "--python", ">=3.11,<3.13"]
    if profile in {"gpu", "cpu"}:
        # Shared local profiles also host backend-managed inference packages.
        # Keep those extras while the manifest and lock continue to govern the
        # core dependency set. The lightweight HTTP profile remains exact.
        command.append("--inexact")
    if copied_spec:
        command.append("--no-install-project")
    run = subprocess.run if runner is None else runner
    completed = run(command, cwd=str(project), env=env, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"{profile} runtime uv sync failed with exit code {completed.returncode}")


def _transport_text(plan: RuntimePlan) -> str:
    if plan.tts_profile not in SUPPORTED_PROFILES:
        raise ValueError("invalid TTS profile for batch transport")
    if plan.perception_profile is not None and plan.perception_profile not in {"cpu", "gpu"}:
        raise ValueError("invalid perception profile for batch transport")
    if plan.vlm_device is not None and not _VLM_DEVICE_FOR_BATCH.fullmatch(plan.vlm_device):
        raise ValueError("invalid VLM device for batch transport")

    lines = ["@echo off", f'set "BAT_TTS_PROFILE={plan.tts_profile}"']
    if plan.perception_profile is not None:
        lines.extend(
            (
                f'set "BAT_VLM_PROFILE={plan.perception_profile}"',
                f'set "BAT_VLM_DEVICE={plan.vlm_device}"',
            )
        )
    lines.append("exit /b 0")
    return "\r\n".join(lines) + "\r\n"


def _write_transport(plan: RuntimePlan, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(_transport_text(plan).encode("utf-8"))
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def prepare_runtime(
    plan: RuntimePlan,
    transport_path: Path | str,
    *,
    uv_executable: str | None = None,
    runner=None,
) -> Path:
    """Prepare only the required distinct profiles and publish the BAT plan."""
    transport = validate_transport_path(plan.adapter_root, transport_path)
    # A reused nonce must never leave a stale launch plan that survives a
    # failed validation or sync.
    transport.unlink(missing_ok=True)
    _validate_manifests(plan)
    uv = uv_executable or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv was not found on PATH")

    for profile in plan.profiles:
        _sync_profile(plan, profile, uv_executable=uv, runner=runner)

    missing = [str(plan.python_path(profile)) for profile in plan.profiles if not plan.python_path(profile).is_file()]
    if missing:
        raise RuntimeError(f"prepared runtime Python was not found: {missing[0]}")

    _write_transport(plan, transport)
    return transport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=sorted(SUPPORTED_VARIANTS))
    parser.add_argument("--adapter-root", type=Path, default=ADAPTER_ROOT)
    parser.add_argument("--dry-plan", action="store_true", help="print the derived plan without syncing")
    parser.add_argument("--transport-file", type=Path, help="BAT transport path; required for preparation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_runtime_plan(args.variant, args.adapter_root)
        if args.dry_plan:
            print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
            return 0
        if args.transport_file is None:
            raise ValueError("--transport-file is required unless --dry-plan is used")
        result = prepare_runtime(plan, args.transport_file)
        print(json.dumps({**plan.to_dict(), "transport_file": str(result)}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"[model runtime] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
