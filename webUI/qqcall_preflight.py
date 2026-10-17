"""Bounded QQCall deployment checks without importing native modules in WebUI."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess


_ISSUES = {
    "environment_invalid": "UniversalVC 需要 Python 3.11 或更高版本的 64 位环境",
    "dependencies_unavailable": "UniversalVC Python 依赖或原生 DLL 不完整",
    "service_config_invalid": "UniversalVC service.toml 配置无效",
    "vad_model_missing": "缺少 VAD 模型",
    "vad_model_invalid": "VAD 模型无法加载",
    "speaker_model_missing": "缺少声纹模型",
    "speaker_model_invalid": "声纹模型无法加载",
}


async def _probe(python: Path, script: Path, *, timeout: float = 45) -> dict:
    child = await asyncio.create_subprocess_exec(
        str(python), str(script), cwd=str(script.parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout)
    finally:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            await child.wait()
    if len(output) > 65536 or child.returncode not in (0, 2):
        raise ValueError("probe_failed")
    result = json.loads(output)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("probe_failed")
    if result["ok"] != (child.returncode == 0):
        raise ValueError("probe_failed")
    return result


async def check_qqcall_deployment(root: Path) -> list[str]:
    issues = []
    universal = root / "NachoBot-UniversalVC-Adapter"
    python = universal / ".venv/Scripts/python.exe"
    script = universal / "check_deployment.py"
    if not python.is_file():
        issues.append("缺少 UniversalVC 独立 Python 环境")
    elif not script.is_file() or not (universal / "service_main.py").is_file():
        issues.append("UniversalVC 服务文件不完整")
    else:
        try:
            result = await _probe(python, script)
            codes = result.get("issues")
            if (not isinstance(codes, list) or len(codes) > len(_ISSUES)
                    or any(type(code) is not str or code not in _ISSUES for code in codes)
                    or result["ok"] != (not codes)):
                raise ValueError("probe_failed")
            issues.extend(_ISSUES[code] for code in codes)
        except (OSError, ValueError, asyncio.TimeoutError):
            issues.append("UniversalVC 环境或模型检查失败、超时或进程异常退出")
    python = root / "NachoBot/.venv/Scripts/python.exe"
    script = root / "NachoBot-QQCall/scripts/prepare-audio-route.py"
    if not python.is_file() or not script.is_file():
        issues.append("QQCall 音频检查环境或脚本缺失")
    else:
        try:
            result = await _probe(python, script, timeout=20)
            if result["ok"] and result.get("phase") == "existing_guard_valid":
                pass
            elif result["ok"] and result.get("phase") == "guard_prepared":
                issues.append("尚未生成本机音频路由配置，请运行 prepare-audio-route.py --apply")
            else:
                issues.append("虚拟声卡或音频路由不可用，请检查驱动、活动设备及录音/播放设备选择")
        except (OSError, ValueError, asyncio.TimeoutError):
            issues.append("虚拟声卡与本机音频路由检查失败或超时")
    return issues


def deployment_error(issues: list[str]) -> str:
    return ("QQCall 启动已阻止：" + "；".join(issues)
            + "。请前往部署向导部署或修复 Universal 服务（UniversalVC），完成模型下载、虚拟声卡安装和本机音频路由准备后重试。")
