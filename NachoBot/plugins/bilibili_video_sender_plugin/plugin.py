from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import tempfile
import time
import urllib.parse
import urllib.request
import subprocess
import shutil
import aiohttp
import uuid
import time

from typing import Any, Dict, List, Optional, Tuple, Type

from src.common.logger import get_logger

# 为模块级独立函数创建logger
_utils_logger = get_logger("plugin.bilibili_video_sender.utils")


def convert_windows_to_wsl_path(windows_path: str) -> str:
    """将Windows路径转换为WSL路径

    例如：E:\path\to\file.mp4 -> /mnt/e/path/to/file.mp4
    """
    try:
        # 尝试使用wslpath命令转换路径（从Windows调用WSL）
        try:
            # 在Windows上调用wsl wslpath命令
            result = subprocess.run(["wsl", "wslpath", "-u", windows_path], capture_output=True, text=False, check=True)
            wsl_path = result.stdout.decode("utf-8", errors="replace").strip()
            if wsl_path:
                return wsl_path
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # 如果wslpath命令失败，手动转换路径
        # 移除盘符中的冒号，将反斜杠转换为正斜杠
        if re.match(r"^[a-zA-Z]:", windows_path):
            drive = windows_path[0].lower()
            path = windows_path[2:].replace("\\", "/")
            return f"/mnt/{drive}/{path}"
        return windows_path
    except Exception:
        # 转换失败时返回原路径
        return windows_path


from src.plugin_system.base import (
    BaseAction,
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    ComponentInfo,
)
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.base.component_types import (
    ActionActivationType,
    EventType,
    NachoMessages,
)
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.apis import send_api


class FFmpegManager:
    """跨平台FFmpeg管理器"""

    _logger = get_logger("plugin.bilibili_video_sender.ffmpeg_manager")

    def __init__(self):
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.system = platform.system().lower()
        self.ffmpeg_dir = os.path.join(self.plugin_dir, "ffmpeg")

    def get_ffmpeg_path(self) -> Optional[str]:
        """获取ffmpeg可执行文件路径"""
        return self._get_executable_path("ffmpeg")

    def get_ffprobe_path(self) -> Optional[str]:
        """获取ffprobe可执行文件路径"""
        return self._get_executable_path("ffprobe")

    def _get_executable_path(self, executable_name: str) -> Optional[str]:
        """根据操作系统获取可执行文件路径"""
        # 确定可执行文件名称和路径
        if self.system == "windows":
            bin_dir = os.path.join(self.ffmpeg_dir, "bin")
            executable_path = os.path.join(bin_dir, f"{executable_name}.exe")
        elif self.system in ["linux", "darwin"]:  # Linux 和 macOS
            # 优先检查平台特定的目录
            platform_bin_dir = os.path.join(self.ffmpeg_dir, "bin", self.system)
            executable_path = os.path.join(platform_bin_dir, executable_name)

            # 如果平台特定目录不存在，检查通用bin目录
            if not os.path.exists(executable_path):
                bin_dir = os.path.join(self.ffmpeg_dir, "bin")
                executable_path = os.path.join(bin_dir, executable_name)
        else:
            self._logger.warning(f"不支持的操作系统: {self.system}")
            return None

        # 检查插件内置的ffmpeg
        if os.path.exists(executable_path):
            self._logger.debug(f"Found bundled {executable_name}: {executable_path}")
            return executable_path

        # 检查系统PATH中的ffmpeg
        system_executable = shutil.which(executable_name)
        if system_executable:
            self._logger.debug(f"Found system {executable_name}: {system_executable}")
            return system_executable

        self._logger.warning(f"未找到{executable_name}可执行文件")
        return None

    def check_hardware_encoders(self) -> Dict[str, Any]:
        """检测可用的硬件编码器"""
        ffmpeg_path = self.get_ffmpeg_path()
        if not ffmpeg_path:
            return {"available_encoders": [], "recommended_encoder": "libx264"}

        available_encoders = []

        # 定义要检测的硬件编码器列表（按优先级排序）
        encoders_to_check = [
            # NVIDIA GPU 编码器
            {"name": "h264_nvenc", "type": "nvidia", "codec": "h264", "description": "NVIDIA H.264硬件编码"},
            {"name": "hevc_nvenc", "type": "nvidia", "codec": "h265", "description": "NVIDIA H.265硬件编码"},
            # Intel Quick Sync Video
            {"name": "h264_qsv", "type": "intel", "codec": "h264", "description": "Intel QSV H.264硬件编码"},
            {"name": "hevc_qsv", "type": "intel", "codec": "h265", "description": "Intel QSV H.265硬件编码"},
            # AMD GPU 编码器
            {"name": "h264_amf", "type": "amd", "codec": "h264", "description": "AMD H.264硬件编码"},
            {"name": "hevc_amf", "type": "amd", "codec": "h265", "description": "AMD H.265硬件编码"},
            # Apple VideoToolbox (macOS)
            {"name": "h264_videotoolbox", "type": "apple", "codec": "h264", "description": "Apple H.264硬件编码"},
            {"name": "hevc_videotoolbox", "type": "apple", "codec": "h265", "description": "Apple H.265硬件编码"},
        ]

        try:
            # 获取所有可用的编码器
            cmd = [ffmpeg_path, "-encoders"]
            process = subprocess.run(cmd, capture_output=True, text=False, timeout=15)

            if process.returncode == 0:
                encoders_output = process.stdout.decode("utf-8", errors="replace")

                # 检查每个硬件编码器是否可用
                for encoder in encoders_to_check:
                    if encoder["name"] in encoders_output:
                        # 进一步测试编码器是否真正可用
                        if self._test_encoder(ffmpeg_path, encoder["name"]):
                            available_encoders.append(encoder)
                            self._logger.debug(f"Found available encoder: {encoder['description']}")
                        else:
                            self._logger.debug(f"Encoder {encoder['name']} exists but unavailable")
            else:
                stderr_text = process.stderr.decode("utf-8", errors="replace") if process.stderr else ""
                self._logger.warning(f"获取编码器列表失败: {stderr_text}")

        except Exception as e:
            self._logger.warning(f"检测硬件编码器时发生错误: {e}")

        # 确定推荐的编码器
        recommended_encoder = self._get_recommended_encoder(available_encoders)

        result = {
            "available_encoders": available_encoders,
            "recommended_encoder": recommended_encoder,
            "total_hardware_encoders": len(available_encoders),
        }

        self._logger.debug(
            f"Hardware encoder detection complete: {len(available_encoders)} available, recommend: {recommended_encoder}"
        )
        return result

    def _test_encoder(self, ffmpeg_path: str, encoder_name: str) -> bool:
        """测试编码器是否真正可用"""
        try:
            # 创建一个1秒的测试视频来验证编码器
            cmd = [
                ffmpeg_path,
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=320x240:rate=1",
                "-c:v",
                encoder_name,
                "-t",
                "1",
                "-f",
                "null",
                "-",
            ]

            process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
            return process.returncode == 0

        except Exception:
            return False

    def _get_recommended_encoder(self, available_encoders: List[Dict[str, Any]]) -> str:
        """根据可用编码器选择推荐的编码器"""
        if not available_encoders:
            return "libx264"  # 默认软件编码器

        # 优先级排序：NVIDIA > Intel > AMD > Apple
        priority_order = ["nvidia", "intel", "amd", "apple"]

        for encoder_type in priority_order:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]

        # 如果没有H.264硬件编码器，返回第一个可用的
        return available_encoders[0]["name"]

    def check_ffmpeg_availability(self) -> Dict[str, Any]:
        """检查FFmpeg可用性"""
        result = {
            "ffmpeg_available": False,
            "ffprobe_available": False,
            "ffmpeg_path": None,
            "ffprobe_path": None,
            "ffmpeg_version": None,
            "system": self.system,
            "hardware_acceleration": {},
        }

        # 检查ffmpeg
        ffmpeg_path = self.get_ffmpeg_path()
        if ffmpeg_path:
            result["ffmpeg_available"] = True
            result["ffmpeg_path"] = ffmpeg_path

            try:
                # 获取ffmpeg版本信息
                cmd = [ffmpeg_path, "-version"]
                process = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
                if process.returncode == 0:
                    stdout_text = process.stdout.decode("utf-8", errors="replace")
                    version_line = stdout_text.split("\n")[0] if stdout_text else ""
                    result["ffmpeg_version"] = version_line
                    self._logger.debug(f"FFmpeg version: {version_line}")

                    # 检测硬件编码器
                    result["hardware_acceleration"] = self.check_hardware_encoders()
            except Exception as e:
                self._logger.warning(f"Failed to get FFmpeg version: {e}")

        # 检查ffprobe
        ffprobe_path = self.get_ffprobe_path()
        if ffprobe_path:
            result["ffprobe_available"] = True
            result["ffprobe_path"] = ffprobe_path

        self._logger.debug(
            f"FFmpeg availability check: ffmpeg={result['ffmpeg_available']}, ffprobe={result['ffprobe_available']}"
        )
        return result


# 全局FFmpeg管理器实例
_ffmpeg_manager = FFmpegManager()


def _prepare_split_dir() -> str:
    """清理旧目录并为本次任务准备唯一的data/split子目录。"""
    plugin_dir = os.path.dirname(os.path.abspath(__file__))
    base_split_dir = os.path.join(plugin_dir, "data", "split")

    # 1. 尝试清理过期的子目录 (超过1小时)
    try:
        if os.path.exists(base_split_dir):
            current_time = time.time()
            for item in os.listdir(base_split_dir):
                item_path = os.path.join(base_split_dir, item)
                if os.path.isdir(item_path):
                    try:
                        # 检查修改时间
                        mtime = os.path.getmtime(item_path)
                        if current_time - mtime > 3600:  # 1小时
                            shutil.rmtree(item_path)
                            _utils_logger.debug(f"已清理过期分块目录: {item}")
                    except Exception as e:
                        # 忽略清理错误 (可能是被锁定)
                        pass
    except Exception as e:
        _utils_logger.warning(f"清理旧分块目录出错: {e}")

    # 2. 创建本次任务的唯一目录
    unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    task_split_dir = os.path.join(base_split_dir, unique_name)

    try:
        os.makedirs(task_split_dir, exist_ok=True)
        _utils_logger.debug(f"为本次任务创建分块目录: {task_split_dir}")
    except Exception as e:
        _utils_logger.error(f"创建分块目录失败: {e}")
        # 如果创建失败，回退到使用 base_split_dir (虽然可能也会失败)
        os.makedirs(base_split_dir, exist_ok=True)
        return base_split_dir

    return task_split_dir


class ProgressBar:
    """进度条显示类"""

    def __init__(self, total_size: int, description: str = "下载进度", bar_length: int = 30):
        self.total_size = total_size
        self.description = description
        self.bar_length = bar_length
        self.current_size = 0
        self.last_update = 0
        self.update_interval = 0.1  # 100ms更新一次，避免过于频繁

    def update(self, downloaded: int):
        """更新进度"""
        self.current_size = downloaded
        current_time = time.time()

        # 控制更新频率，避免过于频繁的日志输出
        if current_time - self.last_update < self.update_interval:
            return

        self.last_update = current_time

        # 计算进度百分比
        if self.total_size > 0:
            percentage = (downloaded / self.total_size) * 100
        else:
            percentage = 0

        # 计算进度条填充长度
        filled_length = int(self.bar_length * downloaded // self.total_size) if self.total_size > 0 else 0

        # 构建进度条
        bar = "█" * filled_length + "░" * (self.bar_length - filled_length)

        # 格式化文件大小显示
        downloaded_mb = downloaded / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024) if self.total_size > 0 else 0

        # 输出进度条
        print(
            f"\r{self.description}: [{bar}] {percentage:5.1f}% ({downloaded_mb:6.1f}MB/{total_mb:6.1f}MB)",
            end="",
            flush=True,
        )

    def finish(self):
        """完成进度条显示"""
        # 确保显示100%
        self.update(self.total_size)
        print()  # 换行


class BilibiliVideoInfo:
    """基础视频信息。"""

    def __init__(self, aid: int, cid: int, title: str, bvid: Optional[str] = None, duration: int = 0):
        self.aid = aid
        self.cid = cid
        self.title = title
        self.bvid = bvid
        self.duration = duration


