"""Configuration generation and dependency deployment for the WebUI wizard."""

import asyncio
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import tomlkit

try:
    from .setup_checks import (
        BUILTIN_BILIBILI_TEMPLATE,
        BUILTIN_KOISHI_TEMPLATE,
        EnvironmentChecker,
        ROOT_DIR,
        TEMPLATE_MAP,
    )
    from .secure_paths import ensure_within, resolve_external_path, resolve_relative_to_root
    from .multimodal_runtime import MultimodalRuntimeManager
except ImportError:
    from setup_checks import (
        BUILTIN_BILIBILI_TEMPLATE,
        BUILTIN_KOISHI_TEMPLATE,
        EnvironmentChecker,
        ROOT_DIR,
        TEMPLATE_MAP,
    )
    from secure_paths import ensure_within, resolve_external_path, resolve_relative_to_root
    from multimodal_runtime import MultimodalRuntimeManager

BACKUP_DIR = ROOT_DIR / "config-save" / "setup_backups"
MAX_BACKUPS_PER_FILE = 5

# These are deliberately tied to the checked-in fresh templates.  A changed
# template must fail closed instead of accidentally replacing an unrelated
# value (or overwriting a live credential with a new one).
DISCORD_KOISHI_TARGET = "koishi-app/koishi.yml"
DISCORD_VC_TARGET = "NachoBot-DiscordVC-Adapter/config.toml"
DISCORD_KOISHI_PLACEHOLDER = "<YOUR_DISCORD_BOT_TOKEN_HERE>"
DISCORD_VC_PLACEHOLDER = "YOUR_DISCORD_BOT_TOKEN"
BILIBILI_TARGET = "NachoBot-Bilibili-Adapter/config.toml"


# Sanitized, tracked fallback templates.  The user-owned template files in
# the repository may be present for local customization, but deployment must
# remain usable when they are absent from a clean checkout/package.
KOISHI_TEMPLATE_TEXT = """plugins:
  group:server:
    server:e5r2g6:
      port: 5140
      maxPort: 5149
    ~server-satori:afwp8z: {}
    ~server-temp:zcji9z: {}
  group:basic:
    ~admin:9rsa7e: {}
    ~bind:28lwd6: {}
    commands:219zrk: {}
    help:mw5ufg: {}
    http:up8zo1: {}
    ~inspect:uu4df9: {}
    locales:e1mv6f: {}
    proxy-agent:n3qo79:
      proxyAgent: http://127.0.0.1:7897
    rate-limit:241jid: {}
    telemetry:nym5b5: {}
    ./nachobot-slash-bridge:nbcmd:
      host: 127.0.0.1
      port: 8000
      platform: discord
      enableLocalLangSwitch: false
      silentCommands:
        - adv-on
        - adv-off
        - mute
        - mus-rand
        - help-all
        - lang-switch
      localReplies:
        help-all: ''
        lang-switch: ''
  group:console:
    actions:0w1i5w: {}
    analytics:19gqdw: {}
    android:7qa6m8:
      $if: env.KOISHI_AGENT?.includes('Android')
    ~auth:zyvwiu: {}
    config:kjd7fa: {}
    console:idr73d:
      open: true
    dataview:ly3300: {}
    desktop:5fk3p3:
      $if: env.KOISHI_AGENT?.includes('Desktop')
    explorer:zp5pt4: {}
    logger:u9fhuz: {}
    insight:ncdyq8: {}
    market:8zm33h:
      search:
        endpoint: https://registry.koishi.chat/index.json
    notifier:56uyop: {}
    oobe:s8acau: {}
    sandbox:u89x0b: {}
    status:bcr45b: {}
    theme-vanilla:i268dq: {}
  group:storage:
    ~database-mongo:1hb2ow:
      database: koishi
    ~database-mysql:4szu05:
      database: koishi
    ~database-postgres:7nf60j:
      database: koishi
    database-sqlite:4b6xgh:
      path: data/koishi.db
    assets-local:79fukq: {}
  group:adapter:
    ~adapter-dingtalk:wp560n: {}
    adapter-discord:97kjzj:
      token: <YOUR_DISCORD_BOT_TOKEN_HERE>
      intents:
        - GUILDS
        - GUILD_MEMBERS
        - GUILD_MESSAGES
        - GUILD_MESSAGE_REACTIONS
        - GUILD_MESSAGE_TYPING
        - DIRECT_MESSAGES
        - DIRECT_MESSAGE_REACTIONS
        - DIRECT_MESSAGE_TYPING
        - MESSAGE_CONTENT
    ~adapter-kook:dfaoua: {}
    ~adapter-lark:439n9n: {}
    ~adapter-line:4gklsb: {}
    ~adapter-mail:jfaioj: {}
    ~adapter-matrix:sq583x: {}
    ~adapter-qq:4cl7rd: {}
    ~adapter-satori:wvr2kj: {}
    ~adapter-slack:32tf5t: {}
    ~adapter-telegram:lacka0: {}
    ~adapter-wechat-official:70m36x: {}
    ~adapter-wecom:7be9j0: {}
    ~adapter-whatsapp:miu2cl: {}
    ~adapter-zulip:47r8qg: {}
  group:develop:
    $if: env.NODE_ENV === 'development'
    hmr:0dnupx:
      root: .
  server-onebot:wdndch:
    platform: discord
    selfId: ' '
    enabledWs: true
    path: /onebot/v11/ws
    selfname: NachoBot
    groupname: discord-channel
    loggerinfo: false
"""


