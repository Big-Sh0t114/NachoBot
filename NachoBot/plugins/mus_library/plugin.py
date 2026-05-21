from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple, List, Type, Dict
import json, re, base64, random, difflib
import hashlib, time
import asyncio, os, tempfile, wave, audioop
import urllib.request, urllib.error
from urllib.parse import urljoin

from src.plugin_system import BasePlugin, register_plugin, BaseCommand


PLUGIN_DIR = Path(__file__).parent
LIB_PATH = PLUGIN_DIR / "music_library.json"
AUDIO_DIR = PLUGIN_DIR / "audio"
TODO_LIST_PATH = PLUGIN_DIR / "list.txt"
DUMP_LIST_PATH = PLUGIN_DIR / "dump.txt"
AUDIO_EXT_WHITELIST = {".wav"}


def _cfg(obj, key: str, default=None):
    """读取插件配置；无则给默认值。"""
    try:
        cfg = getattr(obj, "config", None) or {}
        return cfg.get(key, default)
    except Exception:
        return default


def _load_library() -> List[dict]:
    """读取曲库，并在需要时把 audio 目录中的新文件自动登记进去。"""
    data: List[dict] = []
    try:
        with open(LIB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                data = []
    except Exception:
        data = []

    return _sync_library_with_audio(data)


def _persist_library(data: List[dict]) -> None:
    try:
        LIB_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _normalize_file_key(rel_path: str) -> str:
    try:
        return Path(rel_path).name.casefold()
    except Exception:
        return ""


def _sync_library_with_audio(library: List[dict]) -> List[dict]:
    """保证 music_library.json 覆盖 audio 目录下新增的 WAV 文件。"""
    updated = list(library)
    changed = False

    if not AUDIO_DIR.exists():
        return updated

    known_files = set()
    known_titles = set()

    for item in updated:
        if not isinstance(item, dict):
            continue
        title_key = _normalize_query(item.get("title", ""))
        file_key = _normalize_file_key(str(item.get("file", "")))
        if title_key:
            known_titles.add(title_key)
        if file_key:
            known_files.add(file_key)

    added: List[dict] = []
    for audio_file in AUDIO_DIR.iterdir():
        if not audio_file.is_file() or audio_file.suffix.lower() not in AUDIO_EXT_WHITELIST:
            continue

        file_key = audio_file.name.casefold()
        if file_key in known_files:
            continue

        title = audio_file.stem.strip() or audio_file.name
        title_key = _normalize_query(title)

        # 如果库里已有同名歌曲但未记录文件，则补全文件路径
        if title_key and title_key in known_titles:
            for item in updated:
                if _normalize_query(item.get("title", "")) == title_key and not _normalize_file_key(
                    str(item.get("file", ""))
                ):
                    item["file"] = audio_file.relative_to(PLUGIN_DIR).as_posix()
                    known_files.add(file_key)
                    changed = True
                    break
            continue

        added.append(
            {
                "title": title,
                "artist": "",
                "aliases": [title],
                "file": audio_file.relative_to(PLUGIN_DIR).as_posix(),
            }
        )
        known_files.add(file_key)
        if title_key:
            known_titles.add(title_key)
        changed = True

    if added:
        # 保持输出稳定：新增的按名称排序后插入末尾
        added.sort(key=lambda s: _normalize_query(s.get("title", "")))
        updated.extend(added)

    if changed:
        _persist_library(updated)

    return updated


def _match_best(query: str, library: List[dict]) -> Tuple[Optional[dict], float]:
    """优先 rapidfuzz；缺包则退化为子串匹配。"""
    try:
        from rapidfuzz import process, fuzz  # type: ignore

        keys, idx = [], []
        for i, s in enumerate(library):
            fields = [s.get("title", ""), s.get("artist", "")] + (s.get("aliases", []) or [])
            for k in filter(None, fields):
                keys.append(k)
                idx.append(i)
        if not keys:
            return None, 0.0
        # 默认用 WRatio，容错更高（大小写/间距/错别字）
        matched, score, pos = process.extractOne(query, keys, scorer=fuzz.WRatio)
        return (library[idx[pos]] if matched else None), float(score or 0)
    except Exception:
        q = query.lower()
        for s in library:
            fields = " ".join([s.get("title", ""), s.get("artist", "")] + (s.get("aliases", []) or []))
            if q in fields.lower():
                return s, 100.0
        return None, 0.0


def _normalize_query(text: str) -> str:
    return (text or "").strip().casefold()


def _load_pending_list() -> set[str]:
    try:
        with open(TODO_LIST_PATH, "r", encoding="utf-8") as f:
            items: set[str] = set()
            for line in f:
                norm = _normalize_query(line)
                if norm:
                    items.add(norm)
            return items
    except Exception:
        return set()


def _append_pending(query: str) -> None:
    norm = _normalize_query(query)
    if not norm:
        return
    try:
        existing = _load_pending_list()
        hit, _ = _fuzzy_contains(norm, existing, 85)
        if hit:
            return
        if not TODO_LIST_PATH.exists():
            _ensure_dump_template()
            TODO_LIST_PATH.touch(exist_ok=True)
        with open(TODO_LIST_PATH, "a", encoding="utf-8") as f:
            f.write(query.strip() + "\n")
    except Exception:
        pass


def _ensure_dump_template() -> None:
    if DUMP_LIST_PATH.exists():
        return
    try:
        DUMP_LIST_PATH.write_text("[dump]\n\n[invaild]\n", encoding="utf-8")
    except Exception:
        pass


def _load_dump_categories() -> Tuple[set[str], set[str]]:
    """读取 dump.txt，返回 (dump_set, invalid_set)。"""
    dump_set: set[str] = set()
    invalid_set: set[str] = set()
    try:
        with open(DUMP_LIST_PATH, "r", encoding="utf-8") as f:
            section = None
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                lower = line.casefold()
                if lower == "[dump]":
                    section = "dump"
                    continue
                if lower == "[invaild]":
                    section = "invaild"
                    continue
                norm = _normalize_query(line)
                if not norm:
                    continue
                if section == "dump":
                    dump_set.add(norm)
                elif section == "invaild":
                    invalid_set.add(norm)
        return dump_set, invalid_set
    except Exception:
        return dump_set, invalid_set


def _best_similarity(query: str, candidates: List[str]) -> Tuple[float, Optional[str]]:
    best_score = 0.0
    best_item: Optional[str] = None
    if not query or not candidates:
        return best_score, best_item
    try:
        from rapidfuzz import process, fuzz  # type: ignore

        matched = process.extractOne(query, candidates, scorer=fuzz.WRatio)
        if matched:
            item, score, _ = matched
            return float(score or 0), item
        return 0.0, None
    except Exception:
        for c in candidates:
            score = difflib.SequenceMatcher(None, query, c).ratio() * 100
            if score > best_score:
                best_score = score
                best_item = c
        return best_score, best_item


def _fuzzy_contains(query: str, candidates: set[str], threshold: float) -> Tuple[bool, float]:
    score, _ = _best_similarity(query, list(candidates))
    return score >= threshold, score


async def _play_song(cmd: BaseCommand, song: dict) -> Tuple[bool, Optional[str], bool]:
    """共用播放流程：根据 song dict 发送语音/文件。"""
    rel = song.get("file") or ""
    wav = (PLUGIN_DIR / rel).resolve() if rel else (AUDIO_DIR / f"{song.get('title', '')}.wav").resolve()
    if not wav.exists():
        await cmd.send_text(f"找到歌曲 {song.get('title', '?')}，但音频缺失：{wav.name}")
        return True, f"file_missing:{wav.name}", True

    prefer_silk = bool(cmd.get_config("mus_library.prefer_silk", _cfg(cmd, "prefer_silk", True)))  # type: ignore
    silk_bitrate = int(cmd.get_config("mus_library.silk_bitrate", _cfg(cmd, "silk_bitrate", 24000)) or 24000)
    cache_ttl_hours = float(cmd.get_config("mus_library.cache_ttl_hours", _cfg(cmd, "cache_ttl_hours", 0)) or 0)
    debug_timing = bool(cmd.get_config("mus_library.debug_timing", _cfg(cmd, "debug_timing", False)))

    src_wav = await _trim_wav(wav, 0)

    if prefer_silk:
        if debug_timing:
            try:
                import rsilk  # type: ignore

                try:
                    await cmd.send_text(f"[mus_library] rsilk OK @ {silk_bitrate}bps")
                except Exception:
                    pass
            except Exception as e:
                try:
                    await cmd.send_text(f"[mus_library] rsilk import failed: {type(e).__name__}: {e}")
                except Exception:
                    pass

        cache_path = _silk_cache_path(src_wav, silk_bitrate)
        t0 = time.time()
        silk, hit = await _get_or_build_silk(src_wav, silk_bitrate, cache_ttl_hours)
        if silk:
            ok = await _send_record_v11(cmd, silk)
            if ok:
                if debug_timing:
                    ms = int((time.time() - t0) * 1000)
                    src = "cache" if hit else "encode"
                    try:
                        await cmd.send_text(f"[mus_library] SILK {src} {ms}ms @{silk_bitrate}bps -> {cache_path.name}")
                    except Exception:
                        pass
                if src_wav != wav:
                    try:
                        src_wav.unlink(missing_ok=True)
                    except Exception:
                        pass
                return True, f"play:{song.get('title', '?')}", True
            else:
                print(f"[mus_library] HTTP send failed for SILK. Fallback to WS sending SILK...")
                try:
                    # Fallback: try sending SILK via WS directly
                    voice_b64 = base64.b64encode(silk).decode("ascii")
                    ok = await cmd.send_voice(voice_b64)
                    if ok:
                        if debug_timing:
                            try:
                                await cmd.send_text(f"[mus_library] SILK(WS) sent. Fallback success.")
                            except Exception:
                                pass
                        if src_wav != wav:
                            try:
                                src_wav.unlink(missing_ok=True)
                            except Exception:
                                pass
                        return True, f"play_ws:{song.get('title', '?')}", True
                except Exception as e:
                    print(f"[mus_library] SILK WS fallback failed: {e}")

        else:
            if debug_timing:
                try:
                    await cmd.send_text("[mus_library] SILK encode returned None (codec unavailable or error)")
                except Exception:
                    pass

    # Safety check: Do not send large WAVs over WS
    try:
        fsize = src_wav.stat().st_size
        if fsize > 2 * 1024 * 1024:  # 2MB limit
            await cmd.send_text(
                f"[mus_library] 发送失败：HTTP接口未配置或连接失败，且文件过大({fsize / 1024 / 1024:.1f}MB)无法通过WS发送。请检查 config.toml 中的 onebot_base 配置。"
            )
            if src_wav != wav:
                src_wav.unlink(missing_ok=True)
            return True, "file_too_large", True
    except Exception:
        pass

    try:
        voice_b64 = base64.b64encode(src_wav.read_bytes()).decode("ascii")
        ok = await cmd.send_voice(voice_b64)
        if ok:
            if src_wav != wav:
                try:
                    src_wav.unlink(missing_ok=True)
                except Exception:
                    pass
            return True, f"play:{song.get('title', '?')}", True
    except Exception:
        pass

    ok = await _send_file_v11(cmd, src_wav)
    if ok:
        if src_wav != wav:
            try:
                src_wav.unlink(missing_ok=True)
            except Exception:
                pass
        return True, f"file:{song.get('title', '?')}", True

    if src_wav != wav:
        try:
            src_wav.unlink(missing_ok=True)
        except Exception:
            pass
    await cmd.send_text("[mus_library] 发送失败：适配器不支持语音/文件。")
    return True, "adapter_unsupported", True


def _as_base64_uri_from_bytes(b: bytes) -> str:
    return "base64://" + base64.b64encode(b).decode("ascii")


def _as_base64_uri_from_path(p: Path) -> str:
    return "base64://" + base64.b64encode(p.read_bytes()).decode("ascii")


async def _trim_wav(src: Path, max_seconds: int) -> Path:
    """把 WAV 裁剪为前 max_seconds 秒；max_seconds<=0 则返回原文件。"""
    if not max_seconds or max_seconds <= 0:
        return src
    tmp = Path(tempfile.gettempdir()) / f"mus_trim_{os.getpid()}_{int(asyncio.get_event_loop().time() * 1000)}.wav"
    with wave.open(str(src), "rb") as r:
        ch, sw, sr, n = r.getnchannels(), r.getsampwidth(), r.getframerate(), r.getnframes()
        frames_keep = min(n, int(max_seconds * sr))
        data = r.readframes(frames_keep)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        w.writeframes(data)
    return tmp


async def _wav_to_silk_py(wav_path: Path, bit_rate: int = 24000) -> Optional[bytes]:
    """纯 Python：wave+audioop 预处理到 24k/mono/16bit，再用 rsilk 编码为 SILK（Tencent 容器）。"""
    try:
        import rsilk  # pip install rsilk
    except Exception:
        return None

    def _encode() -> Optional[bytes]:
        # 放到线程里跑，避免阻塞事件循环
        try:
            with wave.open(str(wav_path), "rb") as w:
                n_ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
                pcm = w.readframes(n)
            if sw != 2:
                pcm_local = audioop.lin2lin(pcm, sw, 2)
            else:
                pcm_local = pcm
            if n_ch != 1:
                pcm_local = audioop.tomono(pcm_local, 2, 0.5, 0.5)
            if sr != 24000:
                pcm_local, _ = audioop.ratecv(pcm_local, 2, 1, sr, 24000, None)

            br = int(max(8000, min(int(bit_rate or 24000), 40000)))
            return rsilk.encode(
                input=pcm_local, sample_rate=24000, bit_rate=br, max_internal_sample_rate=24000, tencent=True
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"[mus_library] rsilk encode failed: {e}")
            return None

    try:
        return await asyncio.to_thread(_encode)
    except Exception as e:
        print(f"[mus_library] _wav_to_silk_py thread failed: {e}")
        return None


def _silk_cache_path(wav_path: Path, bit_rate: int) -> Path:
    st = wav_path.stat()
    raw = f"{wav_path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{int(bit_rate or 24000)}"
    key = hashlib.md5(raw.encode("utf-8")).hexdigest()
    cache_dir = PLUGIN_DIR / "cache_silk"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.silk"


async def _get_or_build_silk(wav_path: Path, bit_rate: int, ttl_hours: float) -> Tuple[Optional[bytes], bool]:
    """返回 (silk_bytes, cache_hit)。ttl_hours<=0 则不使用缓存。
    以 wav 绝对路径 + mtime_ns + size + bitrate 作为缓存键，缓存到 .cache 目录。
    """
    try:
        ttl = float(ttl_hours or 0)
    except Exception:
        ttl = 0.0

    cache_file = _silk_cache_path(wav_path, bit_rate)

    if ttl > 0 and cache_file.exists():
        try:
            age_sec = time.time() - cache_file.stat().st_mtime
            if age_sec <= ttl * 3600:
                return cache_file.read_bytes(), True
        except Exception:
            pass

    silk = await _wav_to_silk_py(wav_path, bit_rate)
    if silk:
        try:
            cache_file.write_bytes(silk)
        except Exception:
            pass
    return silk, False


async def _http_post_json(url: str, payload: dict, headers: Dict[str, str] | None = None) -> tuple[int, str]:
    def _do():
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdr = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            hdr.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdr)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "ignore")

    try:
        return await asyncio.to_thread(_do)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "ignore")
        except Exception:
            body = str(e)
        print(f"[mus_library] HTTP POST Error {e.code}: {body}")
        return e.code, body
    except Exception as e:
        print(f"[mus_library] HTTP POST Exception: {type(e).__name__}: {e}")
        return 0, f"{type(e).__name__}: {e}"