class BilibiliParser:
    """哔哩哔哩链接解析器。"""

    _logger = get_logger("plugin.bilibili_video_sender.parser")

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    VIDEO_URL_PATTERN = re.compile(
        r"https?://(?:www\.)?bilibili\.com/video/(?P<bv>BV[\w]+|av\d+)",
        re.IGNORECASE,
    )
    B23_SHORT_PATTERN = re.compile(r"https?://b23\.tv/[\w]+", re.IGNORECASE)

    @staticmethod
    def _build_request(url: str, headers: Optional[Dict[str, str]] = None) -> urllib.request.Request:
        default_headers = {
            "User-Agent": BilibiliParser.USER_AGENT,
            "Referer": "https://www.bilibili.com/",
        }
        if headers:
            default_headers.update(headers)
        return urllib.request.Request(url, headers=default_headers)

    @staticmethod
    def _fetch_json(url: str) -> Dict[str, Any]:
        req = BilibiliParser._build_request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
            data = resp.read()
        return json.loads(data.decode("utf-8", errors="ignore"))

    @staticmethod
    def _follow_redirect(url: str) -> str:
        req = BilibiliParser._build_request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public short URL
            return resp.geturl()

    @staticmethod
    def _extract_bvid(url: str) -> Optional[str]:
        match = BilibiliParser.VIDEO_URL_PATTERN.search(url)
        if not match:
            return None
        raw_id = match.group("bv")
        if raw_id.lower().startswith("bv"):
            return raw_id
        # 兼容 av 号：需要先通过 view 接口查询 bvid
        return None

    @staticmethod
    def find_first_bilibili_url(text: str) -> Optional[str]:
        # 先匹配 b23.tv 短链
        short = BilibiliParser.B23_SHORT_PATTERN.search(text)
        if short:
            try:
                return BilibiliParser._follow_redirect(short.group(0))
            except Exception:
                # 回退为原短链
                return short.group(0)

        # 再匹配标准视频链接
        match = BilibiliParser.VIDEO_URL_PATTERN.search(text)
        if match:
            return match.group(0)
        return None

    @staticmethod
    def get_view_info_by_url(url: str) -> Optional[BilibiliVideoInfo]:
        # 优先解析 BV 号
        bvid = BilibiliParser._extract_bvid(url)

        query: str
        if bvid:
            query = f"bvid={urllib.parse.quote(bvid)}"
        else:
            # 兜底：尝试从路径中提取 av 号
            m = re.search(r"/video/av(?P<aid>\d+)", url)
            if not m:
                return None
            aid = m.group("aid")
            query = f"aid={aid}"

        api = f"https://api.bilibili.com/x/web-interface/view?{query}"
        payload = BilibiliParser._fetch_json(api)
        if payload.get("code") != 0:
            return None

        data = payload.get("data", {})
        pages = data.get("pages") or []
        if not pages:
            return None

        first_page = pages[0]
        return BilibiliVideoInfo(
            aid=int(data.get("aid")),
            cid=int(first_page.get("cid")),
            title=str(data.get("title", "")),
            bvid=str(data.get("bvid", "")) or None,
            duration=int(data.get("duration", 0) or first_page.get("duration", 0)),
        )

    @staticmethod
    def get_play_urls(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], str]:
        opts = options or {}

        # 配置参数
        BilibiliParser._logger.debug("Starting to fetch video playback URLs", aid=aid, cid=cid)

        use_wbi = bool(opts.get("use_wbi", True))
        prefer_dash = bool(opts.get("prefer_dash", True))
        fnval = int(opts.get("fnval", 4048 if prefer_dash else 1))
        fourk = 1 if bool(opts.get("fourk", True)) else 0
        qn = int(opts.get("qn", 0))
        platform = str(opts.get("platform", "pc"))
        high_quality = 1 if bool(opts.get("high_quality", False)) else 0
        try_look = 1 if bool(opts.get("try_look", False)) else 0
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()

        # 鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)

        if not has_cookie:
            BilibiliParser._logger.warning("未提供Cookie，将使用游客模式（清晰度限制）")

        # 清晰度选择逻辑优化
        if qn == 0:
            if has_cookie:
                qn = 64  # 登录后默认720P
            else:
                qn = 32  # 未登录默认480P
        else:
            # 检查清晰度权限
            qn_info = {
                6: "240P",
                16: "360P",
                32: "480P",
                64: "720P",
                80: "1080P",
                112: "1080P+",
                116: "1080P60",
                120: "4K",
                125: "HDR",
                126: "杜比视界",
            }
            qn_name = qn_info.get(qn, f"未知({qn})")

            # 清晰度权限检查
            if qn >= 64 and not has_cookie:
                BilibiliParser._logger.warning(f"请求{qn_name}清晰度但未登录，可能失败")
            if qn >= 80 and not has_cookie:
                BilibiliParser._logger.warning(f"请求{qn_name}清晰度需要大会员账号")
            if qn >= 116 and not has_cookie:
                BilibiliParser._logger.warning(f"请求{qn_name}高帧率需要大会员账号")
            if qn >= 125 and not has_cookie:
                BilibiliParser._logger.warning(f"请求{qn_name}需要大会员账号")

        # 构建请求参数
        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fnver": "0",
            "fnval": str(fnval),
            "fourk": str(fourk),
            "platform": platform,
        }

        if qn > 0:
            params["qn"] = str(qn)

        if high_quality:
            params["high_quality"] = "1"

        if try_look:
            params["try_look"] = "1"

        if buvid3:
            # 生成 session: md5(buvid3 + 当前毫秒)
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash

        # 添加gaia_source参数（有Cookie时非必要）
        if not has_cookie:
            params["gaia_source"] = "view-card"

        # WBI 签名
        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )

        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        # 构建请求头：可带 Cookie
        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)
            headers["gaia_source"] = sessdata  # 添加 gaia_source
        else:
            BilibiliParser._logger.info("使用游客模式")

        # 发起请求
        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            BilibiliParser._logger.error(f"HTTP请求失败: {e}")
            return [], f"网络请求失败: {e}"

        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            BilibiliParser._logger.error(f"JSON解析失败: {e}")
            return [], "响应数据格式错误"

        if payload.get("code") != 0:
            error_msg = payload.get("message", "接口返回错误")
            BilibiliParser._logger.error(f"API返回错误: code={payload.get('code')}, message={error_msg}")
            return [], error_msg

        BilibiliParser._logger.debug("API请求成功，开始解析响应数据")
        data = payload.get("data", {})

        # 处理dash格式
        dash = data.get("dash")
        if not dash:
            BilibiliParser._logger.debug("未找到dash格式数据")
            # 检查是否有durl格式
            durl = data.get("durl")
            if durl:
                BilibiliParser._logger.debug(f"找到durl格式数据，共{len(durl)}个文件")
                # 处理durl格式
                candidates = []
                for i, item in enumerate(durl):
                    url = item.get("baseUrl") or item.get("base_url")
                    if url:
                        candidates.append(url.replace("http:", "https:"))
                        BilibiliParser._logger.info(f"添加durl文件{i + 1}: {url[:50]}...")
                if candidates:
                    return candidates, "ok (durl格式)"
            return [], "未找到dash数据"

        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        BilibiliParser._logger.debug(f"找到{len(videos)}个视频流和{len(audios)}个音频流")

        # 记录视频流详细信息
        if videos:
            BilibiliParser._logger.debug("Video stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Resolution':<12} {'Codec':<25} {'Bitrate':<10} {'FPS':<10}")
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                BilibiliParser._logger.debug(
                    f"{i + 1:<4} {width}x{height:<8} {codec:<25} {bandwidth // 1000:<10}kbps {frame_rate:<10}"
                )

        # 记录音频流详细信息
        if audios:
            BilibiliParser._logger.debug("Audio stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []

        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            BilibiliParser._logger.debug(f"Found {len(dolby_audios)} Dolby audio streams")
            if dolby_audios:
                BilibiliParser._logger.debug("Dolby audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            BilibiliParser._logger.debug(f"Found {len(flac_audios)} FLAC audio stream")
            if flac_audios:
                BilibiliParser._logger.debug("FLAC audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        # 合并所有音频流
        all_audios = audios + dolby_audios + flac_audios

        if not videos:
            BilibiliParser._logger.warning("未找到视频流")
            return [], "未找到视频流"

        if not all_audios:
            BilibiliParser._logger.warning("未找到音频流")

        # 参考原脚本，按照质量排序（降序）
        videos.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)

        candidates = []

        # 参考原脚本，选择最高质量的视频流
        if videos:
            best_video = videos[0]
            video_url = best_video.get("baseUrl") or best_video.get("base_url")
            if video_url:
                candidates.append(video_url.replace("http:", "https:"))
                codec = best_video.get("codecs", "unknown")
                bandwidth = best_video.get("bandwidth", 0)
                width = best_video.get("width", 0)
                height = best_video.get("height", 0)
                BilibiliParser._logger.debug(
                    f"Selected best video stream: {width}x{height}, {codec}, {bandwidth // 1000}kbps"
                )

        # 参考原脚本，选择最高质量的音频流
        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"Selected best audio stream: {codec}, {bandwidth // 1000}kbps")

        if candidates:
            BilibiliParser._logger.debug(f"Got {len(candidates)} playback URLs")
            return candidates, "ok"

        BilibiliParser._logger.error("Failed to get playback URLs")
        return [], "未获取到播放地址"

    @staticmethod
    def get_play_urls_force_dash(
        aid: int,
        cid: int,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], str]:
        """强制获取dash格式的视频和音频流"""
        opts = options or {}

        BilibiliParser._logger.debug(f"=== Force fetch DASH format ===")
        BilibiliParser._logger.debug(f"Video ID: aid={aid}, cid={cid}")
        BilibiliParser._logger.debug(f"Config: {opts}")

        use_wbi = bool(opts.get("use_wbi", True))
        fnval = 4048  # 强制使用DASH格式
        fourk = 1 if bool(opts.get("fourk", True)) else 0
        platform = str(opts.get("platform", "pc"))
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()

        # 记录鉴权状态
        has_cookie = bool(sessdata)
        has_buvid3 = bool(buvid3)
        BilibiliParser._logger.debug(f"Force DASH auth: has_cookie={has_cookie}, has_buvid3={has_buvid3}")

        if not has_cookie:
            BilibiliParser._logger.warning("Force DASH: no Cookie, may affect HD fetching")

        params: Dict[str, Any] = {
            "avid": str(aid),
            "cid": str(cid),
            "otype": "json",
            "fourk": str(fourk),
            "fnver": "0",
            "fnval": str(fnval),
            "platform": platform,
        }

        if buvid3:
            ms = str(int(time.time() * 1000))
            session_hash = hashlib.md5((buvid3 + ms).encode("utf-8")).hexdigest()
            params["session"] = session_hash

        # 添加gaia_source参数（有Cookie时非必要）
        if not has_cookie:
            params["gaia_source"] = "view-card"

        api_base = (
            "https://api.bilibili.com/x/player/wbi/playurl" if use_wbi else "https://api.bilibili.com/x/player/playurl"
        )

        final_params = BilibiliWbiSigner.sign_params(params) if use_wbi else params
        query = urllib.parse.urlencode(final_params)
        api = f"{api_base}?{query}"

        headers: Dict[str, str] = {}
        if sessdata:
            cookie_parts = [f"SESSDATA={sessdata}"]
            if buvid3:
                cookie_parts.append(f"buvid3={buvid3}")
            headers["Cookie"] = "; ".join(cookie_parts)
            headers["gaia_source"] = sessdata  # 添加 gaia_source

        try:
            req = BilibiliParser._build_request(api, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec - trusted public API
                data_bytes = resp.read()
        except Exception as e:
            BilibiliParser._logger.error(f"Force DASH HTTP error: {e}")
            return [], f"Force DASH network error: {e}"

        try:
            payload = json.loads(data_bytes.decode("utf-8", errors="ignore"))
        except Exception as e:
            BilibiliParser._logger.error(f"Force DASH JSON parse error: {e}")
            return [], "Force DASH response format error"

        if payload.get("code") != 0:
            error_msg = payload.get("message", "API error")
            BilibiliParser._logger.error(f"Force DASH API error: code={payload.get('code')}, msg={error_msg}")
            return [], error_msg

        BilibiliParser._logger.debug("Force DASH request successful, parsing response")
        data = payload.get("data", {})

        # 检查是否仍然返回durl格式
        durl = data.get("durl")
        if durl:
            BilibiliParser._logger.debug(f"Force DASH also returned durl format: {len(durl)} files (single-file only)")
            # 记录durl文件信息
            for i, item in enumerate(durl):
                url = item.get("baseUrl") or item.get("base_url")
                size = item.get("size", 0)
                BilibiliParser._logger.info(
                    f"Force DASH durl文件{i + 1}: 大小={size // 1024 // 1024}MB, URL={url[:50]}..."
                )
            return [], "Video has single-file format only"

        dash = data.get("dash")
        if not dash:
            BilibiliParser._logger.warning("Force DASH: no dash data found")
            # 检查其他可能的数据结构
            BilibiliParser._logger.info(f"Force DASH response data structure: {list(data.keys())}")
            return [], "No dash data"

        videos = dash.get("video") or []
        audios = dash.get("audio") or []

        BilibiliParser._logger.debug(f"Force DASH: {len(videos)} video streams, {len(audios)} audio streams")

        # 记录视频流详细信息（表格格式）
        if videos:
            BilibiliParser._logger.debug("Force DASH video stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Resolution':<12} {'Codec':<25} {'Bitrate':<10} {'FPS':<10}")
            for i, video in enumerate(videos):
                codec = video.get("codecs", "unknown")
                bandwidth = video.get("bandwidth", 0)
                width = video.get("width", 0)
                height = video.get("height", 0)
                frame_rate = video.get("frameRate", "unknown")
                BilibiliParser._logger.debug(
                    f"{i + 1:<4} {width}x{height:<8} {codec:<25} {bandwidth // 1000:<10}kbps {frame_rate:<10}"
                )

        # 记录音频流详细信息（表格格式）
        if audios:
            BilibiliParser._logger.debug("Force DASH audio stream details:")
            BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
            for i, audio in enumerate(audios):
                codec = audio.get("codecs", "unknown")
                bandwidth = audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        # 参考原脚本，处理杜比和flac音频
        dolby_audios = []
        flac_audios = []

        dolby = dash.get("dolby")
        if dolby and dolby.get("audio"):
            dolby_audios = dolby.get("audio", [])
            if dolby_audios:
                BilibiliParser._logger.debug("Force DASH Dolby audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(dolby_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        flac = dash.get("flac")
        if flac and flac.get("audio"):
            flac_audios = [flac.get("audio")]
            if flac_audios:
                BilibiliParser._logger.debug("Force DASH FLAC audio stream details:")
                BilibiliParser._logger.debug(f"{'No.':<4} {'Codec':<25} {'Bitrate':<10}")
                for i, audio in enumerate(flac_audios):
                    codec = audio.get("codecs", "unknown")
                    bandwidth = audio.get("bandwidth", 0)
                    BilibiliParser._logger.debug(f"{i + 1:<4} {codec:<25} {bandwidth // 1000:<10}kbps")

        all_audios = audios + dolby_audios + flac_audios

        if not videos or not all_audios:
            BilibiliParser._logger.warning(
                f"Force DASH: missing streams - video={len(videos)}, audio={len(all_audios)}"
            )
            return [], "Missing video or audio streams"

        # 按照质量排序
        videos.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        all_audios.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)

        candidates = []

        # 获取最高质量的视频和音频流
        if videos:
            best_video = videos[0]
            video_url = best_video.get("baseUrl") or best_video.get("base_url")
            if video_url:
                candidates.append(video_url.replace("http:", "https:"))
                codec = best_video.get("codecs", "unknown")
                bandwidth = best_video.get("bandwidth", 0)
                width = best_video.get("width", 0)
                height = best_video.get("height", 0)
                BilibiliParser._logger.debug(
                    f"Force DASH selected video: {width}x{height}, {codec}, {bandwidth // 1000}kbps"
                )

        if all_audios:
            best_audio = all_audios[0]
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            if audio_url:
                candidates.append(audio_url.replace("http:", "https:"))
                codec = best_audio.get("codecs", "unknown")
                bandwidth = best_audio.get("bandwidth", 0)
                BilibiliParser._logger.debug(f"Force DASH selected audio: {codec}, {bandwidth // 1000}kbps")

        if len(candidates) >= 2:
            BilibiliParser._logger.debug("Force DASH: got complete video and audio streams")
            return candidates, "ok"
        else:
            BilibiliParser._logger.warning("Force DASH: incomplete streams")
            return candidates, "Incomplete video and audio streams"

    @staticmethod
    def validate_config(options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """验证配置参数的有效性"""

        opts = options or {}
        validation_result = {"valid": True, "warnings": [], "errors": [], "recommendations": []}

        # 检查Cookie配置
        sessdata = str(opts.get("sessdata", "")).strip()
        buvid3 = str(opts.get("buvid3", "")).strip()

        if not sessdata:
            validation_result["warnings"].append("未配置SESSDATA，将使用游客模式")
            validation_result["recommendations"].append("建议配置SESSDATA以获得更好的清晰度和功能")
        else:
            if len(sessdata) < 10:
                validation_result["errors"].append("SESSDATA长度异常，可能配置错误")
                validation_result["valid"] = False

        if not buvid3:
            validation_result["warnings"].append("未配置Buvid3，session参数生成可能失败")
            validation_result["recommendations"].append("建议配置Buvid3以确保session参数正常生成")
        else:
            if len(buvid3) < 10:
                validation_result["errors"].append("Buvid3长度异常，可能配置错误")
                validation_result["valid"] = False

        # 检查清晰度配置
        qn = int(opts.get("qn", 0))
        if qn > 0:
            qn_info = {
                6: "240P",
                16: "360P",
                32: "480P",
                64: "720P",
                80: "1080P",
                112: "1080P+",
                116: "1080P60",
                120: "4K",
                125: "HDR",
                126: "杜比视界",
            }
            qn_name = qn_info.get(qn, f"未知({qn})")

            if qn >= 64 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}清晰度但未配置Cookie，可能失败")
            if qn >= 80 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}清晰度需要大会员账号")
            if qn >= 116 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}高帧率需要大会员账号")
            if qn >= 125 and not sessdata:
                validation_result["warnings"].append(f"请求{qn_name}需要大会员账号")

            BilibiliParser._logger.info(f"清晰度配置: {qn_name} (qn={qn})")

        # 检查其他配置
        fnval = int(opts.get("fnval", 4048))

        platform = str(opts.get("platform", "pc"))
        if platform not in ["pc", "html5"]:
            validation_result["warnings"].append(f"platform值{platform}不是标准值")

        # 记录验证结果
        if validation_result["warnings"]:
            BilibiliParser._logger.debug(f"Config warnings: {validation_result['warnings']}")
        if validation_result["errors"]:
            BilibiliParser._logger.error(f"Config errors: {validation_result['errors']}")
        if validation_result["recommendations"]:
            BilibiliParser._logger.debug(f"Config suggestions: {validation_result['recommendations']}")

        BilibiliParser._logger.debug(f"Config validation: {'pass' if validation_result['valid'] else 'fail'}")
        return validation_result

    @staticmethod
    def get_video_duration(video_path: str) -> Optional[float]:
        """获取视频时长（秒）"""
        try:
            import subprocess

            # 使用跨平台FFmpeg管理器获取ffprobe路径
            ffprobe_path = _ffmpeg_manager.get_ffprobe_path()

            if not ffprobe_path:
                BilibiliParser._logger.warning("未找到ffprobe，无法获取视频时长")
                return None

            # 使用ffprobe获取视频时长
            cmd = [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
            ]
            BilibiliParser._logger.debug(f"Running ffprobe: {' '.join(cmd)}")

            # 使用正确的编码设置来避免跨平台编码问题
            result = subprocess.run(cmd, capture_output=True, text=False)

            BilibiliParser._logger.debug(f"ffprobe return code: {result.returncode}")
            if result.stdout:
                stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
                BilibiliParser._logger.debug(f"ffprobe output: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
                BilibiliParser._logger.debug(f"ffprobe stderr: {stderr_text}")

            if result.returncode == 0:
                duration_str = result.stdout.decode("utf-8", errors="replace").strip()
                try:
                    duration = float(duration_str)
                    BilibiliParser._logger.debug(f"Video duration: {duration}s")
                    return duration
                except ValueError:
                    BilibiliParser._logger.warning(f"Failed to parse duration: '{duration_str}'")
                    return None
            else:
                BilibiliParser._logger.warning(f"ffprobe failed with code: {result.returncode}")
                return None
        except Exception as e:
            BilibiliParser._logger.error(f"Error getting video duration: {e}")
            return None


class VideoCompressor:
    """视频压缩处理类 - 支持自动硬件加速"""

    _logger = get_logger("plugin.bilibili_video_sender.compressor")

    def __init__(self, ffmpeg_path: Optional[str] = None, config: Optional[Dict] = None):
        self.ffmpeg_path = ffmpeg_path or _ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            self._logger.warning("未找到ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = "ffmpeg"

        # 读取配置
        self.config = config or {}
        enable_hardware = self.config.get("ffmpeg", {}).get("enable_hardware_acceleration", True)
        force_encoder = self.config.get("ffmpeg", {}).get("force_encoder", "")

        if not enable_hardware:
            # 禁用硬件加速
            self.recommended_encoder = "libx264"
            self._logger.debug("Hardware acceleration disabled, using software: libx264")
        elif force_encoder:
            # 强制使用指定编码器
            self.recommended_encoder = force_encoder
            self._logger.debug(f"Using forced encoder: {force_encoder}")
        else:
            # 自动检测硬件编码器
            self.hardware_info = _ffmpeg_manager.check_hardware_encoders()
            self.recommended_encoder = self._select_best_encoder()

            if self.recommended_encoder != "libx264":
                available_count = self.hardware_info.get("total_hardware_encoders", 0)
                self._logger.debug(f"Detected {available_count} hardware encoders, using: {self.recommended_encoder}")
            else:
                self._logger.debug("No hardware encoders available, using software: libx264")

    def _select_best_encoder(self) -> str:
        """根据配置的优先级选择最佳编码器"""
        available_encoders = self.hardware_info.get("available_encoders", [])
        if not available_encoders:
            return "libx264"

        # 获取优先级配置
        priority_list = self.config.get("ffmpeg", {}).get("encoder_priority", ["nvidia", "intel", "amd", "apple"])

        # 按优先级查找可用的编码器
        for encoder_type in priority_list:
            for encoder in available_encoders:
                if encoder["type"] == encoder_type and encoder["codec"] == "h264":
                    return encoder["name"]

        # 如果按优先级没找到，返回第一个可用的H.264编码器
        for encoder in available_encoders:
            if encoder["codec"] == "h264":
                return encoder["name"]

        # 最后回退到软件编码
        return "libx264"

    def compress_video(self, input_path: str, output_path: str, target_size_mb: int = 100, quality: int = 23) -> bool:
        """
        压缩视频到指定大小

        Args:
            input_path: 输入视频路径
            output_path: 输出视频路径
            target_size_mb: 目标文件大小（MB）
            quality: 压缩质量 (1-51，数值越小质量越高)

        Returns:
            是否压缩成功
        """
        try:
            import subprocess
            import os

            # 检查输入文件
            if not os.path.exists(input_path):
                self._logger.error(f"输入文件不存在: {input_path}")
                return False

            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            self._logger.info(
                "Starting video compression",
                input_path=input_path,
                input_size_mb=f"{input_size_mb:.2f}",
                target_size_mb=target_size_mb,
                encoder=self.recommended_encoder,
            )

            # 如果文件已经小于目标大小，直接复制
            if input_size_mb <= target_size_mb:
                import shutil

                shutil.copy2(input_path, output_path)
                self._logger.debug(
                    "File size already meets requirement, skipping compression", size_mb=f"{input_size_mb:.2f}"
                )
                return True

            # 构建FFmpeg压缩命令 - 使用自动检测的编码器
            cmd = self._build_compression_command(input_path, output_path, quality)

            self._logger.debug(f"Executing FFmpeg compression command: {' '.join(cmd)}")

            # 执行压缩
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=1800)  # 30分钟超时

            if result.returncode == 0:
                # 检查压缩后的文件大小
                if os.path.exists(output_path):
                    output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                    compression_ratio = (1 - output_size_mb / input_size_mb) * 100
                    self._logger.info(
                        "Video compression successful",
                        input_size_mb=f"{input_size_mb:.2f}",
                        output_size_mb=f"{output_size_mb:.2f}",
                        compression_ratio=f"{compression_ratio:.1f}%",
                        encoder=self.recommended_encoder,
                    )

                    # 如果压缩后仍然过大，尝试更高的压缩率
                    if output_size_mb > target_size_mb and quality < 35:
                        self._logger.debug(
                            "Output still oversized, increasing compression",
                            output_size_mb=f"{output_size_mb:.2f}",
                            target_size_mb=target_size_mb,
                            new_quality=quality + 5,
                        )
                        return self.compress_video(input_path, output_path, target_size_mb, quality + 5)

                    return True
                else:
                    self._logger.error("压缩后文件不存在")
                    return False
            else:
                self._logger.error(f"视频压缩失败，返回码: {result.returncode}")
                if result.stderr:
                    stderr_text = result.stderr.decode("utf-8", errors="replace")
                    self._logger.error(f"FFmpeg错误信息: {stderr_text}")
                return False

        except subprocess.TimeoutExpired:
            self._logger.error("视频压缩超时")
            return False
        except Exception as e:
            self._logger.error(f"视频压缩异常: {e}")
            return False

    def _build_compression_command(self, input_path: str, output_path: str, quality: int) -> List[str]:
        """构建基于硬件加速的压缩命令"""

        # 基础命令
        cmd = [self.ffmpeg_path, "-i", input_path]

        # 根据编码器类型添加不同的参数
        if self.recommended_encoder == "libx264":
            # 软件编码 H.264
            cmd.extend(["-c:v", "libx264", "-crf", str(quality), "-preset", "medium", "-c:a", "aac", "-b:a", "128k"])
            self._logger.debug("使用软件编码器 libx264")

        elif "nvenc" in self.recommended_encoder:
            # NVIDIA 硬件编码
            cmd.extend(
                [
                    "-c:v",
                    self.recommended_encoder,
                    "-cq",
                    str(quality),  # 对于 nvenc 使用 -cq 而不是 -crf
                    "-preset",
                    "p4",  # NVENC 预设：p1(fastest) 到 p7(slowest)，p4是平衡
                    "-profile:v",
                    "high",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                ]
            )
            self._logger.debug(f"使用 NVIDIA 硬件编码器 {self.recommended_encoder}")

        elif "qsv" in self.recommended_encoder:
            # Intel Quick Sync Video
            cmd.extend(
                [
                    "-c:v",
                    self.recommended_encoder,
                    "-global_quality",
                    str(quality),  # QSV 使用 global_quality
                    "-preset",
                    "medium",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                ]
            )
            self._logger.debug(f"使用 Intel QSV 硬件编码器 {self.recommended_encoder}")

        elif "amf" in self.recommended_encoder:
            # AMD 硬件编码
            cmd.extend(
                [
                    "-c:v",
                    self.recommended_encoder,
                    "-qp_i",
                    str(quality),  # AMD AMF 使用 qp_i
                    "-qp_p",
                    str(quality),
                    "-quality",
                    "balanced",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                ]
            )
            self._logger.debug(f"使用 AMD 硬件编码器 {self.recommended_encoder}")

        elif "videotoolbox" in self.recommended_encoder:
            # Apple VideoToolbox
            cmd.extend(
                [
                    "-c:v",
                    self.recommended_encoder,
                    "-q:v",
                    str(quality),  # VideoToolbox 使用 -q:v
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                ]
            )
            self._logger.debug(f"使用 Apple VideoToolbox 硬件编码器 {self.recommended_encoder}")

        else:
            # 未知编码器，回退到软件编码
            self._logger.warning(f"未知编码器 {self.recommended_encoder}，回退到软件编码")
            cmd.extend(["-c:v", "libx264", "-crf", str(quality), "-preset", "medium", "-c:a", "aac", "-b:a", "128k"])

        # 通用参数
        cmd.extend(
            [
                "-movflags",
                "+faststart",  # 优化流媒体播放
                "-y",  # 覆盖输出文件
                output_path,
            ]
        )

        return cmd


class VideoSplitter:
    """视频分块处理类"""

    _logger = get_logger("plugin.bilibili_video_sender.splitter")

    def __init__(self, ffmpeg_path: Optional[str] = None):
        self.ffmpeg_path = ffmpeg_path or _ffmpeg_manager.get_ffmpeg_path()
        if not self.ffmpeg_path:
            self._logger.warning("未找到ffmpeg，将使用系统默认路径")
            self.ffmpeg_path = "ffmpeg"

    def split_video_by_size(self, input_path: str, output_dir: str, max_size_mb: int = 100) -> List[str]:
        """
        根据文件大小智能分割视频，优化分片大小接近目标大小

        Args:
            input_path: 输入视频路径
            output_dir: 输出目录
            max_size_mb: 每个分片的目标大小（MB）

        Returns:
            分割后的视频文件路径列表
        """
        try:
            import subprocess
            import os

            # 获取视频信息
            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            self._logger.debug(
                "Starting optimized split",
                input_path=input_path,
                input_size_mb=f"{input_size_mb:.2f}",
                target_size_mb=max_size_mb,
            )

            # 获取视频时长
            duration = BilibiliParser.get_video_duration(input_path)
            if not duration:
                self._logger.error("无法获取视频时长，回退到固定时间分割", input_path=input_path)
                return self.split_video(input_path, output_dir)

            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)

            # 清理输出目录
            for file in os.listdir(output_dir):
                if file.startswith("part_") and file.endswith(".mp4"):
                    os.remove(os.path.join(output_dir, file))

            # 计算初始分段数 - 更精确的算法
            # 目标：让每个分片尽可能接近目标大小
            optimal_segments = max(2, int(input_size_mb / max_size_mb))

            # 如果文件大小接近目标大小的整数倍，使用精确分段
            if abs(input_size_mb - optimal_segments * max_size_mb) < max_size_mb * 0.1:
                optimal_segments = int(input_size_mb / max_size_mb)

            segment_duration = duration / optimal_segments

            self._logger.debug(
                "Split strategy calculated",
                duration_seconds=duration,
                segments=optimal_segments,
                segment_duration=f"{segment_duration:.1f}",
            )

            # 构建输出文件模式
            output_pattern = os.path.join(output_dir, f"part_%03d.mp4")

            # 构建FFmpeg命令
            cmd = [
                self.ffmpeg_path,
                "-i",
                input_path,
                "-c",
                "copy",  # 复制流，不重新编码
                "-f",
                "segment",
                "-segment_time",
                str(segment_duration),  # 使用计算出的分段时间
                "-reset_timestamps",
                "1",  # 重置时间戳
                "-segment_start_number",
                "0",  # 从0开始编号
                "-avoid_negative_ts",
                "make_zero",  # 避免负时间戳
                "-y",  # 覆盖现有文件
                output_pattern,
            ]

            self._logger.debug(f"Executing FFmpeg split command: {' '.join(cmd)}")

            # 执行分割 - 修复编码问题
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=1800)

            if result.returncode == 0:
                # 查找生成的分片文件并分析大小
                split_files = []
                total_size = 0
                i = 0

                while True:
                    part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                    if os.path.exists(part_path):
                        file_size = os.path.getsize(part_path)
                        if file_size > 0:
                            split_files.append(part_path)
                            file_size_mb = file_size / (1024 * 1024)
                            total_size += file_size_mb
                            self._logger.debug(f"Found split file: {part_path}, size: {file_size_mb:.2f}MB")
                        else:
                            self._logger.warning(f"Split file size is 0: {part_path}")
                        i += 1
                    else:
                        break

                if split_files:
                    # 分析分片效果
                    avg_size = total_size / len(split_files)
                    oversized_count = sum(1 for f in split_files if os.path.getsize(f) / (1024 * 1024) > max_size_mb)

                    self._logger.debug(f"Optimized split complete: {len(split_files)} parts")
                    self._logger.debug(f"Size stats: avg={avg_size:.2f}MB, oversized={oversized_count}")

                    # 显示每个分片的详细信息
                    for i, part_path in enumerate(split_files):
                        part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                        deviation = abs(part_size_mb - max_size_mb)
                        self._logger.debug(f"Part {i + 1}: {part_size_mb:.2f}MB (deviation: {deviation:.2f}MB)")

                    return split_files
                else:
                    self._logger.error("Optimized split complete but no files found")
                    return []
            else:
                self._logger.error(f"Optimized split failed with code: {result.returncode}")
                if result.stderr:
                    stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                    self._logger.error(f"FFmpeg error: {stderr_text}")
                return []

        except Exception as e:
            self._logger.error(f"Optimized split exception: {e}")
            return []

    def split_video_optimized(self, input_path: str, output_dir: str, target_size_mb: int = 100) -> List[str]:
        """
        使用迭代优化算法分割视频，使分片大小尽可能接近目标大小

        Args:
            input_path: 输入视频路径
            output_dir: 输出目录
            target_size_mb: 每个分片的目标大小（MB）

        Returns:
            分割后的视频文件路径列表
        """
        try:
            import subprocess
            import os

            # 获取视频信息
            input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            self._logger.debug(
                f"Starting iterative optimization split: {input_path} ({input_size_mb:.2f}MB) -> target: {target_size_mb}MB"
            )

            # 获取视频时长
            duration = BilibiliParser.get_video_duration(input_path)
            if not duration:
                self._logger.error("无法获取视频时长，回退到普通分割")
                return self.split_video_by_size(input_path, output_dir, target_size_mb)

            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)

            # 清理输出目录
            for file in os.listdir(output_dir):
                if file.startswith("part_") and file.endswith(".mp4"):
                    os.remove(os.path.join(output_dir, file))

            # 迭代优化算法
            best_result = None
            best_score = float("inf")

            # 尝试不同的分段数，找到最佳结果
            min_segments = max(2, int(input_size_mb / target_size_mb))
            max_segments = min(int(input_size_mb / (target_size_mb * 0.5)), int(duration / 30))  # 每段至少30秒

            self._logger.debug(f"Iteration range: {min_segments} - {max_segments} segments")

            for segments in range(min_segments, max_segments + 1):
                self._logger.debug(f"Trying {segments} segments")

                # 清理之前的尝试
                for file in os.listdir(output_dir):
                    if file.startswith("part_") and file.endswith(".mp4"):
                        os.remove(os.path.join(output_dir, file))

                segment_duration = duration / segments

                # 构建FFmpeg命令
                output_pattern = os.path.join(output_dir, f"part_%03d.mp4")
                cmd = [
                    self.ffmpeg_path,
                    "-i",
                    input_path,
                    "-c",
                    "copy",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(segment_duration),
                    "-reset_timestamps",
                    "1",
                    "-segment_start_number",
                    "0",
                    "-avoid_negative_ts",
                    "make_zero",
                    "-y",
                    output_pattern,
                ]

                # 执行分割 - 修复编码问题
                result = subprocess.run(cmd, capture_output=True, text=False, timeout=900)

                if result.returncode == 0:
                    # 分析分割结果
                    split_files = []
                    total_deviation = 0
                    oversized_count = 0

                    i = 0
                    while True:
                        part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                        if os.path.exists(part_path):
                            file_size = os.path.getsize(part_path)
                            if file_size > 0:
                                split_files.append(part_path)
                                file_size_mb = file_size / (1024 * 1024)
                                deviation = abs(file_size_mb - target_size_mb)
                                total_deviation += deviation
                                if file_size_mb > target_size_mb:
                                    oversized_count += 1
                                self._logger.debug(f"Found split file: part_{i:03d}.mp4, size: {file_size_mb:.2f}MB")
                            else:
                                self._logger.debug(f"Skipped empty file: part_{i:03d}.mp4")
                            i += 1
                        else:
                            self._logger.debug(f"Scan complete, no more files found: part_{i:03d}.mp4")
                            break

                    if split_files:
                        # 计算评分：平均偏差 + 超大分片惩罚
                        avg_deviation = total_deviation / len(split_files)
                        score = avg_deviation + (oversized_count * 10)  # 超大分片严重惩罚

                        self._logger.debug(
                            f"{segments} segments: avg_deviation={avg_deviation:.2f}MB, oversized={oversized_count}, score={score:.2f}"
                        )

                        if score < best_score:
                            best_score = score
                            # 深度复制文件列表，确保不会被后续清理影响
                            best_result = []
                            for split_file in split_files:
                                # 为最佳结果创建副本文件
                                best_file = split_file.replace(".mp4", f"_best.mp4")
                                import shutil

                                shutil.copy2(split_file, best_file)
                                best_result.append(best_file)
                            self._logger.debug(f"New best result: {segments} segments, score={score:.2f}")

            if best_result:
                # 显示最佳结果
                total_size = sum(os.path.getsize(f) for f in best_result) / (1024 * 1024)
                avg_size = total_size / len(best_result)
                oversized_count = sum(1 for f in best_result if os.path.getsize(f) / (1024 * 1024) > target_size_mb)

                self._logger.debug(f"Iterative optimization complete: {len(best_result)} parts")
                self._logger.debug(
                    f"Size stats: avg={avg_size:.2f}MB, oversized={oversized_count}, best_score={best_score:.2f}"
                )

                # 显示每个分片的详细信息
                for i, part_path in enumerate(best_result):
                    part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                    deviation = abs(part_size_mb - target_size_mb)
                    self._logger.debug(f"Part {i + 1}: {part_size_mb:.2f}MB (deviation: {deviation:.2f}MB)")

                # 清理临时文件（非最佳结果的文件）
                for file in os.listdir(output_dir):
                    if file.startswith("part_") and file.endswith(".mp4") and not file.endswith("_best.mp4"):
                        try:
                            os.remove(os.path.join(output_dir, file))
                        except Exception as e:
                            self._logger.debug(f"Cleaned temp file: {e}")

                # 重命名最佳结果文件为标准格式
                final_result = []
                for i, best_file in enumerate(best_result):
                    final_file = os.path.join(output_dir, f"part_{i:03d}.mp4")
                    try:
                        if os.path.exists(final_file):
                            os.remove(final_file)
                        os.rename(best_file, final_file)
                        final_result.append(final_file)
                    except Exception as e:
                        self._logger.warning(f"Failed to rename result file: {e}")
                        final_result.append(best_file)

                return final_result
            else:
                self._logger.warning("Iterative optimization failed, fallback to normal split")
                return self.split_video_by_size(input_path, output_dir, target_size_mb)

        except Exception as e:
            self._logger.error(f"Iterative optimization exception: {e}")
            return self.split_video_by_size(input_path, output_dir, target_size_mb)

    def split_video(self, input_path: str, output_dir: str) -> List[str]:
        """
        将视频分割成3分钟长度的片段（固定时间分割）

        Args:
            input_path: 输入视频路径
            output_dir: 输出目录

        Returns:
            分割后的视频文件路径列表
        """
        try:
            import subprocess
            import os

            self._logger.debug(
                f"Starting video split: input={input_path}, output directory={output_dir}, split interval=3 minutes"
            )
            self._logger.debug(f"FFmpeg path: {self.ffmpeg_path}")
            self._logger.debug(f"Input file exists: {os.path.exists(input_path)}")
            if os.path.exists(input_path):
                input_size_mb = os.path.getsize(input_path) / (1024 * 1024)
                self._logger.debug(f"Input file size: {input_size_mb:.2f}MB")

            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)

            # 获取输入文件名（不含扩展名），处理中文文件名
            base_name = os.path.splitext(os.path.basename(input_path))[0]

            # 为了避免Windows上的中文路径问题，使用英文标识符
            # 构建输出文件模式，使用英文标识符避免编码问题
            output_pattern = os.path.join(output_dir, f"part_%03d.mp4")

            # 每3分钟分割一次（180秒）

            # 构建FFmpeg命令 - 使用基于时间的分片（每3分钟）
            cmd = [
                self.ffmpeg_path,
                "-i",
                input_path,
                "-c",
                "copy",  # 复制流，不重新编码
                "-f",
                "segment",
                "-segment_time",
                "180",  # 每3分钟分割一次（180秒）
                "-reset_timestamps",
                "1",  # 重置时间戳
                "-segment_start_number",
                "0",  # 从0开始编号
                "-avoid_negative_ts",
                "make_zero",  # 避免负时间戳
                "-y",  # 覆盖输出文件
                output_pattern,
            ]

            # 执行分割命令
            self._logger.debug(f"Executing FFmpeg split command: {' '.join(cmd)}")

            # 使用正确的编码设置来避免Windows上的编码问题
            # 添加环境变量设置，确保FFmpeg能正常工作
            env = os.environ.copy()
            env["FFREPORT"] = "file=ffmpeg_debug.log:level=32"  # 启用FFmpeg调试日志

            result = subprocess.run(cmd, capture_output=True, text=False, env=env)

            self._logger.debug(f"FFmpeg split return code: {result.returncode}")
            if result.stdout:
                stdout_text = result.stdout.decode("utf-8", errors="replace").strip()
                # 成功时标准输出通常为进度/信息
                self._logger.debug(f"FFmpeg split stdout: {stdout_text}")
            if result.stderr:
                stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
                # 注意：FFmpeg 常把普通信息写入 stderr。仅在失败(returncode!=0)时按错误记录
                if result.returncode == 0:
                    self._logger.debug(f"FFmpeg split stderr: {stderr_text}")
                else:
                    self._logger.error(f"FFmpeg split error: {stderr_text}")

            if result.returncode != 0:
                stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                self._logger.error(f"视频分割失败: {stderr_text}")
                # 尝试使用备用分片方法
                self._logger.debug("尝试使用备用分片方法...")
                return self._fallback_split_video(input_path, output_dir)

            # 查找生成的分割文件，使用英文标识符
            split_files = []
            i = 0
            max_attempts = 100  # 防止无限循环

            # 等待一下，确保文件系统同步
            import time

            time.sleep(1)

            while i < max_attempts:
                part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                if os.path.exists(part_path):
                    file_size = os.path.getsize(part_path)
                    if file_size > 0:  # 确保文件不是空的
                        split_files.append(part_path)
                        self._logger.debug(f"Found split file: {part_path}, size: {file_size} bytes")
                        i += 1
                    else:
                        self._logger.warning(f"分块文件大小为0，跳过: {part_path}")
                        break
                else:
                    # 正常结束：下一个顺序分块不存在，停止扫描
                    self._logger.debug(f"无更多分块，停止扫描。下一个期望: {part_path}")
                    break

            if not split_files:
                self._logger.warning("未找到任何分片文件")

            self._logger.debug(f"视频分割完成，共生成{len(split_files)}个片段")

            return split_files

        except Exception as e:
            self._logger.error(f"视频分割过程中发生错误: {e}")
            return []

    def _fallback_split_video(self, input_path: str, output_dir: str) -> List[str]:
        """备用视频分片方法，使用更简单的FFmpeg命令"""
        try:
            self._logger.debug("使用备用分片方法...")

            # 确保输出目录存在
            os.makedirs(output_dir, exist_ok=True)

            # 使用更简单的分片命令（每3分钟）
            output_pattern = os.path.join(output_dir, "part_%03d.mp4")

            cmd = [
                self.ffmpeg_path,
                "-i",
                input_path,
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_time",
                "180",  # 每3分钟分割一次（180秒）
                "-reset_timestamps",
                "1",
                "-y",
                output_pattern,
            ]

            self._logger.debug(f"备用分片命令: {' '.join(cmd)}")

            # 执行命令
            result = subprocess.run(cmd, capture_output=True, text=False)

            if result.returncode != 0:
                stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                self._logger.error(f"备用分片也失败: {stderr_text}")
                return []

            # 查找生成的文件
            split_files = []
            i = 0
            while i < 100:  # 最多查找100个文件
                part_path = os.path.join(output_dir, f"part_{i:03d}.mp4")
                if os.path.exists(part_path) and os.path.getsize(part_path) > 0:
                    split_files.append(part_path)
                    self._logger.debug(f"备用方法找到分片: {part_path}")
                    i += 1
                else:
                    break

            return split_files

        except Exception as e:
            self._logger.error(f"备用分片方法也失败: {e}")
            return []