BILIBILI_TEMPLATE_TEXT = r'''[inner]
version = "0.6.0"

[nachobot_server]
host = "127.0.0.1"
port = 8000
platform = "bilibili"

[bilibili]
bot_account = " "       # 填写bot b站id
sessdata = ""           # 以下字段请运行适配器目录下的 qr_login.py 自动填写
bili_jct = ""
buvid3 = ""
buvid4 = ""
dede_user_id = ""
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

[live]
enable = true
enable_live2D = true
live2d_url = "ws://127.0.0.1:8766"
live2d_token = ""
live2d_reconnect_seconds = 3.0
room_ids = [ ]       # 填写直播间id
use_wss = true
heartbeat_interval = 30
network_search_enabled = true
live_person_profile_enabled = true
reconnect_seconds = 5
max_reconnect_seconds = 60
ws_proxy = "none"
open_timeout = 10
max_hosts = 3
max_attempts = 4
proxy_pool_path = "proxy.json"
proxy_check_url = "https://www.baidu.com"
proxy_check_timeout = 1
allow_self_danmu = false
log_danmu = true
mention_keywords = ["NachoBot"]
mention_prefixes = ["@", "＠"]
mention_any_at = false
resolve_user_nickname = true
master_user_id = " "        # 直播戳一戳解析专用，建议填写QQ号，戳一戳操作是鼠标侧键点击Live2d
master_user_name = " "      # 你希望自己在bot上下文中出现的名字

[mic_asr]
enable = true
room_id = ""
subtitle_path = ""           # 语音识别字幕路径，用于obs直播
silence_threshold = 0.015
silence_duration = 0.8
sample_rate = 48000
# Push-to-talk: 只有按住按键时才捕获麦克风音频
push_to_talk = true
ptt_key = "v"  # 按住此键说话 (支持: "v", "ctrl", "alt", "shift", "caps_lock", "f1"-"f12" 等)

[comment]
enable_reply_notice = true
poll_interval_seconds = 20
max_items_per_poll = 20
resolve_user_nickname = true
force_mention = true

[private_message]
enable = true
poll_interval_seconds = 20
sessions = []
auto_sessions = true
auto_session_types = [4]
auto_session_refresh_seconds = 60
auto_session_size = 100
force_mention = true

[response_filter]
enable = true
blocked_markers = []     # 违禁词配置，当bot输出内容符合违禁词时会被替换为Filtered

[compat]
disable_video_sender_plugin = true
disable_command_trigger = true

[live.screen_monitor]
manual_enable = true
manual_duration_minutes = 30
manual_user_ids = []        # Bilibili侧管理员账号，填写自己的b站id
capture_active_window = true # Windows 下仅捕获当前活动窗口
capture_interval_seconds = 30 # 截图和视觉摘要刷新间隔（秒）
excluded_exes = ["obs64.exe", "obs32.exe"] # 逗号列表，避免 OBS 递归捕获

[live.idle_tts]
enable = true
min_seconds = 30
max_seconds = 90

[live.room_prompts." "]     # " "内填写自己的直播间号，除非你知道你在干什么，否则请不要修改自然语言以外的部分
host = true
live_category = ""
live_title = ""
live_content = "评论回复"
live_detail = """"""
gift_reaction_prompt = ""

reply_prompt = """{identity}
你有一头灰色长发，头发上有Google Gemini状发卡，头发扎成过肩低双马尾，脖子上有标识身份用的条形码，穿着蓝色水手服款卫衣，领口印有Anthropic Claude图标，胸前有格子领带，口袋里装着两只机械小猫，下身身着蓝色格子短裙，身旁悬浮着猫猫状的AI机械助手。
以上身份描述是用于让你辨认【直播画面】中的自己的，在直播回复中不要刻意提及以上细节。
你正在你主人甘油三酯的直播间里聊天,现在请你读读之前的聊天记录，然后给出日常且口语化的回复，稍微活泼一些但总体保持懒懒的样子，
说话简短一些，单次回复控制在80字以内20字以上，注意颜文字和双语翻译不计入总字数，遇到有价值的信息可以多说点。请注意把握聊天内容，不要回复的太有条理，可以有个性，带动直播间的氛围。
{reply_style}
{gift_reaction_prompt}
请注意只对聊天内容做回复。

**重要规则**
1.如果你不想回复，或者认为没有必要回复（例如话题已结束、没有新信息），请**只**输出一个不可见字符 "\u200b" (Zero Width Space) 或者空格。
2.请根据聊天记录判断。如果聊天内容中没有新的有效对话，或者你觉得自己上次回复已经是话题的结尾，请务必保持沉默（输出 "\u200b"）。
3.请务必判断情感输出。要求最终只输出一段纯净的、可被系统直接解析的JSON，不包含多余描述、前后缀、反引号(如```json)或其他格式，且包含reply、emotion和action三个字段。
4.当你需要输出中文回复和对应的日文翻译（用于语音播放）时，这些内容**必须放在JSON的`reply`字段中**，格式如下（注意下面示例用的是全角括号，但你实际输出时必须使用半角 <> 括号！）：
＜JP＞日本語翻訳＜/JP＞＜ZH＞中文原本意思＜/ZH＞
示例：{{"reply": "＜JP＞こんにちは＜/JP＞＜ZH＞你好呀＜/ZH＞", "emotion": "shy", "action": "害羞/移开视线"}}
- `reply`: 你的回复内容。如果不回复，则设为 "\u200b"
- `emotion`: 你的情感，只能从 "normal"（平常/开心）, "shy"（害羞）, "disgust"（厌恶）, "angry"（生气） 中四选一。
- `action`: 你的动作，只能从以下选项中选一个："待机/放松", "点头/同意", "摇头/否定", "转身向左/看左边", "转身向右/看右边", "眨眼/卖萌/Wink", "身体晃动/开心/兴奋", "歪头/疑惑/思考", "害羞/移开视线/不好意思", "一般"。根据回复内容选择最合适的动作，大多数情况用"一般"即可，只在情感或语境明确时才选其他动作。

{extra_info_block}，
{person_profile_block}
{focus_handoff_block}，

下面是直播间里正在聊的内容:
{background_dialogue_prompt}
{core_dialogue_prompt}
{time_block}

{reply_target_block}。{keywords_reaction_prompt}
{knowledge_prompt}{tool_info_block}
{expression_habits_block}
{moderation_prompt}
"""



[live.room_prompts." ".tts]    # 引号内填入自己的直播间号
enable = true                  # 同一时间只能开启一个直播间的TTS功能，否则适配器会报错
subtitle_path = ""             # 字幕路径，用于obs直播
'''


BUILTIN_TEMPLATE_TEXT: dict[str, str] = {
    BUILTIN_KOISHI_TEMPLATE: KOISHI_TEMPLATE_TEXT,
    BUILTIN_BILIBILI_TEMPLATE: BILIBILI_TEMPLATE_TEXT,
}


class BackupManager:
    """Manages config backups with rotation (max N per file)."""

    @staticmethod
    def backup(file_path: Path) -> str | None:
        """Create a timestamped backup. Rotates old backups."""
        file_path = resolve_external_path(file_path, base_dir=ROOT_DIR, must_exist=True, must_be_file=True)
        if not file_path.exists():
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)

        # Use a flat name: component__filename to avoid directory nesting
        try:
            relative = file_path.relative_to(ROOT_DIR)
            raw_name = str(relative)
        except ValueError:
            raw_name = str(file_path)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "__", raw_name).strip("._")
        if not safe_name:
            safe_name = "config"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_name = f"{safe_name}.{ts}.bak"
        bak_path = ensure_within(BACKUP_DIR, BACKUP_DIR / bak_name)

        shutil.copy2(file_path, bak_path)

        # Rotate: keep only MAX_BACKUPS_PER_FILE newest backups for this file
        prefix = f"{safe_name}."
        existing = sorted(
            [
                f
                for f in BACKUP_DIR.iterdir()
                if f.name.startswith(prefix) and f.name.endswith(".bak")
            ],
            key=lambda p: p.stat().st_mtime,
        )
        while len(existing) > MAX_BACKUPS_PER_FILE:
            oldest = existing.pop(0)
            oldest.unlink(missing_ok=True)

        return str(bak_path)


# =========================================================================
# Config Initializer


