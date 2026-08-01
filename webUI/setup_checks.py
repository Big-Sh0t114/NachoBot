"""Environment and external-path checks for the WebUI setup wizard."""

import os
import socket
import subprocess
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent

TEMPLATE_MAP: dict[str, str] = {
    "NachoBot/template/bot_config_template.toml": "NachoBot/config/bot_config.toml",
    "NachoBot/template/model_config_template.toml": "NachoBot/config/model_config.toml",
    "NachoBot/template/topics_config_template.toml": "NachoBot/config/topics_config.toml",
    "NachoBot/template/template.env": "NachoBot/.env",
    "NachoBot-Napcat-Adapter/template/template_config.toml": "NachoBot-Napcat-Adapter/config.toml",
    "NachoBot-Multimodal-Adapter/template_configs/base_template.toml": "NachoBot-Multimodal-Adapter/configs/base.toml",
    "NachoBot-Multimodal-Adapter/template_configs/gpt-sovits_template.toml": "NachoBot-Multimodal-Adapter/configs/gpt-sovits.toml",
    "NachoBot-Multimodal-Adapter/template_configs/vox_template.toml": "NachoBot-Multimodal-Adapter/configs/vox.toml",
    "NachoBot-UniversalVC-Adapter/template/config_template.toml": "NachoBot-UniversalVC-Adapter/config.toml",
    "NachoBot-Multimodal-Adapter/template_configs/perception_template.toml": "NachoBot-Multimodal-Adapter/configs/perception.toml",
}

KNOWN_PORTS: dict[str, int] = {
    "NachoBot Core": 8000,
    "Napcat Adapter": 8095,
    "Multimodal Adapter": 8070,
    "TTS Engine": 9880,
    "VLM / ASR API": 9874,
    "Koishi": 5140,
    "WebUI": 8088,
}


# =========================================================================
# Environment Checker