class BilibiliWbiSigner:
    """WBI 签名工具：自动获取 wbi key 并缓存，生成 w_rid/wts"""

    _logger = get_logger("plugin.bilibili_video_sender.wbi_signer")

    _mixin_key_indices: List[int] = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        40,
        17,
        16,
        7,
        24,
        55,
        54,
        4,
        52,
        30,
        26,
        22,
        44,
        0,
        1,
        34,
        25,
        6,
        51,
        11,
        36,
        20,
        21,
    ]

    _cached_mixin_key: Optional[str] = None
    _cached_at: float = 0.0
    _cache_ttl_seconds: int = 3600

    @classmethod
    def _fetch_wbi_keys(cls) -> Tuple[str, str]:
        """从 nav 接口拉取 wbi img/sub key"""
        url = "https://api.bilibili.com/x/web-interface/nav"
        data = BilibiliParser._fetch_json(url)
        wbi_img = (((data or {}).get("data") or {}).get("wbi_img")) or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")

        def _extract_key(u: str) -> str:
            filename = u.rsplit("/", 1)[-1]
            return filename.split(".")[0]

        img_key = _extract_key(img_url)
        sub_key = _extract_key(sub_url)
        return img_key, sub_key

    @classmethod
    def _gen_mixin_key(cls) -> str:
        now = time.time()
        if cls._cached_mixin_key and (now - cls._cached_at) < cls._cache_ttl_seconds:
            return cls._cached_mixin_key
        img_key, sub_key = cls._fetch_wbi_keys()
        raw = img_key + sub_key
        mixed = "".join(raw[i] for i in cls._mixin_key_indices)[:32]
        cls._cached_mixin_key = mixed
        cls._cached_at = now
        return mixed

    @classmethod
    def sign_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """生成 wts 和 w_rid 并返回带签名的参数副本"""
        mixin_key = cls._gen_mixin_key()
        # 复制并清洗参数
        safe_params: Dict[str, Any] = {}
        for k, v in params.items():
            if isinstance(v, str):
                v2 = re.sub(r"[!'()*]", "", v)
            else:
                v2 = v
            safe_params[k] = v2
        # 加入 wts
        wts = int(time.time())
        safe_params["wts"] = wts
        # 排序并 urlencode
        items = sorted(safe_params.items(), key=lambda x: x[0])
        query = urllib.parse.urlencode(items, doseq=True)
        w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        safe_params["w_rid"] = w_rid
        return safe_params