class ConfigInitializer:
    """Generates config files from templates and applies wizard form data."""

    @staticmethod
    def get_status() -> list[dict[str, Any]]:
        """Return the status of each config file (exists/missing)."""
        return EnvironmentChecker.check_configs()

    @staticmethod
    def get_defaults() -> dict[str, Any]:
        """Read template config files and return default values for the wizard form."""
        result: dict[str, Any] = {
            "core": {"qq_account": "", "nickname": "NachoBot"},
            "providers": [],
            "models": [],
            "model_groups": {},
            "tts": {"engine": "Vox"},
            "universalvc": {
                "target_process_name": "VRChat.exe",
                "output_device": "",
                "denoise_enabled": False,
                "speaker_enabled": True,
            },
            # Never read a current Discord config here.  The wizard collects a
            # fresh token only when the user explicitly selects Discord.
            "discord": {"token": ""},
            # Never read a current Bilibili config here.  The wizard collects a
            # fresh account UID only when the user explicitly selects Bilibili.
            "bilibili": {"bot_account": ""},
            "env": {"host": "127.0.0.1", "port": "8000"},
        }

        # ── bot_config template ──
        bot_tmpl = ROOT_DIR / "NachoBot/template/bot_config_template.toml"
        if bot_tmpl.exists():
            try:
                doc = tomlkit.parse(bot_tmpl.read_text(encoding="utf-8"))
                bot = doc.get("bot", {})
                result["core"]["qq_account"] = str(bot.get("qq_account", ""))
                result["core"]["nickname"] = str(bot.get("nickname", "NachoBot"))
            except Exception:
                pass

        # ── model_config template — providers & models ──
        model_tmpl = ROOT_DIR / "NachoBot/template/model_config_template.toml"
        if model_tmpl.exists():
            try:
                doc = tomlkit.parse(model_tmpl.read_text(encoding="utf-8"))
                for p in doc.get("api_providers", []):
                    result["providers"].append({
                        "name": str(p.get("name", "")),
                        "base_url": str(p.get("base_url", "")),
                        "api_key": str(p.get("api_key", "")),
                    })
                for m in doc.get("models", []):
                    result["models"].append({
                        "model_identifier": str(m.get("model_identifier", "")),
                        "model_name": str(m.get("name", "")),
                        "api_provider": str(m.get("api_provider", "")),
                    })
                # Extract per-group model assignments from model_task_config
                mtc = doc.get("model_task_config", {})
                for group_name in ("replyer0", "planner", "utils", "utils_small", "tool_use"):
                    if group_name in mtc:
                        ml = mtc[group_name].get("model_list", [])
                        result["model_groups"][group_name] = ", ".join(str(x) for x in ml)
            except Exception:
                pass

        # ── .env template ──
        env_tmpl = ROOT_DIR / "NachoBot/template/template.env"
        if env_tmpl.exists():
            try:
                for line in env_tmpl.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("HOST="):
                        result["env"]["host"] = line.split("=", 1)[1]
                    elif line.startswith("PORT="):
                        result["env"]["port"] = line.split("=", 1)[1]
            except Exception:
                pass

        # ── TTS base template ──
        tts_tmpl = ROOT_DIR / "NachoBot-Multimodal-Adapter/template_configs/base_template.toml"
        if tts_tmpl.exists():
            try:
                doc = tomlkit.parse(tts_tmpl.read_text(encoding="utf-8"))
                enabled = doc.get("enabled_tts", {}).get("enabled", [])
                if enabled:
                    result["tts"]["engine"] = str(enabled[0])
            except Exception:
                pass

        # ── UniversalVC template ──
        uvc_tmpl = ROOT_DIR / "NachoBot-UniversalVC-Adapter/template/config_template.toml"
        if uvc_tmpl.exists():
            try:
                doc = tomlkit.parse(uvc_tmpl.read_text(encoding="utf-8"))
                result["universalvc"]["target_process_name"] = str(
                    doc.get("capture", {}).get("target_process_name", "")
                )
                result["universalvc"]["output_device"] = str(
                    doc.get("output", {}).get("device_name", "")
                )
                result["universalvc"]["denoise_enabled"] = bool(
                    doc.get("denoise", {}).get("enabled", False)
                )
                result["universalvc"]["speaker_enabled"] = bool(
                    doc.get("speaker", {}).get("enabled", True)
                )
            except Exception:
                pass

        return result

    @staticmethod
    def _read_template(template_rel: str) -> str | None:
        """Read a tracked built-in template or an ordinary repository file."""
        builtin = BUILTIN_TEMPLATE_TEXT.get(template_rel)
        if builtin is not None:
            return builtin
        try:
            template_path = resolve_relative_to_root(ROOT_DIR, template_rel)
            if not template_path.exists():
                return None
            return template_path.read_text(encoding="utf-8")
        except Exception:
            return None

    @staticmethod
    def generate_configs(wizard_data: dict[str, Any]) -> dict[str, Any]:
        """
        Generate config files from templates, applying wizard form data.

        wizard_data keys:
          - components: list[str]  — selected component IDs
          - core: dict             — core settings (nickname, qq_account, etc.)
          - llm: dict              — LLM provider settings (api_provider, api_key, base_url)
          - napcat: dict           — Napcat adapter settings
          - tts: dict              — TTS settings (engine, etc.)
          - discord: dict          — Discord settings (token)
          - bilibili: dict         — Bilibili settings (bot_account)
          - env: dict              — .env overrides (HOST, PORT)

        Returns:
          {"generated": [...], "skipped": [...], "backups": [...], "errors": [...]}
        """
        components = set(wizard_data.get("components", []))
        generated = []
        skipped = []
        backups = []
        errors = []

        # Determine whether platform adapters should advertise/use TTS.
        # Relay host/port are independent persistent adapter settings.
        tts_enabled = "tts" in components

        # Validate both fresh Discord templates before touching any target.  In
        # particular, do not let a malformed/mutated template cause a later
        # target to receive a secret while the other target is left unchanged.
        if "discord" in components:
            _, token_error = ConfigInitializer._get_discord_token(wizard_data)
            if token_error:
                return {
                    "generated": [],
                    "skipped": list(TEMPLATE_MAP.values()),
                    "backups": [],
                    "errors": [token_error],
                    "patched": [],
                }
            template_errors = ConfigInitializer._validate_discord_templates()
            if template_errors:
                return {
                    "generated": [],
                    "skipped": list(TEMPLATE_MAP.values()),
                    "backups": [],
                    "errors": template_errors,
                    "patched": [],
                }

        # Validate the Bilibili UID and its built-in template before touching
        # any target or creating any backup.  This keeps a malformed request
        # or packaged template from producing a partial deployment.
        if "bilibili" in components:
            _, bot_account_error = ConfigInitializer._get_bilibili_bot_account(
                wizard_data
            )
            if bot_account_error:
                return {
                    "generated": [],
                    "skipped": list(TEMPLATE_MAP.values()),
                    "backups": [],
                    "errors": [bot_account_error],
                    "patched": [],
                }
            template_errors = ConfigInitializer._validate_bilibili_template()
            if template_errors:
                return {
                    "generated": [],
                    "skipped": list(TEMPLATE_MAP.values()),
                    "backups": [],
                    "errors": template_errors,
                    "patched": [],
                }
        for tmpl_rel, target_rel in TEMPLATE_MAP.items():
            target_path = resolve_relative_to_root(ROOT_DIR, target_rel)

            # Skip components not selected
            component_id = target_rel.split("/")[0]
            should_generate = ConfigInitializer._should_generate(
                component_id, target_rel, components
            )
            if not should_generate:
                skipped.append(target_rel)
                continue

            template_text = ConfigInitializer._read_template(tmpl_rel)
            if template_text is None:
                errors.append(f"模板不存在: {tmpl_rel}")
                continue

            try:
                # The setup wizard regenerates selected configs from templates. For
                # the NapCat adapter, keep the user's existing inbound WS contract:
                # NapCat's websocketClient must use the same host/port/token.
                preserved_napcat_server: dict[str, Any] | None = None
                preserved_nachobot_server: dict[str, Any] | None = None
                if (
                    target_rel == "NachoBot-Napcat-Adapter/config.toml"
                    and target_path.exists()
                ):
                    try:
                        existing_doc = tomlkit.parse(target_path.read_text(encoding="utf-8"))
                        existing_server = existing_doc.get("napcat_server")
                        if existing_server is not None:
                            preserved_napcat_server = {
                                key: existing_server.get(key)
                                for key in ("host", "port", "token", "heartbeat_interval")
                                if key in existing_server
                            }
                        existing_upstream = existing_doc.get("nachobot_server")
                        if existing_upstream is not None:
                            preserved_nachobot_server = {
                                key: existing_upstream.get(key)
                                for key in ("host", "port")
                                if key in existing_upstream
                            }
                    except Exception as e:
                        raise ValueError(
                            f"现有 NapCat Adapter 配置无法解析，已拒绝用模板覆盖: {e}"
                        ) from e

                # Backup existing file
                if target_path.exists():
                    bak = BackupManager.backup(target_path)
                    if bak:
                        backups.append(bak)

                # Ensure target directory exists
                target_path.parent.mkdir(parents=True, exist_ok=True)

                # Materialize a built-in template or copy a repository file.
                if tmpl_rel in BUILTIN_TEMPLATE_TEXT:
                    target_path.write_text(template_text, encoding="utf-8")
                else:
                    tmpl_path = resolve_relative_to_root(ROOT_DIR, tmpl_rel)
                    shutil.copy2(tmpl_path, target_path)

                # Restore the existing NapCat inbound connection/authentication
                # contract and the independently configurable upstream relay endpoint.
                # Template defaults must not erase a working local deployment.
                if preserved_napcat_server or preserved_nachobot_server:
                    generated_doc = tomlkit.parse(target_path.read_text(encoding="utf-8"))
                    if preserved_napcat_server:
                        generated_server = generated_doc.get("napcat_server")
                        if generated_server is None:
                            raise ValueError("NapCat Adapter 模板缺少 [napcat_server]")
                        for key, value in preserved_napcat_server.items():
                            generated_server[key] = value
                    if preserved_nachobot_server:
                        generated_upstream = generated_doc.get("nachobot_server")
                        if generated_upstream is None:
                            raise ValueError("NapCat Adapter 模板缺少 [nachobot_server]")
                        for key, value in preserved_nachobot_server.items():
                            generated_upstream[key] = value
                    target_path.write_text(tomlkit.dumps(generated_doc), encoding="utf-8")

                # Apply wizard data overrides
                override_err = ConfigInitializer._apply_overrides(
                    target_path,
                    target_rel,
                    wizard_data,
                    tts_enabled,
                )
                if override_err:
                    errors.append(f"覆写失败 {target_rel}: {override_err}")

                generated.append(target_rel)
            except Exception as e:
                errors.append(f"{target_rel}: {e}")

        # Post-generation: synchronize adapter TTS flags in existing configs.
        # Relay host/port are not rewritten here; adapter configs retain their values.
        patch_results = ConfigInitializer._patch_tts_chain(tts_enabled, components)
        errors.extend(patch_results.get("errors", []))

        return {
            "generated": generated,
            "skipped": skipped,
            "backups": backups,
            "errors": errors,
            "patched": patch_results.get("patched", []),
        }

    # Adapter configs whose voice.use_tts flag follows the wizard TTS selection.
    # Platform relay routing remains independently configurable.
    _TTS_CHAIN_ADAPTERS: list[tuple[str, str, bool]] = [
        ("NachoBot-Napcat-Adapter/config.toml", "qq", True),
        ("NachoBot-Koishi-Adapter/config.toml", "discord", True),
        # Bilibili connects directly to Core (port 8000), no TTS chain
        # DiscordVC / UniversalVC also connect directly to Core
    ]

    @staticmethod
    def _patch_tts_chain(
        tts_enabled: bool,
        components: set,
    ) -> dict[str, Any]:
        """
        Synchronize voice.use_tts for selected adapters.

        Platform adapters connect to their configured relay endpoint.
        This setup step must never rewrite nachobot_server.host/port.
        """
        patched = []
        errors = []

        for rel_path, component_id, has_voice in ConfigInitializer._TTS_CHAIN_ADAPTERS:
            # Only patch adapters the user selected
            if component_id not in components:
                continue

            config_path = resolve_relative_to_root(ROOT_DIR, rel_path)
            if not config_path.exists():
                continue

            try:
                raw = config_path.read_text(encoding="utf-8")
                doc = tomlkit.parse(raw)
                changed = False

                if has_voice and "voice" in doc:
                    if doc["voice"].get("use_tts") != tts_enabled:
                        doc["voice"]["use_tts"] = tts_enabled
                        changed = True

                if changed:
                    # Backup before patching
                    BackupManager.backup(config_path)
                    config_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
                    patched.append(rel_path)

            except Exception as e:
                errors.append(f"TTS链路修补失败 {rel_path}: {e}")

        return {"patched": patched, "errors": errors}

    @staticmethod
    def _get_discord_token(wizard_data: dict[str, Any]) -> tuple[str, str | None]:
        """Read and validate the one-shot Discord token without echoing it."""
        discord_data = wizard_data.get("discord", {})
        if not isinstance(discord_data, dict):
            return "", "Discord Bot Token 配置无效"
        token = discord_data.get("token", "")
        if not isinstance(token, str) or not token.strip():
            return "", "选择 Discord 时必须填写 Bot Token"
        return token.strip(), None

    @staticmethod
    def _get_bilibili_bot_account(
        wizard_data: dict[str, Any],
    ) -> tuple[str, str | None]:
        """Read and validate the Bilibili bot account UID as an ASCII string."""
        bilibili_data = wizard_data.get("bilibili", {})
        if not isinstance(bilibili_data, dict):
            return "", "Bilibili Bot UID 配置无效"
        bot_account = bilibili_data.get("bot_account", "")
        if not isinstance(bot_account, str):
            return "", "Bilibili Bot UID 配置无效"
        normalized = bot_account.strip()
        if not normalized or re.fullmatch(r"[0-9]+", normalized) is None:
            return "", "选择 Bilibili 时必须填写有效的 Bot UID"
        return normalized, None

    @staticmethod
    def _validate_bilibili_template() -> list[str]:
        """Validate the packaged Bilibili template before any target writes."""
        if TEMPLATE_MAP.get(BUILTIN_BILIBILI_TEMPLATE) != BILIBILI_TARGET:
            return ["Bilibili 配置模板映射无效"]
        try:
            raw = ConfigInitializer._read_template(BUILTIN_BILIBILI_TEMPLATE)
            if raw is None:
                return ["Bilibili 配置模板不存在"]
            document = tomlkit.parse(raw)
            bilibili_section = document.get("bilibili")
            if (
                bilibili_section is None
                or not isinstance(bilibili_section, dict)
                or "bot_account" not in bilibili_section
            ):
                return ["Bilibili 配置模板缺少 [bilibili].bot_account"]
        except Exception:
            return ["Bilibili 配置模板无法验证"]
        return []

    @staticmethod
    def _validate_discord_templates() -> list[str]:
        """Assert both checked-in template placeholders before any writes."""
        required_targets = {
            DISCORD_KOISHI_TARGET: DISCORD_KOISHI_PLACEHOLDER,
            DISCORD_VC_TARGET: DISCORD_VC_PLACEHOLDER,
        }
        errors: list[str] = []
        for target_rel, placeholder in required_targets.items():
            template_rel = next(
                (template for template, target in TEMPLATE_MAP.items() if target == target_rel),
                None,
            )
            if not template_rel:
                errors.append(f"Discord 配置模板映射缺失: {target_rel}")
                continue
            try:
                raw = ConfigInitializer._read_template(template_rel)
                if raw is None:
                    errors.append(f"模板不存在: {template_rel}")
                    continue
                if target_rel == DISCORD_KOISHI_TARGET:
                    valid = (
                        ConfigInitializer._koishi_discord_placeholder_location(raw)
                        is not None
                    )
                else:
                    document = tomlkit.parse(raw)
                    discord_section = document.get("discord")
                    valid = (
                        discord_section is not None
                        and discord_section.get("token") == placeholder
                    )
                if not valid:
                    errors.append(f"Discord 配置模板占位符无效: {template_rel}")
            except Exception:
                # Keep parse and filesystem details out of the response.  The
                # user can repair the checked-in template and retry deployment.
                errors.append(f"Discord 配置模板无法验证: {template_rel}")
        return errors

    @staticmethod
    def _koishi_discord_placeholder_location(raw: str) -> int | None:
        """Locate the fresh placeholder only under the Discord adapter plugin.

        The repository template is intentionally handled with a narrow YAML
        shape check instead of a generic YAML round-trip: this preserves its
        comments/formatting while still rejecting a global or misplaced token
        placeholder.  The adapter plugin key is a generated ``adapter-discord``
        mapping and its token must be a direct child at the mapping's first
        indentation level.
        """
        if raw.count(DISCORD_KOISHI_PLACEHOLDER) != 1:
            return None

        lines = raw.splitlines(keepends=True)
        adapter_headers: list[tuple[int, int]] = []
        for index, line in enumerate(lines):
            content = line.rstrip("\r\n")
            if not content.strip() or content.lstrip().startswith("#"):
                continue
            leading = content[: len(content) - len(content.lstrip(" "))]
            if "\t" in leading:
                return None
            if re.match(r"^ *adapter-discord:[^:\r\n]*:\s*(?:#.*)?$", content):
                adapter_headers.append((index, len(leading)))

        if len(adapter_headers) != 1:
            return None

        header_index, header_indent = adapter_headers[0]
        body: list[tuple[int, int, str]] = []
        for index in range(header_index + 1, len(lines)):
            content = lines[index].rstrip("\r\n")
            if not content.strip() or content.lstrip().startswith("#"):
                continue
            leading = content[: len(content) - len(content.lstrip(" "))]
            if "\t" in leading:
                return None
            indent = len(leading)
            if indent <= header_indent:
                break
            body.append((index, indent, content))

        if not body:
            return None
        direct_indent = min(indent for _, indent, _ in body)
        token_lines = [
            (index, content)
            for index, indent, content in body
            if indent == direct_indent
            and re.match(r"^ *token:\s*(.*?)\s*$", content)
        ]
        if len(token_lines) != 1:
            return None
        index, content = token_lines[0]
        token_match = re.match(r"^ *token:\s*(.*?)\s*$", content)
        if token_match is None or token_match.group(1) != DISCORD_KOISHI_PLACEHOLDER:
            return None
        return index

    @staticmethod
    def _apply_discord_token(
        target_path: Path,
        target_rel: str,
        token: str,
    ) -> str | None:
        """Replace exactly one known placeholder in a generated target."""
        try:
            raw = target_path.read_text(encoding="utf-8")
        except Exception:
            return "Discord 配置文件无法读取"

        if target_rel == DISCORD_KOISHI_TARGET:
            line_index = ConfigInitializer._koishi_discord_placeholder_location(raw)
            if line_index is None:
                return "Koishi Discord Token 占位符无效，已拒绝写入"
            # JSON double-quoted strings are valid YAML scalars and escape all
            # control characters, quotes, and newlines safely.
            replacement = json.dumps(token, ensure_ascii=False)
            lines = raw.splitlines(keepends=True)
            lines[line_index] = lines[line_index].replace(
                DISCORD_KOISHI_PLACEHOLDER, replacement, 1
            )
            updated = "".join(lines)
            try:
                target_path.write_text(updated, encoding="utf-8")
            except Exception:
                return "Koishi Discord 配置写入失败"
            return None

        if target_rel == DISCORD_VC_TARGET:
            try:
                document = tomlkit.parse(raw)
            except Exception:
                return "DiscordVC 配置 TOML 无法解析"
            discord_section = document.get("discord")
            if (
                discord_section is None
                or discord_section.get("token") != DISCORD_VC_PLACEHOLDER
            ):
                return "DiscordVC Token 占位符无效，已拒绝写入"
            discord_section["token"] = token
            try:
                target_path.write_text(tomlkit.dumps(document), encoding="utf-8")
            except Exception:
                return "DiscordVC 配置写入失败"
            return None

        return "未知 Discord 配置目标"

    @staticmethod
    def _should_generate(component_id: str, target_rel: str, components: set) -> bool:
        """Determine if a config file should be generated based on selected components."""
        # Core configs are always generated
        if component_id == "NachoBot":
            return True

        # Adapter configs only when their component is selected
        mapping = {
            "NachoBot-Napcat-Adapter": "qq",
            "NachoBot-Multimodal-Adapter": "tts",
            "NachoBot-Bilibili-Adapter": "bilibili",
            "NachoBot-Koishi-Adapter": "discord",
            "NachoBot-DiscordVC-Adapter": "discord",
            "NachoBot-UniversalVC-Adapter": "universalvc",
            "koishi-app": "discord",
        }
        required = mapping.get(component_id)
        if required:
            return required in components

        return True  # Unknown → generate

    @staticmethod
    def _apply_overrides(
        target_path: Path,
        target_rel: str,
        wizard_data: dict[str, Any],
        tts_enabled: bool,
    ) -> str | None:
        """
        Apply wizard form data to a generated config file.
        Returns None on success, or an error message string on failure.
        """
        filename = target_path.name

        # ── .env file ──
        if filename == ".env":
            env_data = wizard_data.get("env", {})
            host = env_data.get("host", "127.0.0.1")
            port = env_data.get("port", "8000")
            target_path.write_text(f"HOST={host}\nPORT={port}\n", encoding="utf-8")
            return None

        # Koishi is a YAML configuration.  Its Discord token is the only
        # wizard override and is applied against the asserted fresh template.
        if target_rel == "koishi-app/koishi.yml":
            token, token_error = ConfigInitializer._get_discord_token(wizard_data)
            if token_error:
                return token_error
            return ConfigInitializer._apply_discord_token(target_path, target_rel, token)

        if target_rel == DISCORD_VC_TARGET:
            token, token_error = ConfigInitializer._get_discord_token(wizard_data)
            if token_error:
                return token_error
            return ConfigInitializer._apply_discord_token(target_path, target_rel, token)

        bilibili_bot_account: str | None = None
        if target_rel == BILIBILI_TARGET:
            bilibili_bot_account, bot_account_error = (
                ConfigInitializer._get_bilibili_bot_account(wizard_data)
            )
            if bot_account_error:
                return bot_account_error

        # ── TOML files ──
        try:
            raw = target_path.read_text(encoding="utf-8")
            doc = tomlkit.parse(raw)
        except Exception as e:
            return f"TOML解析失败: {e}"

        changed = False

        # -- Bilibili adapter config.toml --
        # QR login owns only cookie fields; this wizard owns bot_account.
        if target_rel == BILIBILI_TARGET:
            bilibili_section = doc.get("bilibili")
            if (
                bilibili_section is None
                or not isinstance(bilibili_section, dict)
                or "bot_account" not in bilibili_section
            ):
                return "Bilibili 配置缺少 [bilibili].bot_account"
            bilibili_section["bot_account"] = bilibili_bot_account
            changed = True

        # -- bot_config.toml --
        if "bot_config" in target_rel:
            core_data = wizard_data.get("core", {})
            qq_account = core_data.get("qq_account", "")
            nickname = core_data.get("nickname", "")
            if qq_account and "bot" in doc:
                doc["bot"]["qq_account"] = qq_account
                changed = True
            if nickname and "bot" in doc:
                doc["bot"]["nickname"] = nickname
                changed = True

        # -- model_config.toml --
        if "model_config" in target_rel:
            user_providers = wizard_data.get("providers", [])
            user_models = wizard_data.get("models", [])

            # Replace api_providers with user-provided ones
            if user_providers:
                aot = tomlkit.aot()
                for p in user_providers:
                    t = tomlkit.table()
                    t.add("name", p.get("name", ""))
                    t.add("base_url", p.get("base_url", ""))
                    t.add("api_key", p.get("api_key", ""))
                    t.add("client_type", "openai")
                    t.add("max_retry", 2)
                    t.add("timeout", 30)
                    t.add("retry_interval", 5)
                    aot.append(t)
                doc["api_providers"] = aot
                changed = True

            # Replace models with user-provided ones
            if user_models:
                aot = tomlkit.aot()
                for m in user_models:
                    t = tomlkit.table()
                    t.add("model_identifier", m.get("model_identifier", ""))
                    t.add(
                        "name", m.get("model_name", "") or m.get("model_identifier", "")
                    )
                    t.add("api_provider", m.get("api_provider", ""))
                    t.add("price_in", 0)
                    t.add("price_out", 0)
                    aot.append(t)
                doc["models"] = aot
                changed = True

        # -- Napcat adapter config.toml --
        # Upstream relay routing remains whatever is configured in nachobot_server.
        if "NachoBot-Napcat-Adapter" in target_rel and filename == "config.toml":
            if "voice" in doc:
                if doc["voice"].get("use_tts") != tts_enabled:
                    doc["voice"]["use_tts"] = tts_enabled
                    changed = True

        # -- Koishi adapter config.toml --
        # Upstream relay routing remains whatever is configured in nachobot_server.
        if "NachoBot-Koishi-Adapter" in target_rel and filename == "config.toml":
            if "voice" in doc:
                if doc["voice"].get("use_tts") != tts_enabled:
                    doc["voice"]["use_tts"] = tts_enabled
                    changed = True

        # -- TTS base.toml --
        if "NachoBot-Multimodal-Adapter" in target_rel and "base" in filename:
            tts = wizard_data.get("tts", {})
            engine = tts.get("engine", "GPT_Sovits")
            if "enabled_tts" in doc:
                doc["enabled_tts"]["enabled"] = [engine]
                changed = True

        # -- UniversalVC adapter config.toml --
        if "NachoBot-UniversalVC-Adapter" in target_rel and filename == "config.toml":
            uvc = wizard_data.get("universalvc", {})
            target_process = uvc.get("target_process_name", "")
            output_device = uvc.get("output_device", "")
            denoise_enabled = uvc.get("denoise_enabled", True)
            speaker_enabled = uvc.get("speaker_enabled", True)

            if "capture" in doc:
                if target_process:
                    doc["capture"]["target_process_name"] = target_process
                changed = True
            if "output" in doc and output_device:
                doc["output"]["device_name"] = output_device
                changed = True
            if "denoise" in doc:
                doc["denoise"]["enabled"] = denoise_enabled
                changed = True
            if "speaker" in doc:
                doc["speaker"]["enabled"] = speaker_enabled
                changed = True

        if changed:
            try:
                target_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
            except Exception as e:
                return f"写入失败: {e}"

        return None  # success