async def _send_record_v11(cmd: BaseCommand, silk_bytes: bytes) -> bool:
    """用 OneBot v11 的 record 段发送语音（群聊/私聊）。"""
    ob_base = str(_cfg(cmd, "onebot_base", "http://127.0.0.1:5700")).rstrip("/")
    token = _cfg(cmd, "onebot_token", "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    msg = getattr(cmd, "message", None)
    force_gid = _cfg(cmd, "nonebot_force_group_id", None) or _cfg(cmd, "onebot_force_group_id", None)
    group_id = (
        (str(force_gid).strip() if force_gid else None)
        or getattr(msg, "group_id", None)
        or getattr(getattr(cmd, "chat_stream", None), "group_id", None)
    )
    user_id = getattr(msg, "user_id", None) or getattr(getattr(cmd, "chat_stream", None), "user_id", None)

    uri = _as_base64_uri_from_bytes(silk_bytes)

    if group_id:
        url = urljoin(ob_base + "/", "send_group_msg")
        payload = {"group_id": int(group_id), "message": [{"type": "record", "data": {"file": uri}}]}
        code, body = await _http_post_json(url, payload, headers)
        if (200 <= code < 300) and ('"status":"ok"' in body.lower() or '"retcode":0' in body):
            return True
    if user_id:
        url = urljoin(ob_base + "/", "send_private_msg")
        payload = {"user_id": int(user_id), "message": [{"type": "record", "data": {"file": uri}}]}
        code, body = await _http_post_json(url, payload, headers)
        if (200 <= code < 300) and ('"status":"ok"' in body.lower() or '"retcode":0' in body):
            return True
    return False


async def _send_file_v11(cmd: BaseCommand, wav_path: Path) -> bool:
    """兜底：上传群文件（base64 传输，规避中文路径）。私聊无官方上传接口，忽略。"""
    ob_base = str(_cfg(cmd, "onebot_base", "http://127.0.0.1:5700")).rstrip("/")
    token = _cfg(cmd, "onebot_token", "")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    msg = getattr(cmd, "message", None)
    force_gid = _cfg(cmd, "nonebot_force_group_id", None) or _cfg(cmd, "onebot_force_group_id", None)
    group_id = (
        (str(force_gid).strip() if force_gid else None)
        or getattr(msg, "group_id", None)
        or getattr(getattr(cmd, "chat_stream", None), "group_id", None)
    )

    if not group_id:
        return False

    url = urljoin(ob_base + "/", "upload_group_file")
    payload = {"group_id": int(group_id), "file": _as_base64_uri_from_path(wav_path), "name": wav_path.name}
    code, body = await _http_post_json(url, payload, headers)
    return (200 <= code < 300) and ('"status":"ok"' in body.lower() or '"retcode":0' in body)


class PlayMusicCommand(BaseCommand):
    """点歌命令：点歌/播放/来首 + 关键词"""

    command_name = "play"
    command_description = "点歌/播放/来首 + 关键词，匹配内置音乐库并以语音播放"
    command_pattern = r"^(?:\S+\s+)?(?:点歌|播放|来首)\s*(?P<query>.+)$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        try:
            query = (self.matched_groups or {}).get("query", "").strip()
            if not query and getattr(self, "message", None):
                text = (getattr(self.message, "text", "") or "").strip()
                m = re.match(self.command_pattern, text)
                if m:
                    query = (m.group("query") or "").strip()
            if not query:
                await self.send_text("请在指令后添加歌名，例如：点歌 打上花火")
                return True, "no_query", True

            norm_query = _normalize_query(query)
            dump_list, invalid_list = _load_dump_categories()
            if norm_query:
                hit_dump, _ = _fuzzy_contains(norm_query, dump_list, 85)
                if hit_dump:
                    await self.send_text("这不是歌吧(´-ω-`)")
                    return True, f"dump:{query}", True
                hit_invalid, _ = _fuzzy_contains(norm_query, invalid_list, 85)
                if hit_invalid:
                    await self.send_text(f"「{query}」太难了qwq，NachoBot学不会喵")
                    return True, f"invaild:{query}", True

            lib = _load_library()
            song, score = _match_best(query, lib)
            if not song or score < 80:
                pending = _load_pending_list()
                hit_pending, _ = _fuzzy_contains(norm_query, pending, 85)
                if norm_query and hit_pending:
                    await self.send_text(f"NachoBot已经在很努力的学「{query}」了喵")
                    return True, f"no_match_pending:{query}", True

                _append_pending(query)
                await self.send_text(
                    f"「{query}」现在还不会唱喵..（相似度 {int(score)}），已经加入NachoBot的待做清单了！"
                )
                return True, f"no_match:{query}", True
            return await _play_song(self, song)

        except Exception as e:
            try:
                await self.send_text(f"[mus_library] 执行异常: {type(e).__name__}: {e}")
            finally:
                return True, "exception", True


class RandomMusicCommand(BaseCommand):
    """随机播放曲库中的一首歌曲 (#mus_rand)"""

    command_name = "mus_rand"
    command_description = "随机从曲库挑选一首歌并播放"
    command_pattern = r"^#mus_rand$"

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        try:
            lib = _load_library()
            if not lib:
                await self.send_text("曲库是空的喵，先去添加几首歌吧~")
                return True, "library_empty", True

            song = random.choice(lib)
            try:
                await self.send_text(f"那就来一首「{song.get('title', '?')}」好了喵(´-ω-` )")
            except Exception:
                pass

            return await _play_song(self, song)

        except Exception as e:
            try:
                await self.send_text(f"[mus_library] 随机播放异常: {type(e).__name__}: {e}")
            finally:
                return True, "exception", True


@register_plugin
class MusicPlayerPlugin(BasePlugin):
    """Mus Library 插件：内置音乐库点歌并语音播放（低延迟整合版）"""

    plugin_name = "mus_library"
    enable_plugin = True

    dependencies: List[str] = []
    python_dependencies: List[str] = ["rsilk"]

    # 配置节描述（可选）：便于外部展示
    config_section_descriptions = {
        "plugin": "插件启用配置",
    }

    config_file_name: str = "config.toml"
    config_schema: dict = {
        # 插件开关
        "plugin": {
            "enable": {"type": "boolean", "default": True, "description": "是否启用插件"},
        },
        "onebot_base": {"type": "string", "default": "http://127.0.0.1:5700", "description": "Napcat OneBot HTTP 地址"},
        "onebot_token": {"type": "string", "default": "", "description": "Napcat OneBot HTTP Token（可留空）"},
        "nonebot_force_group_id": {
            "type": "string",
            "default": "",
            "description": "可选：强制把消息发到此群（拿不到 group_id 时兜底）",
        },
        "prefer_silk": {
            "type": "boolean",
            "default": True,
            "description": "优先本地转 SILK 并以 record 段发送（低延迟）",
        },
        "silk_bitrate": {"type": "integer", "default": 24000, "description": "SILK 编码比特率（8k~40k）"},
        "cache_ttl_hours": {"type": "number", "default": 0, "description": "SILK 磁盘缓存有效期（小时，0 关闭）"},
        "debug_timing": {"type": "boolean", "default": False, "description": "打印编码/缓存耗时（调试）"},
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 读取 [plugin.enable] 来控制插件开关（默认启用）
        if self.get_config("plugin.enable", True):
            self.enable_plugin = True
        else:
            self.enable_plugin = False

    def get_plugin_components(self) -> List[Tuple[object, Type]]:
        return [
            (PlayMusicCommand.get_command_info(), PlayMusicCommand),
            (RandomMusicCommand.get_command_info(), RandomMusicCommand),
        ]