class BilibiliAutoSendHandler(BaseEventHandler):
    """收到包含哔哩哔哩视频链接的消息后，自动解析并发送视频。"""

    _logger = get_logger("plugin.bilibili_video_sender.handler")

    event_type = EventType.ON_MESSAGE
    handler_name = "bilibili_auto_send_handler"
    handler_description = "解析B站视频链接并发送视频"

    def _should_return_5_tuple(self) -> bool:
        """判断是否应该返回5元组（基于events_manager版本）

        Returns:
            bool: True表示返回5元组，False表示返回3元组
        """
        # 默认为 False（旧版本），向后兼容
        return self.get_config("plugin.use_new_events_manager", False)

    def _make_return_value(self, success: bool, continue_processing: bool, result: str | None) -> Tuple:
        """根据版本配置生成返回值

        Args:
            success: 执行是否成功
            continue_processing: 是否继续处理后续事件
            result: 执行结果描述

        Returns:
            Tuple: 根据配置返回3元组或5元组
        """
        if self._should_return_5_tuple():
            # 新版本：返回5元组 (success, continue_processing, result, modified_message, metadata)
            return success, continue_processing, result, None, None
        else:
            # 旧版本：返回3元组 (success, continue_processing, result)
            return success, continue_processing, result

    def _is_private_message(self, message: NachoMessages) -> bool:
        """检测消息是否为私聊消息"""

        # 方法1：从message_base_info中获取group_id，如果没有group_id则为私聊
        if message.message_base_info:
            group_id = message.message_base_info.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                self._logger.debug("检测到私聊消息（无group_id）")
                return True
            else:
                self._logger.debug(f"检测到群聊消息（group_id: {group_id}）")
                return False

        # 方法2：从additional_data中获取
        if message.additional_data:
            group_id = message.additional_data.get("group_id")
            if group_id is None or group_id == "" or group_id == "0":
                self._logger.debug("检测到私聊消息（additional_data无group_id）")
                return True
            else:
                self._logger.debug(f"检测到群聊消息（additional_data group_id: {group_id}）")
                return False

        # 默认当作群聊处理
        self._logger.debug("无法确定消息类型，默认当作群聊处理")
        return False

    def _get_user_id(self, message: NachoMessages) -> str | None:
        """从消息中获取用户ID"""
        # 方法1：从message_base_info中获取
        if message.message_base_info:
            user_id = message.message_base_info.get("user_id")
            if user_id:
                return str(user_id)

        # 方法2：从additional_data中获取
        if message.additional_data:
            user_id = message.additional_data.get("user_id")
            if user_id:
                return str(user_id)

        return None

    def _get_group_id(self, message: NachoMessages) -> str | None:
        """从消息中获取群ID"""
        # 方法1：从message_base_info中获取
        if message.message_base_info:
            group_id = message.message_base_info.get("group_id")
            if group_id and group_id != "" and group_id != "0":
                return str(group_id)

        # 方法2：从additional_data中获取
        if message.additional_data:
            group_id = message.additional_data.get("group_id")
            if group_id and group_id != "" and group_id != "0":
                return str(group_id)

        return None

    def _get_stream_id(self, message: NachoMessages) -> str | None:
        """从消息中获取stream_id"""

        # 方法1：直接从message对象的stream_id属性获取
        if message.stream_id:
            return message.stream_id

        # 方法2：从chat_stream属性获取
        if hasattr(message, "chat_stream") and message.chat_stream:
            stream_id = getattr(message.chat_stream, "stream_id", None)
            if stream_id:
                return stream_id

        # 方法3：从message_base_info中获取
        if message.message_base_info:
            # 尝试从message_base_info中提取必要信息生成stream_id
            try:
                from src.chat.message_receive.chat_stream import get_chat_manager

                platform = message.message_base_info.get("platform")
                user_id = message.message_base_info.get("user_id")
                group_id = message.message_base_info.get("group_id")

                if platform and (user_id or group_id):
                    chat_manager = get_chat_manager()
                    if group_id:
                        stream_id = chat_manager.get_stream_id(platform, group_id, True)
                    else:
                        stream_id = chat_manager.get_stream_id(platform, user_id, False)

                    if stream_id:
                        return stream_id
            except Exception as e:
                self._logger.error(f"方法3失败：{e}")

        # 方法4：从additional_data中查找
        if message.additional_data:
            stream_id = message.additional_data.get("stream_id")
            if stream_id:
                return stream_id

        # 如果所有方法都失败，返回None
        self._logger.error("无法获取stream_id")
        return None

    async def _send_text(self, content: str, stream_id: str) -> bool:
        """发送文本消息"""
        try:
            return await send_api.text_to_stream(content, stream_id)
        except Exception as e:
            # 记录错误但不抛出异常，避免影响其他处理器
            return False

    async def _send_private_video(self, original_path: str, converted_path: str, user_id: str) -> bool:
        """通过API发送私聊视频

        Args:
            original_path: 原始文件路径（用于文件检查）
            converted_path: 转换后的路径（用于发送URI）
            user_id: 目标用户ID
        """

        try:
            # 获取配置的端口
            port = self.get_config("api.port", 5700)
            api_url = f"http://localhost:{port}/send_private_msg"

            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                self._logger.error(f"视频文件不存在: {original_path}")
                return False

            # 构造本地文件路径，使用file://协议（使用转换后路径）
            file_uri = f"file://{converted_path}"

            self._logger.debug(f"Private video send - original path: {original_path}")
            self._logger.debug(f"Private video send - converted path: {converted_path}")
            self._logger.debug(f"Private video send - send URI: {file_uri}")

            # 构造请求数据
            request_data = {"user_id": user_id, "message": [{"type": "video", "data": {"file": file_uri}}]}

            self._logger.debug(f"Sending private video API request: {api_url}")
            self._logger.debug(f"Request data: {request_data}")

            # 发送API请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=request_data, timeout=300) as response:
                    if response.status == 200:
                        result = await response.json()
                        self._logger.debug(f"Private video sent successfully: {result}")
                        return True
                    else:
                        error_text = await response.text()
                        self._logger.error(f"Failed to send private video: HTTP {response.status}, {error_text}")
                        return False

        except asyncio.TimeoutError:
            self._logger.error("Private video sending timeout")
            return False
        except Exception as e:
            self._logger.error(f"Private video sending error: {e}")
            return False

    async def _send_group_video(self, original_path: str, converted_path: str, group_id: str) -> bool:
        """通过API发送群视频

        Args:
            original_path: 原始文件路径（用于文件检查）
            converted_path: 转换后的路径（用于发送URI）
            group_id: 目标群ID
        """

        try:
            # 获取配置的端口
            port = self.get_config("api.port", 5700)
            api_url = f"http://localhost:{port}/send_group_msg"

            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                self._logger.error(f"视频文件不存在: {original_path}")
                return False

            # 构造本地文件路径，使用file://协议（使用转换后路径）
            file_uri = f"file://{converted_path}"

            self._logger.debug(f"Group video send - original path: {original_path}")
            self._logger.debug(f"Group video send - converted path: {converted_path}")
            self._logger.debug(f"Group video send - send URI: {file_uri}")

            # 构造请求数据
            request_data = {"group_id": group_id, "message": [{"type": "video", "data": {"file": file_uri}}]}

            self._logger.debug(f"Sending group video API request: {api_url}")
            self._logger.debug(f"Request data: {request_data}")

            # 发送API请求
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=request_data, timeout=300) as response:
                    if response.status == 200:
                        result = await response.json()
                        self._logger.debug(f"Group video sent successfully: {result}")
                        return True
                    else:
                        error_text = await response.text()
                        self._logger.error(f"Failed to send group video: HTTP {response.status}, {error_text}")
                        return False

        except asyncio.TimeoutError:
            self._logger.error("Group video sending timeout")
            return False
        except Exception as e:
            self._logger.error(f"Group video sending error: {e}")
            return False

    async def execute(self, message: NachoMessages) -> Tuple[bool, bool, str | None]:
        if not self.get_config("plugin.enabled", True):
            self._logger.debug("插件已禁用，退出处理")
            return self._make_return_value(True, True, None)

        raw: str = getattr(message, "raw_message", "") or ""

        url = BilibiliParser.find_first_bilibili_url(raw)
        if not url:
            return self._make_return_value(True, True, None)

        self._logger.info("Bilibili video link detected", url=url)

        # 获取stream_id用于发送消息
        stream_id = self._get_stream_id(message)
        if not stream_id:
            self._logger.error("无法获取聊天流ID，尝试备选方案")

            # 备选方案：尝试从message_base_info提取用户信息，直接向用户发送消息
            try:
                from src.chat.message_receive.chat_stream import get_chat_manager

                # 尝试提取平台和用户ID
                platform = None
                user_id = None

                # 从message_base_info中提取
                if message.message_base_info:
                    platform = message.message_base_info.get("platform")
                    user_id = message.message_base_info.get("user_id")

                # 从additional_data中提取
                if not platform and not user_id and message.additional_data:
                    platform = message.additional_data.get("platform")
                    user_id = message.additional_data.get("user_id")

                if platform and user_id:
                    # 创建一个临时的stream_id
                    chat_manager = get_chat_manager()
                    stream_id = chat_manager.get_stream_id(platform, user_id, False)
                else:
                    self._logger.error("备选方案失败：无法获取平台和用户ID")
                    return self._make_return_value(True, True, "无法获取聊天流ID")
            except Exception as e:
                self._logger.error(f"备选方案失败：{e}")
                return self._make_return_value(True, True, "无法获取聊天流ID")

        # 检查FFmpeg可用性
        ffmpeg_info = _ffmpeg_manager.check_ffmpeg_availability()
        show_ffmpeg_warnings = self.get_config("ffmpeg.show_warnings", True)

        if not ffmpeg_info["ffmpeg_available"]:
            if show_ffmpeg_warnings:
                self._logger.debug("FFmpeg unavailable, merge and split functions disabled")
            else:
                self._logger.debug("FFmpeg unavailable, merge and split functions disabled")
        if not ffmpeg_info["ffprobe_available"]:
            if show_ffmpeg_warnings:
                self._logger.debug("ffprobe unavailable, duration detection disabled")
            else:
                self._logger.debug("ffprobe unavailable, duration detection disabled")

        # 读取并记录配置
        config_opts = {
            "use_wbi": self.get_config("bilibili.use_wbi", True),
            "prefer_dash": self.get_config("bilibili.prefer_dash", True),
            "fnval": self.get_config("bilibili.fnval", 4048),
            "fourk": self.get_config("bilibili.fourk", True),
            "qn": self.get_config("bilibili.qn", 0),
            "platform": self.get_config("bilibili.platform", "pc"),
            "high_quality": self.get_config("bilibili.high_quality", False),
            "try_look": self.get_config("bilibili.try_look", False),
            "sessdata": self.get_config("bilibili.sessdata", ""),
            "buvid3": self.get_config("bilibili.buvid3", ""),
            "enable_video_splitting": self.get_config("bilibili.enable_video_splitting", True),
            "delete_original_after_split": self.get_config("bilibili.delete_original_after_split", True),
        }

        # 检查鉴权配置
        if not config_opts["sessdata"]:
            self._logger.debug("No SESSDATA configured, using guest mode")
            if config_opts["qn"] >= 64:
                self._logger.warning(f"Requested quality {config_opts['qn']} but not logged in, may fail")
        if not config_opts["buvid3"]:
            self._logger.debug("No Buvid3 configured, session generation may fail")

        # 执行配置验证
        validation_result = BilibiliParser.validate_config(config_opts)
        if not validation_result["valid"]:
            self._logger.error("配置验证失败，但继续尝试处理")
        if validation_result["warnings"]:
            for warning in validation_result["warnings"]:
                self._logger.debug(f"配置警告: {warning}")
        if validation_result["recommendations"]:
            for rec in validation_result["recommendations"]:
                self._logger.debug(f"配置建议: {rec}")

        loop = asyncio.get_running_loop()

        def _blocking() -> Optional[Tuple[BilibiliVideoInfo, List[str], str]]:
            info = BilibiliParser.get_view_info_by_url(url)
            if not info:
                self._logger.error("Failed to parse video info", url=url)
                return None

            self._logger.debug("Video info parsed", title=info.title, aid=info.aid, cid=info.cid)

            if info.duration > 600:
                self._logger.info(
                    f"Video duration ({info.duration}s) exceeds 10 minutes, discarding before fetching URLs."
                )
                return info, [], "视频时长超过10分钟"

            urls, status = BilibiliParser.get_play_urls(info.aid, info.cid, config_opts)
            self._logger.debug("Playback URLs fetched", status=status, url_count=len(urls), title=info.title)

            return info, urls, status

        try:
            result = await loop.run_in_executor(None, _blocking)
        except Exception as exc:  # noqa: BLE001 - 简要兜底
            error_msg = f"解析失败：{exc}"
            self._logger.error(error_msg)
            return self._make_return_value(True, True, "解析失败")

        if not result:
            error_msg = "未能解析该视频链接，请稍后重试。"
            self._logger.error(error_msg)
            return self._make_return_value(True, True, "解析失败")

        info, urls, status = result
        if not urls:
            error_msg = f"解析失败：{status}"
            self._logger.error(error_msg)
            if status == "视频时长超过10分钟":
                await self._send_text("视频太长，猫猫搬不动啦        (；´-ω-`)", stream_id)
                return self._make_return_value(True, True, "视频过长，已丢弃")
            return self._make_return_value(True, True, "解析失败")

        self._logger.info(f"Parse successful: {info.title}")

        # 发送解析成功消息
        await self._send_text("找到视频了喵！正在搬来..", stream_id)

        # 下载前清理/准备分块目录 (仅执行清理逻辑，返回值忽略)
        # _prepare_split_dir()  <-- 不再需要，因为我们在分块时会创建新目录，且包含了清理逻辑

        # 同时发送视频文件
        self._logger.debug("Starting video download...")

        def _download_to_temp(urls: List[str]) -> Optional[str]:
            try:
                safe_title = re.sub(r"[\\/:*?\"<>|]+", "_", info.title).strip() or "bilibili_video"
                tmp_dir = tempfile.gettempdir()
                temp_path = os.path.join(tmp_dir, f"{safe_title}.mp4")

                self._logger.debug("Preparing download", title=info.title, temp_path=temp_path)

                # 添加特定的请求头来解决403问题
                # 请求头（含可选 Cookie）
                headers = {
                    "User-Agent": BilibiliParser.USER_AGENT,
                    "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com",
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Range": "bytes=0-",  # 支持断点续传
                }
                sessdata_hdr = self.get_config("bilibili.sessdata", "").strip()
                buvid3_hdr = self.get_config("bilibili.buvid3", "").strip()
                if sessdata_hdr:
                    cookie_parts = [f"SESSDATA={sessdata_hdr}"]
                    if buvid3_hdr:
                        cookie_parts.append(f"buvid3={buvid3_hdr}")
                    headers["Cookie"] = "; ".join(cookie_parts)
                    headers["gaia_source"] = sessdata_hdr  # 添加 gaia_source
                    self._logger.debug("Cookie auth added for download")
                else:
                    self._logger.debug("No Cookie for download, may get 403 error")

                # 判断是否是分离的视频和音频流
                # 注意：这里使用外层的urls变量，需要确保在正确的作用域中调用
                if len(urls) >= 2 and (".m4s" in urls[0].lower() or ".m4s" in urls[1].lower()):
                    self._logger.debug("DASH format detected", stream_count=len(urls), format="m4s")

                    # 下载视频流
                    video_temp = os.path.join(tmp_dir, f"{safe_title}_video.m4s")

                    req = BilibiliParser._build_request(urls[0], headers)
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        # 获取文件总大小（如果可用）
                        total_size = resp.headers.get("content-length")
                        total_size = int(total_size) if total_size else 0

                        # 创建进度条
                        progress_bar = ProgressBar(total_size, "Video stream downloading", 30)

                        with open(video_temp, "wb") as f:
                            downloaded = 0
                            while True:
                                chunk = resp.read(1024 * 256)
                                if not chunk:
                                    break
                                f.write(chunk)
                                downloaded += len(chunk)
                                # 使用进度条显示进度
                                progress_bar.update(downloaded)

                        # 完成进度条显示
                        progress_bar.finish()
                        video_size_mb = os.path.getsize(video_temp) / (1024 * 1024)
                        self._logger.debug("Video stream downloaded", size_mb=f"{video_size_mb:.2f}")

                    # 下载音频流
                    audio_temp = os.path.join(tmp_dir, f"{safe_title}_audio.m4s")

                    # 如果有音频URL，下载音频流
                    if len(urls) >= 2:
                        req = BilibiliParser._build_request(urls[1], headers)
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            # 获取文件总大小（如果可用）
                            total_size = resp.headers.get("content-length")
                            total_size = int(total_size) if total_size else 0

                            # 创建进度条
                            progress_bar = ProgressBar(total_size, "Audio stream downloading", 30)

                            with open(audio_temp, "wb") as f:
                                downloaded = 0
                                while True:
                                    chunk = resp.read(1024 * 256)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    downloaded += len(chunk)
                                    # 使用进度条显示进度
                                    progress_bar.update(downloaded)

                            # 完成进度条显示
                            progress_bar.finish()
                            self._logger.debug(
                                f"Audio stream downloaded, size: {os.path.getsize(audio_temp) // (1024 * 1024)}MB"
                            )
                    else:
                        self._logger.debug("No audio stream URL available")
                        audio_temp = None

                    # 尝试使用FFmpeg合并
                    try:
                        import subprocess
                        import shutil

                        # 使用跨平台FFmpeg管理器获取ffmpeg路径
                        ffmpeg_path = _ffmpeg_manager.get_ffmpeg_path()
                        if ffmpeg_path:
                            self._logger.debug(f"Using FFmpeg: {ffmpeg_path}")
                            # 首先检查视频文件格式
                            self._logger.debug("Checking file format...")

                            # 检查视频文件 - 使用跨平台ffprobe
                            ffprobe_path = _ffmpeg_manager.get_ffprobe_path()
                            if ffprobe_path:
                                probe_cmd = [
                                    ffprobe_path,
                                    "-v",
                                    "error",
                                    "-show_entries",
                                    "format=format_name",
                                    "-of",
                                    "default=noprint_wrappers=1:nokey=1",
                                    video_temp,
                                ]
                                try:
                                    video_format = (
                                        subprocess.run(probe_cmd, capture_output=True, text=False)
                                        .stdout.decode("utf-8", errors="replace")
                                        .strip()
                                    )
                                except Exception as e:
                                    self._logger.warning(f"Unable to check video format: {str(e)}")
                                    video_format = "unknown"

                                # 如果有音频文件，检查其格式
                                audio_format = "none"
                                if audio_temp and os.path.exists(audio_temp):
                                    probe_cmd = [
                                        ffprobe_path,
                                        "-v",
                                        "error",
                                        "-show_entries",
                                        "format=format_name",
                                        "-of",
                                        "default=noprint_wrappers=1:nokey=1",
                                        audio_temp,
                                    ]
                                    try:
                                        audio_format = (
                                            subprocess.run(probe_cmd, capture_output=True, text=False)
                                            .stdout.decode("utf-8", errors="replace")
                                            .strip()
                                        )
                                    except Exception as e:
                                        self._logger.warning(f"Unable to check audio format: {str(e)}")
                            else:
                                self._logger.warning(f"ffprobe not found, unable to check file format: {ffprobe_path}")
                                video_format = "unknown"
                                audio_format = "none"

                            # 根据文件格式决定处理方式
                            if "m4s" in video_format.lower() or video_temp.lower().endswith(".m4s"):
                                # 对于m4s格式，需要添加特殊参数
                                if audio_temp and os.path.exists(audio_temp):
                                    ffmpeg_cmd = [
                                        ffmpeg_path,
                                        "-i",
                                        video_temp,
                                        "-i",
                                        audio_temp,
                                        "-c:v",
                                        "copy",  # 复制视频流，不重新编码
                                        "-c:a",
                                        "aac",  # 将音频转换为aac格式以确保兼容性
                                        "-strict",
                                        "experimental",
                                        "-b:a",
                                        "192k",  # 设置音频比特率
                                        "-y",
                                        temp_path,
                                    ]
                                else:
                                    # 如果没有音频文件，只处理视频
                                    ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", temp_path]
                            else:
                                # 标准处理方式
                                if audio_temp and os.path.exists(audio_temp):
                                    ffmpeg_cmd = [
                                        ffmpeg_path,
                                        "-i",
                                        video_temp,
                                        "-i",
                                        audio_temp,
                                        "-c:v",
                                        "copy",
                                        "-c:a",
                                        "copy",
                                        "-y",
                                        temp_path,
                                    ]
                                else:
                                    # 如果没有音频文件，只处理视频
                                    ffmpeg_cmd = [ffmpeg_path, "-i", video_temp, "-c:v", "copy", "-y", temp_path]

                            self._logger.debug("Starting to merge video and audio...")

                            # 使用正确的编码设置来避免Windows上的编码问题
                            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=False)

                            if result.returncode == 0:
                                self._logger.debug("Video and audio merged successfully")
                                # 删除临时文件
                                try:
                                    if os.path.exists(video_temp):
                                        os.remove(video_temp)
                                    if audio_temp and os.path.exists(audio_temp):
                                        os.remove(audio_temp)
                                    self._logger.debug("Temporary files cleaned")
                                except Exception as e:
                                    self._logger.warning(f"Failed to clean temp: {str(e)}")

                                return temp_path
                            else:
                                stderr_text = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                                self._logger.warning(f"FFmpeg merge failed: {stderr_text}")
                        else:
                            self._logger.warning("FFmpeg not found, cannot merge video and audio")
                            self._logger.debug("Using video stream only")
                    except Exception as e:
                        self._logger.warning(f"Merge failed: {str(e)}")

                    # 如果所有方法都失败，返回视频流文件
                    self._logger.debug("Using video stream only")
                    return video_temp

                # 非分离流：仅支持DASH，跳过单文件下载
                self._logger.debug("Only DASH streams supported, skipping single file download")
                return None
            except Exception as e:
                self._logger.error(f"Failed to download video: {e}")
                return None

        temp_path = await asyncio.get_running_loop().run_in_executor(None, lambda: _download_to_temp(urls))
        if not temp_path:
            self._logger.warning("Video download failed")
            return self._make_return_value(True, True, "视频下载失败")

        self._logger.debug(f"Video download completed: {temp_path}")
        caption = f"{info.title}"

        # 检查视频时长，决定是否需要分块
        video_duration = BilibiliParser.get_video_duration(temp_path)
        self._logger.debug(f"Detected video duration: {video_duration} seconds")

        # 检查视频文件大小和时长，决定处理策略
        video_size_mb = os.path.getsize(temp_path) / (1024 * 1024)
        self._logger.debug(f"Detected video size: {video_size_mb:.2f}MB")

        # 从配置读取相关设置
        enable_splitting = self.get_config("bilibili.enable_video_splitting", True)
        delete_original = self.get_config("bilibili.delete_original_after_split", True)
        max_video_size_mb = self.get_config("bilibili.max_video_size_mb", 100)
        enable_compression = self.get_config("bilibili.enable_video_compression", True)
        compression_quality = self.get_config("bilibili.compression_quality", 23)

        self._logger.debug(
            f"Video processing configuration: splitting={enable_splitting}, compression={enable_compression}, max size={max_video_size_mb}MB, compression quality={compression_quality}"
        )

        # 新的处理策略：仅基于文件大小分块
        # 分块条件：启用分块 AND FFmpeg可用 AND 文件过大
        should_split = bool(enable_splitting) and ffmpeg_info["ffmpeg_available"] and video_size_mb > max_video_size_mb

        if enable_splitting and not ffmpeg_info["ffmpeg_available"]:
            self._logger.warning("Splitting enabled but FFmpeg unavailable, skipping split processing")

        self._logger.debug(
            f"Processing strategy: should_split={should_split} (file size={video_size_mb:.2f}MB, duration={video_duration} seconds)"
        )

        if should_split:
            self._logger.debug("Using pre-split and compression strategy")

            # 创建分块器 - 使用跨平台FFmpeg管理器
            ffmpeg_info = _ffmpeg_manager.check_ffmpeg_availability()
            self._logger.debug(f"FFmpeg availability check: {ffmpeg_info}")

            if not ffmpeg_info["ffmpeg_available"]:
                self._logger.error("FFmpeg not found in system, unable to split video")
                should_split = False
            else:
                # 使用FFmpeg管理器获取的路径
                splitter = VideoSplitter(ffmpeg_info["ffmpeg_path"])
                self._logger.debug(f"Using FFmpeg path: {ffmpeg_info['ffmpeg_path']}")

            # 准备插件内的持久分块目录：插件目录/data/split
            split_dir = _prepare_split_dir()

            # 第一步：根据配置选择分割算法
            use_optimized = self.get_config("bilibili.use_optimized_splitting", True)
            if use_optimized:
                self._logger.debug("Using iterative optimization split algorithm, obtaining the most accurate split")
                split_files = splitter.split_video_optimized(temp_path, split_dir, max_video_size_mb)
            else:
                self._logger.debug("Using normal intelligent split algorithm")
                split_files = splitter.split_video_by_size(temp_path, split_dir, max_video_size_mb)

            if split_files:
                self._logger.debug(f"Video split completed, {len(split_files)} segments")

                # 第二步：检查并压缩超大的分片
                if enable_compression and ffmpeg_info["ffmpeg_available"]:
                    self._logger.debug("Starting to check and compress large files...")
                    # 构建配置字典传递给压缩器
                    config_dict = {
                        "ffmpeg": {
                            "enable_hardware_acceleration": self.get_config(
                                "ffmpeg.enable_hardware_acceleration", True
                            ),
                            "force_encoder": self.get_config("ffmpeg.force_encoder", ""),
                            "encoder_priority": self.get_config(
                                "ffmpeg.encoder_priority", ["nvidia", "intel", "amd", "apple"]
                            ),
                        }
                    }
                    compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"], config_dict)

                    final_split_files = []
                    compression_stats = {"compressed": 0, "skipped": 0, "failed": 0}

                    for i, part_path in enumerate(split_files):
                        part_size_mb = os.path.getsize(part_path) / (1024 * 1024)
                        self._logger.debug(f"Checking part {i + 1}: {part_size_mb:.2f}MB")

                        if part_size_mb > max_video_size_mb:
                            # 需要压缩的分片
                            self._logger.debug(
                                f"Part {i + 1} size ({part_size_mb:.2f}MB) exceeds limit, compressing..."
                            )
                            compressed_part_path = part_path.replace(".mp4", "_compressed.mp4")

                            if compressor.compress_video(
                                part_path, compressed_part_path, max_video_size_mb, compression_quality
                            ):
                                compressed_size_mb = os.path.getsize(compressed_part_path) / (1024 * 1024)
                                self._logger.debug(
                                    f"Part {i + 1} compression successful: {part_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB"
                                )
                                final_split_files.append(compressed_part_path)
                                compression_stats["compressed"] += 1

                                # 删除原始分片（如果配置允许）
                                if delete_original:
                                    try:
                                        os.remove(part_path)
                                        self._logger.debug(f"Original part {part_path} deleted")
                                    except Exception as e:
                                        self._logger.warning(f"Failed to delete original part: {e}")
                            else:
                                self._logger.warning(f"Part {i + 1} compression failed, using original file")
                                final_split_files.append(part_path)
                                compression_stats["failed"] += 1
                        else:
                            # 大小符合要求的分片
                            self._logger.debug(f"Part {i + 1} size meets requirements, no compression needed")
                            final_split_files.append(part_path)
                            compression_stats["skipped"] += 1

                    self._logger.debug(
                        f"Compression complete: {compression_stats['compressed']} compressed, {compression_stats['skipped']} skipped, {compression_stats['failed']} failed"
                    )
                    split_files = final_split_files
                else:
                    self._logger.debug("Compression disabled or FFmpeg unavailable, skipping compression")

                # 重新扫描split目录，按顺序发送所有分片
                self._logger.debug("Starting to scan split directory, sending all parts in order")

                # 调试：显示split目录的实际内容
                try:
                    actual_files = os.listdir(split_dir)
                    self._logger.debug(f"Actual file list in split directory: {actual_files}")
                    for file in actual_files:
                        file_path = os.path.join(split_dir, file)
                        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                        self._logger.debug(f"  - {file}: {file_size_mb:.2f}MB")
                except Exception as e:
                    self._logger.warning(f"Unable to read split directory content: {e}")

                sent_count = 0
                failed_files = []

                # 扫描split目录中的所有part_xxx.mp4文件（包括压缩后的文件）
                all_split_files = []
                i = 0
                while True:
                    # 优先查找压缩后的文件
                    compressed_filename = f"part_{i:03d}_compressed.mp4"
                    compressed_path = os.path.join(split_dir, compressed_filename)

                    # 然后查找原始文件
                    original_filename = f"part_{i:03d}.mp4"
                    original_path = os.path.join(split_dir, original_filename)

                    found_file = None
                    found_filename = None

                    # 优先使用压缩后的文件
                    if os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 0:
                        found_file = compressed_path
                        found_filename = compressed_filename
                        self._logger.debug(f"Found compressed part file: {compressed_filename}")
                    elif os.path.exists(original_path) and os.path.getsize(original_path) > 0:
                        found_file = original_path
                        found_filename = original_filename
                        self._logger.debug(f"Found original part file: {original_filename}")

                    if found_file:
                        all_split_files.append(found_file)
                        part_size_mb = os.path.getsize(found_file) / (1024 * 1024)
                        self._logger.debug(f"Adding part file: {found_filename}, size: {part_size_mb:.2f}MB")
                        i += 1
                    else:
                        self._logger.debug(
                            f"Scan complete, no more files found: {original_filename} or {compressed_filename}"
                        )
                        break

                self._logger.debug(f"Found {len(all_split_files)} part files, starting to send")

                # 为所有分片进行WSL路径转换
                enable_conversion = self.get_config("wsl.enable_path_conversion", True)
                if enable_conversion:
                    self._logger.debug("Starting to convert paths for all parts to WSL")
                    converted_all_split_files = []
                    for part_path in all_split_files:
                        converted_path = convert_windows_to_wsl_path(part_path)
                        converted_all_split_files.append(converted_path)
                        self._logger.debug(f"Path conversion: {part_path} -> {converted_path}")
                else:
                    self._logger.debug("WSL path conversion disabled, using original paths")
                    converted_all_split_files = all_split_files

                # 按顺序发送所有分片
                for i, (original_path, converted_path) in enumerate(zip(all_split_files, converted_all_split_files)):
                    part_caption = f"{caption} - Part {i + 1}"

                    if await self._send_video_part(original_path, converted_path, part_caption, stream_id, message):
                        sent_count += 1
                        self._logger.debug(f"Part {i + 1} sent successfully")
                    else:
                        self._logger.debug(f"Part {i + 1} failed to send")
                        failed_files.append(original_path)
                # 不删除分块文件与目录，保留给外部发送软件使用；将于下一次下载前清理

                # 根据配置决定是否删除原始下载文件
                if delete_original:
                    try:
                        os.remove(temp_path)
                        self._logger.debug("Original download file deleted")
                    except Exception as e:
                        self._logger.warning(f"Failed to delete original file: {e}")
                else:
                    self._logger.debug("Original download file retained")

                self._logger.info(f"Video split sent successfully, {sent_count}/{len(all_split_files)} parts sent")
                return self._make_return_value(True, True, f"已发送分块视频（{sent_count}个片段）")
            else:
                self._logger.warning("Video split failed, sending original video")
                # 不在此处清理目录，保持一致性；下一次下载前会清理
                should_split = False

        if not should_split:
            # 处理单个视频文件（不分块）
            final_video_path = temp_path

            # 如果文件过大且启用压缩，先压缩
            if video_size_mb > max_video_size_mb and enable_compression and ffmpeg_info["ffmpeg_available"]:
                self._logger.debug(
                    f"Single video file size ({video_size_mb:.2f}MB) exceeds limit, starting compression..."
                )

                compressed_path = temp_path.replace(".mp4", "_compressed.mp4")
                # 构建配置字典传递给压缩器
                config_dict = {
                    "ffmpeg": {
                        "enable_hardware_acceleration": self.get_config("ffmpeg.enable_hardware_acceleration", True),
                        "force_encoder": self.get_config("ffmpeg.force_encoder", ""),
                        "encoder_priority": self.get_config(
                            "ffmpeg.encoder_priority", ["nvidia", "intel", "amd", "apple"]
                        ),
                    }
                }
                compressor = VideoCompressor(ffmpeg_info["ffmpeg_path"], config_dict)

                if compressor.compress_video(temp_path, compressed_path, max_video_size_mb, compression_quality):
                    compressed_size_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                    self._logger.debug(
                        f"Single video compression successful: {video_size_mb:.2f}MB -> {compressed_size_mb:.2f}MB"
                    )
                    final_video_path = compressed_path

                    # 删除原始文件（如果配置允许）
                    if delete_original:
                        try:
                            os.remove(temp_path)
                            self._logger.debug(f"Original video file {temp_path} deleted")
                        except Exception as e:
                            self._logger.warning(f"Failed to delete original video file: {e}")
                else:
                    self._logger.debug("Single video compression failed, using original file")
            elif video_size_mb > max_video_size_mb:
                self._logger.debug(
                    f"Single video file size ({video_size_mb:.2f}MB) exceeds limit but compression not available"
                )
            else:
                self._logger.debug(
                    f"Single video file size ({video_size_mb:.2f}MB) meets requirements, no compression needed"
                )

            # 发送处理后的视频文件
            async def _try_send(path: str) -> bool:
                # 在发送前进行WSL路径转换
                enable_conversion = self.get_config("wsl.enable_path_conversion", True)
                converted_path = convert_windows_to_wsl_path(path) if enable_conversion else path

                self._logger.debug(f"Sending single video - path conversion enabled: {enable_conversion}")
                self._logger.debug(f"Sending single video - original path: {path}")
                self._logger.debug(f"Sending single video - converted path: {converted_path}")

                # 检查是否为私聊消息
                is_private = self._is_private_message(message)

                if is_private:
                    # 私聊消息，使用专用API发送
                    user_id = self._get_user_id(message)
                    if user_id:
                        self._logger.debug(f"Private message detected, sending private video API to user: {user_id}")
                        return await self._send_private_video(path, converted_path, user_id)
                    else:
                        self._logger.error("Private message but unable to get user ID")
                        return False
                else:
                    # 群聊消息，使用群视频API
                    group_id = self._get_group_id(message)
                    if group_id:
                        self._logger.debug(f"Group message detected, sending group video API to group: {group_id}")
                        return await self._send_group_video(path, converted_path, group_id)
                    else:
                        self._logger.error("Group message detected but unable to get group ID, sending failed")
                        return False

            sent_ok = await _try_send(final_video_path)
            if not sent_ok:
                self._logger.debug("Video sending failed")
                await self._send_text("视频解析成功，但发送失败。请检查网络连接和API配置。", stream_id)
            else:
                self._logger.info("Video file sent successfully")

            # 删除临时文件
            try:
                # 删除最终处理的文件
                if os.path.exists(final_video_path):
                    os.remove(final_video_path)
                    self._logger.debug(f"Processed video file {final_video_path} deleted")

                # 如果还有原始文件且不同于最终文件，也删除
                if final_video_path != temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                    self._logger.debug(f"Original video file {temp_path} deleted")
            except Exception as e:
                self._logger.warning(f"Failed to delete temporary file: {e}")

        self._logger.info("Bilibili video processing completed")
        return self._make_return_value(True, True, "已发送视频（若宿主支持）")

    async def _send_video_part(
        self, original_path: str, converted_path: str, caption: str, stream_id: str, message: NachoMessages
    ) -> bool:
        """发送视频分块片段

        Args:
            original_path: 原始文件路径（用于文件检查和base64读取）
            converted_path: 转换后的路径（用于发送URI）
            caption: 视频标题
            stream_id: 流ID
            message: 消息对象
        """
        try:
            # 检查文件是否存在（使用原始路径）
            if not os.path.exists(original_path):
                self._logger.error(f"Video part file does not exist: {original_path}")
                return False

            # 检查文件大小（使用原始路径）
            file_size = os.path.getsize(original_path)
            if file_size == 0:
                self._logger.error(f"Video part file size is 0: {original_path}")
                return False

            self._logger.debug(
                f"Preparing to send video part: {original_path} -> {converted_path}, size: {file_size} bytes"
            )

            # 检查是否为私聊消息
            is_private = self._is_private_message(message)

            if is_private:
                # 私聊消息，使用专用API发送
                user_id = self._get_user_id(message)
                if user_id:
                    self._logger.debug(
                        f"Private message detected in split video, sending private video API to user: {user_id}"
                    )
                    return await self._send_private_video(original_path, converted_path, user_id)
                else:
                    self._logger.error("Private message but unable to get user ID")
                    return False
            else:
                # 群聊消息，使用群视频API
                group_id = self._get_group_id(message)
                if group_id:
                    self._logger.debug(
                        f"Group message detected in split video, sending group video API to group: {group_id}"
                    )
                    return await self._send_group_video(original_path, converted_path, group_id)
                else:
                    self._logger.error(
                        "Group message detected in split video but unable to get group ID, sending failed"
                    )
                    return False

        except Exception as e:
            self._logger.debug(f"Failed to send video part: {e}")

        self._logger.error(f"All sending methods failed: {original_path}")
        return False