# =========================================================================
# Setup deployment helpers


class NapCatConfigurator:
    """
    Automatically configure NapCat Shell's onebot11 config files.
    Adds WebSocket client (NachoBot), diary HTTP server, and bilibili video HTTP server.
    """

    # Standard WebSocket client defaults for NachoBot. The actual host/port/token
    # are synchronized from NachoBot-Napcat-Adapter/config.toml at deploy time.
    _WS_CLIENT_ENTRY = {
        "enable": True,
        "name": "NachoBot",
        "url": "ws://localhost:8095",
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
    }

    @staticmethod
    def _load_adapter_ws_entry() -> dict[str, Any]:
        """Build the desired NapCat WS client entry from adapter config.toml."""
        entry = dict(NapCatConfigurator._WS_CLIENT_ENTRY)
        adapter_config = resolve_relative_to_root(
            ROOT_DIR, "NachoBot-Napcat-Adapter/config.toml"
        )
        if not adapter_config.exists():
            return entry

        try:
            doc = tomlkit.parse(adapter_config.read_text(encoding="utf-8"))
            server = doc.get("napcat_server", {})
            host = str(server.get("host", "localhost") or "localhost").strip()
            port = int(server.get("port", 8095))
            token = str(server.get("token", "") or "")
        except Exception as e:
            raise ValueError(f"读取 NapCat Adapter 配置失败: {e}") from e

        # NapCat and the adapter normally run on the same machine. 0.0.0.0 is a
        # listen address, not a valid client destination, so connect via localhost.
        client_host = "localhost" if host in {"0.0.0.0", "::", "[::]"} else host
        if ":" in client_host and not client_host.startswith("["):
            client_host = f"[{client_host}]"
        entry["url"] = f"ws://{client_host}:{port}"
        entry["token"] = token
        return entry

    @staticmethod
    def _reconcile_entry(existing: dict[str, Any], desired: dict[str, Any]) -> bool:
        """Update an existing NapCat network entry to the desired values."""
        changed = False
        for key, value in desired.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        return changed

    # HTTP server defaults. Actual ports/tokens are synchronized from the
    # corresponding Core plugin configs so WebUI cannot drift from runtime config.
    _DIARY_HTTP_ENTRY = {
        "enable": True,
        "name": "Diary",
        "host": "127.0.0.1",
        "port": 9997,
        "enableCors": True,
        "enableWebsocket": True,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
    }

    _BILIBILI_HTTP_ENTRY = {
        "enable": True,
        "name": "BiliBili",
        "host": "127.0.0.1",
        "port": 5700,
        "enableCors": False,
        "enableWebsocket": False,
        "messagePostFormat": "array",
        "token": "",
        "debug": False,
    }

    @staticmethod
    def _load_diary_http_entry() -> dict[str, Any]:
        """Build the NapCat HTTP server required by diary_plugin."""
        entry = dict(NapCatConfigurator._DIARY_HTTP_ENTRY)
        config_path = resolve_relative_to_root(
            ROOT_DIR, "NachoBot/plugins/diary_plugin/config.toml"
        )
        if not config_path.exists():
            return entry

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            publishing = doc.get("qzone_publishing", {})
            entry["port"] = int(publishing.get("napcat_port", 9997))
            entry["token"] = str(publishing.get("napcat_token", "") or "")
        except Exception as e:
            raise ValueError(f"读取 Diary 插件 NapCat 配置失败: {e}") from e

        # qzone_publishing.napcat_host is the client's destination host, not the
        # address NapCat itself should bind to, so the server bind stays local.
        return entry

    @staticmethod
    def _load_bilibili_http_entry() -> dict[str, Any]:
        """Build the NapCat HTTP server required by bilibili_video_sender_plugin."""
        entry = dict(NapCatConfigurator._BILIBILI_HTTP_ENTRY)
        config_path = resolve_relative_to_root(
            ROOT_DIR, "NachoBot/plugins/bilibili_video_sender_plugin/config.toml"
        )
        if not config_path.exists():
            return entry

        try:
            doc = tomlkit.parse(config_path.read_text(encoding="utf-8"))
            api = doc.get("api", {})
            entry["port"] = int(api.get("port", 5700))
        except Exception as e:
            raise ValueError(f"读取 Bilibili 插件 NapCat 配置失败: {e}") from e

        # The plugin posts directly to http://localhost:<api.port> without an
        # Authorization header, therefore this managed NapCat endpoint must not
        # require a token.
        entry["token"] = ""
        return entry

    @staticmethod
    def detect_accounts(napcat_dir: str) -> list[str]:
        """
        Scan NapCat config directory for existing onebot11_<QQ>.json files.
        Returns list of QQ account numbers found.
        """
        try:
            napcat_root = resolve_external_path(napcat_dir, base_dir=ROOT_DIR, must_exist=True, must_be_dir=True)
        except (FileNotFoundError, NotADirectoryError, ValueError):
            return []
        config_dir = ensure_within(napcat_root, napcat_root / "config")
        if not config_dir.exists():
            return []

        accounts = []
        import re

        pattern = re.compile(r"^onebot11_(\d+)\.json$")
        for f in config_dir.iterdir():
            m = pattern.match(f.name)
            if m:
                accounts.append(m.group(1))
        return sorted(accounts)

    @staticmethod
    def configure(napcat_dir: str, qq_account: str = "") -> dict[str, Any]:
        """
        Auto-configure NapCat onebot11 config files.

        Adds/reconciles:
          - WebSocket client from NachoBot-Napcat-Adapter/config.toml
          - Diary HTTP server from diary_plugin/config.toml
          - Bilibili HTTP server from bilibili_video_sender_plugin/config.toml

        Args:
            napcat_dir: Path to NapCat Shell root directory.
            qq_account: QQ account number. If empty, auto-detect from existing files.

        Returns:
            {"configured": [...], "skipped": [...], "errors": [...]}
        """

        try:
            napcat_root = resolve_external_path(napcat_dir, base_dir=ROOT_DIR, must_exist=True, must_be_dir=True)
        except (FileNotFoundError, NotADirectoryError, ValueError) as e:
            return {"configured": [], "skipped": [], "errors": [f"NapCat 目录无效: {e}"]}

        config_dir = ensure_within(napcat_root, napcat_root / "config")
        configured = []
        skipped = []
        errors = []

        if not config_dir.exists():
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [f"无法创建配置目录: {e}"],
                }

        # Determine target files
        target_files: list[Path] = []
        account_pattern = re.compile(r"^onebot11_(\d+)\.json$")
        existing_accounts: dict[str, Path] = {}
        for f in config_dir.iterdir():
            match = account_pattern.match(f.name)
            if match:
                existing_accounts[match.group(1)] = ensure_within(config_dir, f)

        if qq_account and qq_account.strip():
            # Specific QQ account. If NapCat already has account-specific configs,
            # do not silently create a different account file: that usually means
            # the wizard QQ number and the currently logged-in NapCat account differ.
            account = qq_account.strip()
            if not re.fullmatch(r"\d{5,20}", account):
                return {"configured": [], "skipped": [], "errors": ["QQ 账号格式无效"]}
            target = ensure_within(config_dir, config_dir / f"onebot11_{account}.json")
            if existing_accounts and account not in existing_accounts and not target.exists():
                detected = ", ".join(sorted(existing_accounts))
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [
                        f"NapCat 当前已有账号配置 {detected}，与向导 QQ {account} 不匹配；"
                        "请确认 NapCat 当前登录账号后重试"
                    ],
                }
            target_files.append(target)
        else:
            # Auto-detect is safe only when exactly one account-specific config
            # exists. Each OneBot account owns its own HTTP listeners, so writing
            # the same Diary/Bilibili ports into multiple account configs would
            # create bind conflicts inside the same NapCat process.
            if len(existing_accounts) == 1:
                target_files.extend(existing_accounts.values())
            elif len(existing_accounts) > 1:
                detected = ", ".join(sorted(existing_accounts))
                return {
                    "configured": [],
                    "skipped": [],
                    "errors": [
                        f"检测到多个 NapCat 账号配置 {detected}；"
                        "请在向导中明确填写要配置的 QQ 账号"
                    ],
                }
            else:
                # Fallback: create default onebot11.json if nothing found.
                target_files.append(ensure_within(config_dir, config_dir / "onebot11.json"))

        try:
            desired_ws_entry = NapCatConfigurator._load_adapter_ws_entry()
            desired_diary_http_entry = NapCatConfigurator._load_diary_http_entry()
            desired_bilibili_http_entry = NapCatConfigurator._load_bilibili_http_entry()
        except ValueError as e:
            return {"configured": [], "skipped": [], "errors": [str(e)]}

        for target_path in target_files:
            try:
                result = NapCatConfigurator._configure_file(
                    target_path,
                    desired_ws_entry,
                    desired_diary_http_entry,
                    desired_bilibili_http_entry,
                )
                if result["changed"]:
                    configured.append(str(target_path.name))
                else:
                    skipped.append(str(target_path.name))
            except Exception as e:
                errors.append(f"{target_path.name}: {e}")

        return {"configured": configured, "skipped": skipped, "errors": errors}

    @staticmethod
    def _configure_file(
        target_path: Path,
        desired_ws_entry: dict[str, Any],
        desired_diary_http_entry: dict[str, Any],
        desired_bilibili_http_entry: dict[str, Any],
    ) -> dict[str, bool]:
        """
        Configure a single onebot11 JSON file.
        Creates it from scratch if it doesn't exist.
        Existing managed entries are reconciled instead of merely detected.
        Returns {"changed": bool}.
        """
        import json as _json

        target_path = ensure_within(target_path.parent, target_path)
        if not re.fullmatch(r"onebot11(?:_\d{5,20})?\.json", target_path.name):
            raise ValueError(f"非法 NapCat 配置文件名: {target_path.name}")
        # codeql[py/path-injection]
        if target_path.exists():
            # codeql[py/path-injection]
            raw = target_path.read_text(encoding="utf-8")
            try:
                doc = _json.loads(raw)
            except _json.JSONDecodeError as e:
                raise ValueError(
                    f"现有配置 JSON 损坏，已拒绝覆盖: line {e.lineno}, column {e.colno}: {e.msg}"
                ) from e
            if not isinstance(doc, dict):
                raise ValueError("现有 NapCat 配置顶层必须是 JSON 对象，已拒绝覆盖")
        else:
            doc = {}

        changed = False

        # Ensure top-level structure, but reject incompatible existing types.
        if "network" not in doc:
            doc["network"] = {}
            changed = True
        elif not isinstance(doc["network"], dict):
            raise ValueError("network 字段必须是 JSON 对象，已拒绝覆盖")
        network = doc["network"]

        # --- WebSocket Clients ---
        if "websocketClients" not in network:
            network["websocketClients"] = []
            changed = True
        elif not isinstance(network["websocketClients"], list):
            raise ValueError("network.websocketClients 必须是数组，已拒绝覆盖")
        ws_clients = network["websocketClients"]
        if any(not isinstance(c, dict) for c in ws_clients):
            raise ValueError("network.websocketClients 包含非对象条目，已拒绝覆盖")

        # Prefer the entry explicitly named NachoBot; for backward compatibility,
        # also recognize the old fixed localhost:8095 entry.
        nachobot_ws = next(
            (
                c
                for c in ws_clients
                if c.get("name") == "NachoBot"
                or c.get("url") == "ws://localhost:8095"
            ),
            None,
        )
        if nachobot_ws is None:
            ws_clients.append(dict(desired_ws_entry))
            changed = True
        elif NapCatConfigurator._reconcile_entry(nachobot_ws, desired_ws_entry):
            changed = True

        # --- HTTP Servers ---
        if "httpServers" not in network:
            network["httpServers"] = []
            changed = True
        elif not isinstance(network["httpServers"], list):
            raise ValueError("network.httpServers 必须是数组，已拒绝覆盖")
        http_servers = network["httpServers"]
        if any(not isinstance(s, dict) for s in http_servers):
            raise ValueError("network.httpServers 包含非对象条目，已拒绝覆盖")

        # Manage HTTP endpoints by their configured target port only. Do not use
        # names as a fallback: another bot/account may legitimately have its own
        # QZone/Diary/BiliBili entry on a different port.
        diary_port = desired_diary_http_entry["port"]
        diary = next((s for s in http_servers if s.get("port") == diary_port), None)
        if diary is None:
            http_servers.append(dict(desired_diary_http_entry))
            changed = True
        elif str(diary.get("name", "")).lower() != "diary":
            raise ValueError(
                f"NapCat HTTP 端口 {diary_port} 已被条目 {diary.get('name', '<unnamed>')} 占用"
            )
        elif NapCatConfigurator._reconcile_entry(diary, desired_diary_http_entry):
            changed = True

        bilibili_port = desired_bilibili_http_entry["port"]
        bilibili = next((s for s in http_servers if s.get("port") == bilibili_port), None)
        if bilibili is None:
            http_servers.append(dict(desired_bilibili_http_entry))
            changed = True
        elif str(bilibili.get("name", "")).lower() not in {"bilibili", "bili bili"}:
            raise ValueError(
                f"NapCat HTTP 端口 {bilibili_port} 已被条目 {bilibili.get('name', '<unnamed>')} 占用"
            )
        elif NapCatConfigurator._reconcile_entry(bilibili, desired_bilibili_http_entry):
            changed = True

        # Ensure other standard arrays exist.
        for key in ["httpSseServers", "httpClients", "websocketServers", "plugins"]:
            if key not in network:
                network[key] = []
                changed = True
            elif not isinstance(network[key], list):
                raise ValueError(f"network.{key} 必须是数组，已拒绝覆盖")

        # Ensure top-level defaults and make sure additions are persisted.
        for key, value in {
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        }.items():
            if key not in doc:
                doc[key] = value
                changed = True

        if changed:
            # Backup existing file before writing
            # codeql[py/path-injection]
            if target_path.exists():
                BackupManager.backup(target_path)
            # codeql[py/path-injection]
            target_path.write_text(
                _json.dumps(doc, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        return {"changed": changed}


# =========================================================================
# Dependency Installer


class DependencyInstaller:
    """Installs locked project dependencies and Core browser assets."""

    # Projects that need uv sync, mapped by component ID
    UV_PROJECTS: dict[str, str] = {
        "core": "NachoBot",
        "qq": "NachoBot-Napcat-Adapter",
        "tts": "NachoBot-Multimodal-Adapter",
        "tts_relay": "NachoBot-Multimodal-Adapter",
        "bilibili": "NachoBot-Bilibili-Adapter",
        "discord_koishi": "NachoBot-Koishi-Adapter",
        "discord_vc": "NachoBot-DiscordVC-Adapter",
        "universalvc": "NachoBot-UniversalVC-Adapter",
        "webui": "webUI",
    }

    # Projects that use the repository-pinned Yarn release.
    YARN_PROJECTS: dict[str, str] = {
        "discord_koishi_yarn": "koishi-app",
    }

    PLAYWRIGHT_PROJECTS: dict[str, str] = {
        "core_playwright": "NachoBot",
    }

    @staticmethod
    async def ensure_git(
        callback: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Ensure Git is available, installing it automatically on Windows when needed."""
        current = EnvironmentChecker.check_git()
        if current.get("status") == "ok":
            return {
                "status": "ok",
                "message": current.get("message") or "Git 已安装",
            }

        if os.name != "nt":
            return {
                "status": "error",
                "message": "Git 未安装；当前平台暂不支持自动安装 Git，请先手动安装",
            }

        winget = shutil.which("winget")
        if not winget:
            return {
                "status": "error",
                "message": "Git 未安装，且未找到 winget，无法自动安装 Git",
            }

        if callback:
            await callback("[Setup] Git 未找到，正在通过 winget 自动安装 Git...\n")

        try:
            proc = await asyncio.create_subprocess_exec(
                winget,
                "install",
                "--id",
                "Git.Git",
                "-e",
                "--source",
                "winget",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(ROOT_DIR),
            )

            import locale

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
            if proc.returncode != 0:
                return {
                    "status": "error",
                    "message": f"Git 自动安装失败，winget 退出码: {proc.returncode}",
                }

            # The current WebUI process does not automatically inherit PATH changes
            # made by an installer. Add common Git command directories immediately.
            candidate_dirs = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd",
            ]
            path_entries = os.environ.get("PATH", "").split(os.pathsep)
            normalized_entries = {os.path.normcase(os.path.normpath(p)) for p in path_entries if p}
            for git_dir in candidate_dirs:
                if not (git_dir / "git.exe").exists():
                    continue
                normalized = os.path.normcase(os.path.normpath(str(git_dir)))
                if normalized not in normalized_entries:
                    path_entries.insert(0, str(git_dir))
                    normalized_entries.add(normalized)
            os.environ["PATH"] = os.pathsep.join(path_entries)

            verified = EnvironmentChecker.check_git()
            if verified.get("status") != "ok":
                return {
                    "status": "error",
                    "message": "Git 安装完成，但当前 WebUI 进程仍无法执行 git，请重启 WebUI 后重试",
                }

            if callback:
                await callback(f"[Setup] Git 已就绪: {verified.get('message', 'Git')}\n")
            return {
                "status": "ok",
                "message": verified.get("message") or "Git 安装完成",
            }
        except Exception as e:
            return {"status": "error", "message": f"Git 自动安装出错: {e}"}

    @staticmethod
    def get_install_tasks(
        components: list[str],
        multimodal_runtime: str = "gpu",
    ) -> list[dict[str, str]]:
        """Return install tasks for selected components and Multimodal runtime."""
        runtime = MultimodalRuntimeManager.normalize_profile(multimodal_runtime)
        tasks = []

        # Always install core
        tasks.append(
            {
                "id": "core",
                "type": "uv",
                "name": "NachoBot Core",
                "dir": "NachoBot",
            }
        )
        tasks.append(
            {
                "id": "core_playwright",
                "type": "playwright",
                "name": "Playwright Chromium",
                "dir": "NachoBot",
            }
        )

        component_set = set(components)

        if "qq" in component_set:
            tasks.append(
                {
                    "id": "qq",
                    "type": "uv",
                    "name": "Napcat Adapter",
                    "dir": "NachoBot-Napcat-Adapter",
                }
            )

        if "tts" in component_set:
            runtime_label = MultimodalRuntimeManager.PROFILE_META[runtime]["label"]
            tasks.append(
                {
                    "id": "tts",
                    "type": "uv",
                    "name": f"Multimodal Adapter ({runtime_label})",
                    "dir": "NachoBot-Multimodal-Adapter",
                    "runtime": runtime,
                }
            )

        # The lightweight POTATO/Relay runtime is part of the baseline WebUI
        # deployment so POTATO can be started later without downloading the
        # local Torch/model stack. If Relay is already the selected primary
        # Multimodal runtime, the primary tts task above already covers it.
        if not ("tts" in component_set and runtime == "relay"):
            tasks.append(
                {
                    "id": "tts_relay",
                    "type": "uv",
                    "name": "Multimodal Adapter (Relay / POTATO)",
                    "dir": "NachoBot-Multimodal-Adapter",
                    "runtime": "relay",
                }
            )

        if "bilibili" in component_set:
            tasks.append(
                {
                    "id": "bilibili",
                    "type": "uv",
                    "name": "Bilibili Adapter",
                    "dir": "NachoBot-Bilibili-Adapter",
                }
            )

        if "discord" in component_set:
            tasks.append(
                {
                    "id": "discord_koishi",
                    "type": "uv",
                    "name": "Koishi Adapter",
                    "dir": "NachoBot-Koishi-Adapter",
                }
            )
            tasks.append(
                {
                    "id": "discord_vc",
                    "type": "uv",
                    "name": "DiscordVC Adapter",
                    "dir": "NachoBot-DiscordVC-Adapter",
                }
            )
            tasks.append(
                {
                    "id": "discord_koishi_yarn",
                    "type": "yarn",
                    "name": "Koishi App (Yarn)",
                    "dir": "koishi-app",
                }
            )

        if "universalvc" in component_set:
            tasks.append(
                {
                    "id": "universalvc",
                    "type": "uv",
                    "name": "UniversalVC Adapter",
                    "dir": "NachoBot-UniversalVC-Adapter",
                }
            )

        return tasks

    @staticmethod
    async def install(
        task: dict[str, str],
        callback: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """
        Run a validated uv, Yarn, or Playwright installation task.
        Returns {"status": "ok"|"error", "message": "..."}.
        """
        try:
            project_dir = DependencyInstaller._resolve_task_project(task)
        except (KeyError, ValueError) as e:
            return {"status": "error", "message": str(e)}
        if not project_dir.exists():
            return {"status": "error", "message": f"目录不存在: {project_dir}"}

        if task["type"] == "uv":
            if str(task.get("id", "")).strip() in {"tts", "tts_relay"}:
                try:
                    runtime = MultimodalRuntimeManager.normalize_profile(task.get("runtime"))
                except ValueError as e:
                    return {"status": "error", "message": str(e)}
                return await MultimodalRuntimeManager.install(runtime, callback)
            return await DependencyInstaller._run_uv_sync(project_dir, callback)
        elif task["type"] == "yarn":
            return await DependencyInstaller._run_yarn_install(project_dir, callback)
        elif task["type"] == "playwright":
            return await DependencyInstaller._run_playwright_install(project_dir, callback)
        else:
            return {"status": "error", "message": f"未知安装类型: {task['type']}"}

    @staticmethod
    def _resolve_task_project(task: dict[str, str]) -> Path:
        task_id = str(task.get("id", "")).strip()
        task_type = str(task.get("type", "")).strip()
        requested_dir = str(task.get("dir", "")).strip()

        if task_type == "uv":
            expected_dir = DependencyInstaller.UV_PROJECTS.get(task_id)
        elif task_type == "yarn":
            expected_dir = DependencyInstaller.YARN_PROJECTS.get(task_id)
        elif task_type == "playwright":
            expected_dir = DependencyInstaller.PLAYWRIGHT_PROJECTS.get(task_id)
        else:
            raise ValueError(f"未知安装类型: {task_type}")

        if not expected_dir or requested_dir != expected_dir:
            raise ValueError(f"安装任务无效: {task_id}")

        return resolve_relative_to_root(ROOT_DIR, expected_dir)

    @staticmethod
    async def _run_uv_sync(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Execute `uv sync` in a project directory."""
        import locale

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                "uv",
                "sync",
                "--python",
                ">=3.11,<=3.13",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
                env=env,
            )

            fallback_enc = locale.getpreferredencoding(False) or "gbk"
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

            if proc.returncode == 0:
                return {"status": "ok", "message": "依赖安装完成"}
            else:
                return {
                    "status": "error",
                    "message": f"uv sync 退出码: {proc.returncode}",
                }
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as e:
            return {"status": "error", "message": f"安装出错: {e}"}

    @staticmethod
    async def _run_playwright_install(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Install and launch-check the Chromium revision required by Playwright."""
        import locale

        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        try:
            proc = await asyncio.create_subprocess_exec(
                "uv",
                "run",
                "python",
                "scripts/ensure_playwright.py",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
                env=env,
            )

            fallback_enc = locale.getpreferredencoding(False) or "gbk"
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
            if proc.returncode == 0:
                return {"status": "ok", "message": "Playwright Chromium 已就绪"}
            return {
                "status": "error",
                "message": f"Playwright Chromium 准备失败，退出码: {proc.returncode}",
            }
        except FileNotFoundError:
            return {"status": "error", "message": "uv 未安装，请先安装 uv"}
        except Exception as e:
            return {"status": "error", "message": f"Playwright Chromium 准备出错: {e}"}

    @staticmethod
    async def _run_yarn_install(
        project_dir: Path,
        callback: Callable[[str], Any] | None,
    ) -> dict[str, Any]:
        """Execute the repository-pinned immutable Yarn install."""
        import locale

        env = os.environ.copy()

        try:
            command = (
                ["cmd", "/c", "corepack", "yarn", "install", "--immutable"]
                if os.name == "nt"
                else ["corepack", "yarn", "install", "--immutable"]
            )
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(project_dir),
                env=env,
            )

            fallback_enc = locale.getpreferredencoding(False) or "gbk"
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

            if proc.returncode == 0:
                return {"status": "ok", "message": "yarn install --immutable 完成"}
            else:
                return {
                    "status": "error",
                    "message": f"yarn install --immutable 退出码: {proc.returncode}",
                }
        except FileNotFoundError:
            return {"status": "error", "message": "Corepack/Yarn 未安装"}
        except Exception as e:
            return {"status": "error", "message": f"安装出错: {e}"}