class EnvironmentChecker:
    """Checks the runtime environment required for deployment."""

    @staticmethod
    def check_all() -> dict[str, Any]:
        """Run all environment checks and return results."""
        return {
            "python": EnvironmentChecker.check_python(),
            "node": EnvironmentChecker.check_node(),
            "docker": EnvironmentChecker.check_docker(),
            "gpu": EnvironmentChecker.check_gpu(),
            "ports": EnvironmentChecker.check_ports(),
            "configs": EnvironmentChecker.check_configs(),
        }

    @staticmethod
    def check_python() -> dict[str, Any]:
        """Check Python and uv availability."""
        result = {"status": "error", "python": None, "uv": None, "message": ""}

        # Check Python
        try:
            out = subprocess.run(
                ["python", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                version_str = out.stdout.strip() or out.stderr.strip()
                result["python"] = version_str

                # Validate version range: requires >=3.11, <3.13
                import re
                ver_match = re.search(r"(\d+)\.(\d+)", version_str)
                if ver_match:
                    major, minor = int(ver_match.group(1)), int(ver_match.group(2))
                    if major != 3 or minor < 11 or minor > 12:
                        result["status"] = "error"
                        result["message"] = (
                            f"{version_str} — 版本不兼容，需要 Python ≥3.11 且 ≤3.12"
                        )
                        return result
                else:
                    result["status"] = "warning"
                    result["message"] = f"{version_str} — 无法解析版本号"
                    return result
            else:
                result["message"] = "Python 未找到"
                return result
        except FileNotFoundError:
            result["message"] = "Python 未安装或不在 PATH 中"
            return result
        except Exception as e:
            result["message"] = f"检测 Python 时出错: {e}"
            return result

        # Check uv
        try:
            out = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["uv"] = out.stdout.strip()
                result["status"] = "ok"
                result["message"] = f"{result['python']} · {result['uv']}"
            else:
                result["status"] = "warning"
                result["message"] = f"{result['python']} (uv 未安装 — 建议安装)"
        except FileNotFoundError:
            result["status"] = "warning"
            result["message"] = f"{result['python']} (uv 未安装 — 建议安装)"
        except Exception:
            result["status"] = "warning"
            result["message"] = f"{result['python']} (uv 检测失败)"

        return result

    @staticmethod
    def check_node() -> dict[str, Any]:
        """Check Node.js availability (optional, for Koishi)."""
        result = {"status": "warning", "node": None, "npm": None, "message": ""}

        try:
            out = subprocess.run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["node"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        try:
            out = subprocess.run(
                ["npm", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["npm"] = f"npm {out.stdout.strip()}"
        except (FileNotFoundError, Exception):
            pass

        if result["node"]:
            result["status"] = "ok"
            parts = [f"Node.js {result['node']}"]
            if result["npm"]:
                parts.append(result["npm"])
            result["message"] = " · ".join(parts)
        else:
            result["status"] = "warning"
            result["message"] = "Node.js 未安装 (仅 Discord/Koishi 适配器需要)"

        return result

    @staticmethod
    def check_docker() -> dict[str, Any]:
        """Check Docker availability (optional)."""
        result = {"status": "warning", "docker": None, "compose": None, "message": ""}

        try:
            out = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["docker"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        try:
            out = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0:
                result["compose"] = out.stdout.strip()
        except (FileNotFoundError, Exception):
            pass

        if result["docker"]:
            result["status"] = "ok"
            parts = [result["docker"]]
            if result["compose"]:
                parts.append(result["compose"])
            result["message"] = " · ".join(parts)
        else:
            result["status"] = "warning"
            result["message"] = "Docker 未安装 (可选 — 用于容器化部署)"

        return result

    @staticmethod
    def check_ports() -> list[dict[str, Any]]:
        """Check port availability for all known services."""
        # Dynamically retrieve current WebUI port
        try:
            from webui_config import webui_config

            webui_port = webui_config.port
        except Exception:
            try:
                from .webui_config import webui_config

                webui_port = webui_config.port
            except Exception:
                webui_port = 8088

        results = []
        for name, port in KNOWN_PORTS.items():
            if name == "WebUI":
                port = webui_port
            entry = {
                "name": name,
                "port": port,
                "status": "ok",
                "message": "",
                "pid": None,
            }
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    # Port is in use
                    entry["status"] = "warning"
                    entry["message"] = f"端口 {port} 已被占用"
                    # Try to find PID
                    try:
                        import psutil

                        for conn in psutil.net_connections(kind="inet"):
                            if conn.laddr.port == port and conn.status == "LISTEN":
                                entry["pid"] = conn.pid
                                if conn.pid == os.getpid():
                                    entry["status"] = "ok"
                                    entry["message"] = (
                                        f"端口 {port} 由当前 WebUI 占用 (正常)"
                                    )
                                else:
                                    try:
                                        proc = psutil.Process(conn.pid)
                                        entry["message"] = (
                                            f"端口 {port} 被 {proc.name()} (PID:{conn.pid}) 占用"
                                        )
                                    except Exception:
                                        entry["message"] = (
                                            f"端口 {port} 被占用 (PID:{conn.pid})"
                                        )
                                break
                    except Exception:
                        pass

                    # Fallback: if it is the WebUI port and we are running the check,
                    # we are definitely the one listening on it (or it's the current WebUI instance).
                    if name == "WebUI" and (
                        entry["pid"] is None or entry["pid"] == os.getpid()
                    ):
                        entry["status"] = "ok"
                        entry["message"] = f"端口 {port} 由当前 WebUI 占用 (正常)"
                        if entry["pid"] is None:
                            entry["pid"] = os.getpid()

            except (ConnectionRefusedError, OSError, socket.timeout):
                entry["status"] = "ok"
                entry["message"] = f"端口 {port} 可用"
            results.append(entry)
        return results

    @staticmethod
    def check_configs() -> list[dict[str, Any]]:
        """Check which config files exist and which are missing."""
        results = []
        for tmpl, target in TEMPLATE_MAP.items():
            target_path = ROOT_DIR / target
            tmpl_path = ROOT_DIR / tmpl
            results.append(
                {
                    "template": tmpl,
                    "target": target,
                    "target_exists": target_path.exists(),
                    "template_exists": tmpl_path.exists(),
                    "filename": Path(target).name,
                    "component": target.split("/")[0],
                }
            )
        return results

    @staticmethod
    def check_gpu() -> dict[str, Any]:
        """Check GPU availability and VRAM size (in MB)."""
        result = {
            "status": "ok",
            "has_gpu": False,
            "gpu_name": None,
            "vram_mb": 0.0,
            "message": "未检测到可用 NVIDIA 显卡",
        }

        # 1. Try nvidia-smi (reliable for NVIDIA CUDA GPUs)
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True,
            )
            if out.returncode == 0:
                lines = [
                    line.strip()
                    for line in out.stdout.strip().split("\n")
                    if line.strip()
                ]
                gpus = []
                for line in lines:
                    parts = line.split(",")
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        try:
                            vram = float(parts[1].strip())
                        except ValueError:
                            vram = 0.0
                        gpus.append((name, vram))
                if gpus:
                    gpus.sort(key=lambda x: x[1], reverse=True)
                    best_gpu = gpus[0]
                    result["has_gpu"] = True
                    result["gpu_name"] = best_gpu[0]
                    result["vram_mb"] = best_gpu[1]
                    vram_gb = best_gpu[1] / 1024.0
                    result["message"] = f"{best_gpu[0]} (显存 {vram_gb:.2f} GB)"
                    return result
        except Exception:
            pass

        # 2. Try wmic path win32_VideoController (fallback to check all GPUs)
        try:
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "Name,AdapterRAM"],
                capture_output=True,
                text=True,
                timeout=5,
                shell=True,
            )
            if out.returncode == 0:
                lines = [
                    line.strip()
                    for line in out.stdout.strip().split("\n")
                    if line.strip()
                ]
                if len(lines) > 1:
                    gpus = []
                    header = lines[0].lower()
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) >= 2:
                            try:
                                if header.startswith("adapterram"):
                                    ram_str = parts[0]
                                    name_str = " ".join(parts[1:])
                                else:
                                    ram_str = parts[-1]
                                    name_str = " ".join(parts[:-1])

                                ram_bytes = float(ram_str.strip())
                                vram_mb = ram_bytes / (1024.0 * 1024.0)
                                name = name_str.strip()
                                gpus.append((name, vram_mb))
                            except ValueError:
                                pass
                    if gpus:
                        gpus.sort(key=lambda x: x[1], reverse=True)
                        best_gpu = gpus[0]
                        is_nvidia = "nvidia" in best_gpu[0].lower()
                        result["has_gpu"] = is_nvidia
                        result["gpu_name"] = best_gpu[0]
                        result["vram_mb"] = best_gpu[1]
                        vram_gb = best_gpu[1] / 1024.0
                        if is_nvidia:
                            result["message"] = f"{best_gpu[0]} (显存 {vram_gb:.2f} GB)"
                        else:
                            result["message"] = (
                                f"{best_gpu[0]} (非 NVIDIA 显卡，显存 {vram_gb:.2f} GB)"
                            )
                        return result
        except Exception:
            pass

        return result


# =========================================================================
# Backup Manager


class PathVerifier:
    """Verify that external dependencies are installed at the given paths."""

    # Each entry: (check_type, display_name, validation function, download_url)
    CHECKS = {
        "napcat": {
            "name": "NapCat Shell",
            "hint": "NapCat Shell 安装目录（包含 launcher-user.bat）",
            "download_url": "https://github.com/NapNeko/NapCatQQ/releases",
            "default_rel": "NapCat.Shell",
        },
        "sovits": {
            "name": "GPT-SoVITS",
            "hint": "GPT-SoVITS 安装目录（包含 runtime/python.exe）",
            "download_url": "https://www.yuque.com/baicaigongchang1145haoyuangong/ib3g1e/dkxgpiy9zb96hob4",
            "default_rel": None,
        },
        "voxcpm": {
            "name": "VoxCPM",
            "hint": "VoxCPM 安装目录（包含 .venv/Scripts/python.exe）",
            "download_url": "https://github.com/openbmb/VoxCPM/releases",
            "default_rel": None,
        },
        "nodejs": {
            "name": "Node.js",
            "hint": "系统已安装 Node.js（自动检测 PATH）",
            "download_url": "https://nodejs.org/en/download/",
            "default_rel": None,
        },
        "bilibili_dll": {
            "name": "Live2D Cubism Core",
            "hint": "NachoBot-Bilibili-Adapter 目录下的 Live2DCubismCore.dll",
            "download_url": "https://www.live2d.com/sdk/download/native/",
            "default_rel": None,
        },
        "vb_cable": {
            "name": "VB-Audio Virtual Cable",
            "hint": "VB-Audio Virtual Cable 安装目录（包含 VBCABLE_Setup_x64.exe）",
            "download_url": "https://vb-audio.com/Cable/",
            "default_rel": None,
        },
    }

    @staticmethod
    def verify_path(check_type: str, path: str = "") -> dict[str, Any]:
        """
        Verify an external dependency.

        Returns:
            {"valid": bool, "message": str, "download_url": str}
        """
        info = PathVerifier.CHECKS.get(check_type)
        if not info:
            return {
                "valid": False,
                "message": f"未知检查类型: {check_type}",
                "download_url": "",
            }

        download_url = info["download_url"]

        # -- Node.js: check via PATH, no user path needed --
        if check_type == "nodejs":
            return PathVerifier._check_nodejs(download_url)

        # -- Bilibili DLL: fixed path under project root --
        if check_type == "bilibili_dll":
            return PathVerifier._check_bilibili_dll(download_url)

        # -- Path-based checks --
        if not path or not path.strip():
            return {
                "valid": False,
                "message": "请输入路径",
                "download_url": download_url,
            }

        p = Path(path.strip())
        if not p.exists():
            return {
                "valid": False,
                "message": f"路径不存在: {p}",
                "download_url": download_url,
            }
        if not p.is_dir():
            return {
                "valid": False,
                "message": f"路径不是目录: {p}",
                "download_url": download_url,
            }

        if check_type == "napcat":
            return PathVerifier._check_napcat(p, download_url)
        elif check_type == "sovits":
            return PathVerifier._check_sovits(p, download_url)
        elif check_type == "voxcpm":
            return PathVerifier._check_voxcpm(p, download_url)
        elif check_type == "vb_cable":
            return PathVerifier._check_vb_cable(p, download_url)

        return {"valid": False, "message": "未知检查类型", "download_url": download_url}

    @staticmethod
    def _check_napcat(p: Path, download_url: str) -> dict:
        launcher = p / "launcher-user.bat"
        if launcher.exists():
            return {"valid": True, "message": f"✅ NapCat Shell 已找到: {p}"}
        # Also try napcat.bat as fallback
        napcat_bat = p / "napcat.bat"
        if napcat_bat.exists():
            return {"valid": True, "message": f"✅ NapCat Shell 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 launcher-user.bat: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_sovits(p: Path, download_url: str) -> dict:
        py_exe = p / "runtime" / "python.exe"
        if py_exe.exists():
            return {"valid": True, "message": f"✅ GPT-SoVITS 已找到: {p}"}
        # Alternative: check for api_v2.py
        api_file = p / "api_v2.py"
        if api_file.exists():
            return {"valid": True, "message": f"✅ GPT-SoVITS 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 runtime/python.exe 或 api_v2.py: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_voxcpm(p: Path, download_url: str) -> dict:
        venv_py = p / ".venv" / "Scripts" / "python.exe"
        if venv_py.exists():
            return {"valid": True, "message": f"✅ VoxCPM 已找到: {p}"}
        # Also accept if models dir exists
        models_dir = p / "models"
        if models_dir.exists():
            return {"valid": True, "message": f"✅ VoxCPM 已找到 (models目录): {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 .venv/Scripts/python.exe: {p}",
            "download_url": download_url,
        }

    @staticmethod
    def _check_nodejs(download_url: str) -> dict:
        try:
            result = subprocess.run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                return {"valid": True, "message": f"✅ Node.js 已安装: {ver}"}
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return {
            "valid": False,
            "message": "❌ 未检测到 Node.js，Discord (Koishi) 适配器需要 Node.js",
            "download_url": download_url,
        }

    @staticmethod
    def _check_bilibili_dll(download_url: str) -> dict:
        dll_path = ROOT_DIR / "NachoBot-Bilibili-Adapter" / "Live2DCubismCore.dll"
        if dll_path.exists():
            return {"valid": True, "message": "✅ Live2DCubismCore.dll 已找到"}
        return {
            "valid": False,
            "message": "❌ 未找到 NachoBot-Bilibili-Adapter/Live2DCubismCore.dll",
            "download_url": download_url,
        }

    @staticmethod
    def _check_vb_cable(p: Path, download_url: str) -> dict:
        """Verify VB-Audio Virtual Cable installation directory."""
        # Check for the setup executable (main indicator)
        setup_x64 = p / "VBCABLE_Setup_x64.exe"
        setup_x86 = p / "VBCABLE_Setup.exe"
        # Also accept the driver file directly
        driver_cat = p / "vbaudio_cable64_win10.cat"
        if setup_x64.exists() or setup_x86.exists() or driver_cat.exists():
            return {"valid": True, "message": f"✅ VB-Audio Virtual Cable 已找到: {p}"}
        # Fuzzy check: look for any VB-Audio related exe or sys file
        vb_files = list(p.glob("VBCABLE*")) + list(p.glob("vbaudio*"))
        if vb_files:
            return {"valid": True, "message": f"✅ VB-Audio Virtual Cable 已找到: {p}"}
        return {
            "valid": False,
            "message": f"❌ 未找到 VB-Audio Virtual Cable 安装文件: {p}",
            "download_url": download_url,
        }


# =========================================================================
# NapCat Configurator — auto-configure onebot11 connections