@register_plugin
class BilibiliVideoSenderPlugin(BasePlugin):
    """B站视频解析与自动发送插件。"""

    _logger = get_logger("plugin.bilibili_video_sender.plugin")

    plugin_name: str = "bilibili_video_sender_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "ffmpeg": "FFmpeg相关配置",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "use_new_events_manager": ConfigField(
                type=bool,
                default=False,
                description="是否使用新版events_manager（0.10.2及以上版本设为true，否则设为false）",
            ),
        },
        "bilibili": {
            "use_wbi": ConfigField(type=bool, default=True, description="是否使用WBI签名（推荐开启）"),
            "prefer_dash": ConfigField(type=bool, default=True, description="是否优先使用DASH格式（推荐开启）"),
            "fnval": ConfigField(
                type=int,
                default=4048,
                description="视频流格式标识（1=MP4, 16=FLV, 80=DASH, 64=MP4+DASH, 32=MP4+FLV+DASH, 128=MP4+FLV+DASH+8K, 256=MP4+FLV+DASH+8K+HDR, 512=MP4+FLV+DASH+8K+HDR+杜比, 1024=MP4+FLV+DASH+8K+HDR+杜比+AV1, 2048=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度, 4096=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度+8K360度, 8192=MP4+FLV+DASH+8K+HDR+杜比+AV1+360度+8K360度+HDR360度）",
            ),
            "fourk": ConfigField(type=bool, default=True, description="是否允许4K视频（需要大会员）"),
            "qn": ConfigField(
                type=int,
                default=0,
                description="视频清晰度选择（0=自动, 6=240P, 16=360P, 32=480P, 64=720P, 80=1080P, 112=1080P+, 116=1080P60, 120=4K, 125=HDR, 126=杜比视界）",
            ),
            "platform": ConfigField(
                type=str, default="pc", description="平台类型（pc=web播放, html5=移动端HTML5播放）"
            ),
            "high_quality": ConfigField(
                type=bool, default=False, description="是否启用高画质模式（platform=html5时有效）"
            ),
            "try_look": ConfigField(
                type=bool, default=False, description="是否启用游客高画质尝试模式（未登录时可能获取720P和1080P）"
            ),
            "sessdata": ConfigField(
                type=str, default="", description="B站登录Cookie中的SESSDATA值（用于获取高清晰度视频）"
            ),
            "buvid3": ConfigField(type=str, default="", description="B站设备标识Buvid3（用于生成session参数）"),
            "enable_video_splitting": ConfigField(type=bool, default=True, description="是否启用视频分块功能"),
            "delete_original_after_split": ConfigField(type=bool, default=True, description="是否在分块后删除原始文件"),
            "max_video_size_mb": ConfigField(
                type=int, default=100, description="视频文件大小限制（MB），超过此大小将进行压缩或分割"
            ),
            "enable_video_compression": ConfigField(type=bool, default=True, description="是否启用视频压缩功能"),
            "compression_quality": ConfigField(
                type=int, default=23, description="视频压缩质量 (1-51，数值越小质量越高，推荐18-28)"
            ),
            "use_optimized_splitting": ConfigField(
                type=bool, default=True, description="是否使用迭代优化分割算法（获得更接近目标大小的分片，但耗时较长）"
            ),
        },
        "ffmpeg": {
            "show_warnings": ConfigField(type=bool, default=True, description="是否显示FFmpeg相关警告信息"),
            "enable_hardware_acceleration": ConfigField(
                type=bool, default=True, description="是否启用硬件加速自动检测（推荐开启，可大幅提升视频压缩速度）"
            ),
            "force_encoder": ConfigField(
                type=str,
                default="",
                description="强制使用特定编码器（留空则自动选择，可选值：libx264/h264_nvenc/h264_qsv/h264_amf/h264_videotoolbox）",
            ),
            "encoder_priority": ConfigField(
                type=list,
                default=["nvidia", "intel", "amd", "apple"],
                description="编码器优先级（当检测到多个硬件编码器时的选择顺序）",
            ),
        },
        "wsl": {
            "enable_path_conversion": ConfigField(
                type=bool, default=True, description="是否启用Windows到WSL的路径转换"
            ),
        },
        "api": {
            "port": ConfigField(type=int, default=5700, description="API服务端口号"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (BilibiliAutoSendHandler.get_handler_info(), BilibiliAutoSendHandler),
        ]
