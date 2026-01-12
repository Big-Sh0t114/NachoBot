import asyncio
import base64
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import websockets

try:
    import tomllib as toml
except ImportError:  # pragma: no cover
    import toml  # type: ignore

ROOT_DIR = Path(__file__).resolve().parents[1]
for candidate in ("NachoBot", "NachoBot-Napcat-Adapter", "nachobot_tts_adapter"):
    candidate_path = ROOT_DIR / candidate
    if candidate_path.exists():
        candidate_str = str(candidate_path)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)

from ncnk_message import (  # noqa: E402
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    Router,
    RouteConfig,
    Seg,
    TargetConfig,
    UserInfo,
)


ACCEPT_FORMAT = [
    "text",
    "image",
    "emoji",
    "reply",
    "voice",
    "command",
    "voiceurl",
    "music",
    "videourl",
    "file",
    "imageurl",
    "forward",
    "video",
]


@dataclass
class AdapterConfig:
    onebot_ws_url: str
    onebot_token: str
    onebot_reconnect_seconds: int
    nachobot_host: str
    nachobot_port: int
    platform: str
    group_list_type: str
    group_list: List[str]
    private_list_type: str
    private_list: List[str]
    ban_user_id: List[str]
    use_tts: bool
    log_level: str


def _load_toml(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    if hasattr(toml, "loads"):
        return toml.loads(raw.decode("utf-8"))
    return toml.load(path)  # type: ignore[attr-defined]


def load_config(path: Path) -> AdapterConfig:
    data = _load_toml(path)
    onebot = data.get("onebot_server", {})
    nachobot = data.get("nachobot_server", {})
    chat = data.get("chat", {})
    voice = data.get("voice", {})
    debug = data.get("debug", {})

    ws_url = onebot.get("ws_url", "")
    if not ws_url:
        host = onebot.get("host", "127.0.0.1")
        port = int(onebot.get("port", 5140))
        path_part = onebot.get("path", "/onebot/v11/ws")
        ws_url = f"ws://{host}:{port}{path_part}"

    return AdapterConfig(
        onebot_ws_url=ws_url,
        onebot_token=str(onebot.get("token", "") or ""),
        onebot_reconnect_seconds=int(onebot.get("reconnect_seconds", 5)),
        nachobot_host=str(nachobot.get("host", "127.0.0.1")),
        nachobot_port=int(nachobot.get("port", 8070)),
        platform=str(nachobot.get("platform", "discord")),
        group_list_type=str(chat.get("group_list_type", "whitelist")),
        group_list=[str(x) for x in chat.get("group_list", [])],
        private_list_type=str(chat.get("private_list_type", "blacklist")),
        private_list=[str(x) for x in chat.get("private_list", [])],
        ban_user_id=[str(x) for x in chat.get("ban_user_id", [])],
        use_tts=bool(voice.get("use_tts", True)),
        log_level=str(debug.get("level", "INFO")),
    )


def setup_logging(level: str) -> logging.Logger:
    logger = logging.getLogger("koishi-onebot-adapter")
    if logger.handlers:
        return logger
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    return logger


class KoishiOneBotAdapter:
    def __init__(self, config: AdapterConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.onebot_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.onebot_send_lock = asyncio.Lock()
        self._recent_group_by_user: Dict[str, Tuple[str, str, float]] = {}
        self._recent_group_ttl = 120.0
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=None,
                )
            }
        )
        self.router = Router(route_config)
        self.router.register_class_handler(self.handle_from_nachobot)

    async def run(self) -> None:
        await asyncio.gather(self.router.run(), self.onebot_loop())

    def _is_allowed(self, user_id: str, group_id: Optional[str]) -> bool:
        if user_id in self.config.ban_user_id:
            return False
        if group_id:
            if self.config.group_list_type == "whitelist" and group_id not in self.config.group_list:
                return False
            if self.config.group_list_type == "blacklist" and group_id in self.config.group_list:
                return False
        else:
            if self.config.private_list_type == "whitelist" and user_id not in self.config.private_list:
                return False
            if self.config.private_list_type == "blacklist" and user_id in self.config.private_list:
                return False
        return True

    async def onebot_loop(self) -> None:
        ws_url = self._build_ws_url()
        while True:
            try:
                ws = await self._open_onebot(ws_url)
                self.onebot_ws = ws
                self.logger.info("Connected to OneBot server")
                try:
                    await self._receive_onebot(ws)
                finally:
                    await ws.close()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning(f"OneBot connection error: {exc}")
            await asyncio.sleep(self.config.onebot_reconnect_seconds)

    def _build_ws_url(self) -> str:
        if not self.config.onebot_token:
            return self.config.onebot_ws_url
        if "access_token=" in self.config.onebot_ws_url:
            return self.config.onebot_ws_url
        parsed = urllib.parse.urlparse(self.config.onebot_ws_url)
        query = urllib.parse.parse_qs(parsed.query)
        query["access_token"] = [self.config.onebot_token]
        new_query = urllib.parse.urlencode(query, doseq=True)
        return parsed._replace(query=new_query).geturl()

    async def _open_onebot(self, ws_url: str) -> websockets.WebSocketClientProtocol:
        connect_kwargs: Dict[str, Any] = {"max_size": 2**26}
        if self.config.onebot_token:
            connect_kwargs["extra_headers"] = {"Authorization": f"Bearer {self.config.onebot_token}"}

        try:
            return await websockets.connect(ws_url, **connect_kwargs)
        except TypeError as exc:
            err_text = str(exc)
            if "extra_headers" in err_text and "extra_headers" in connect_kwargs:
                connect_kwargs.pop("extra_headers", None)
                return await websockets.connect(ws_url, **connect_kwargs)
            if "max_size" in err_text and "max_size" in connect_kwargs:
                connect_kwargs.pop("max_size", None)
                return await websockets.connect(ws_url, **connect_kwargs)
            raise

    async def _receive_onebot(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            post_type = data.get("post_type")
            if post_type == "message":
                await self.handle_onebot_message(data)
                continue
            if "status" in data and "retcode" in data:
                status = data.get("status")
                retcode = data.get("retcode")
                message = data.get("message", "")
                echo = data.get("echo")
                if status != "ok":
                    self.logger.warning(
                        f"OneBot action failed: status={status} retcode={retcode} message={message} echo={echo}"
                    )
                else:
                    self.logger.debug(
                        f"OneBot action ok: retcode={retcode} echo={echo}"
                    )

    async def handle_onebot_message(self, data: Dict[str, Any]) -> None:
        message_type = data.get("message_type")
        user_id = str(data.get("user_id") or "")
        self_id = data.get("self_id")
        if self_id is not None and str(self_id) == user_id:
            return

        group_id = None
        raw_group_id = data.get("group_id")
        if raw_group_id is not None and str(raw_group_id) != "":
            group_id = str(raw_group_id)
            if message_type != "group":
                self.logger.warning(
                    f"OneBot message has group_id but message_type={message_type}, forcing group"
                )
                message_type = "group"
        if not user_id:
            return
        if not self._is_allowed(user_id, group_id):
            self.logger.info(
                f"OneBot message blocked by list: message_type={message_type} user_id={user_id} group_id={group_id}"
            )
            return

        segments, additional_config, content_format = await self._parse_onebot_message(data.get("message"))
        if not segments:
            return

        sender = data.get("sender") or {}
        nickname = sender.get("card") or sender.get("nickname") or user_id

        user_info = UserInfo(
            platform=self.config.platform,
            user_id=user_id,
            user_nickname=nickname,
            user_cardname=sender.get("card"),
        )
        group_info = None
        if group_id:
            group_name = self._extract_group_name(data, group_id)
            self._remember_user_group(user_id, group_id, group_name)
            group_info = GroupInfo(
                platform=self.config.platform,
                group_id=group_id,
                group_name=group_name,
            )

        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=str(data.get("message_id") or f"ob-{int(time.time() * 1000)}"),
            time=float(data.get("time") or time.time()),
            user_info=user_info,
            group_info=group_info,
            format_info=FormatInfo(
                content_format=content_format,
                accept_format=ACCEPT_FORMAT,
            ),
            additional_config=additional_config or None,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=segments),
            raw_message=json.dumps(data, ensure_ascii=True),
        )
        message_payload = message.to_dict()
        group_payload = message_payload.get("message_info", {}).get("group_info")
        if group_id and group_payload is not None and not group_payload.get("group_name"):
            group_payload["group_name"] = str(group_id)
        self.logger.info(
            f"Forward OneBot -> NachoBot: message_type={message_type} user_id={user_id} group_id={group_id}"
        )
        await self._send_to_nachobot(message, message_payload)

    @staticmethod
    def _extract_group_name(data: Dict[str, Any], group_id: str) -> str:
        for key in ("group_name", "channel_name", "guild_name"):
            value = data.get(key)
            if value:
                return str(value)
        return str(group_id) if group_id else ""

    async def _send_to_nachobot(self, message: MessageBase, payload: Dict[str, Any]) -> None:
        client = self.router.clients.get(self.config.platform)
        if client is not None:
            await client.send_message(payload)
            return
        await self.router.send_message(message)

    def _remember_user_group(self, user_id: str, group_id: str, group_name: str) -> None:
        if not user_id or not group_id:
            return
        self._recent_group_by_user[user_id] = (group_id, group_name, time.time())

    def _get_recent_group_for_user(self, user_id: str) -> Optional[Tuple[str, str]]:
        if not user_id:
            return None
        data = self._recent_group_by_user.get(user_id)
        if not data:
            return None
        group_id, group_name, last_ts = data
        if time.time() - last_ts > self._recent_group_ttl:
            self._recent_group_by_user.pop(user_id, None)
            return None
        return group_id, group_name

    async def _parse_onebot_message(
        self, raw_message: Any
    ) -> Tuple[List[Seg], Dict[str, Any], List[str]]:
        segments: List[Seg] = []
        additional_config: Dict[str, Any] = {}
        content_format: List[str] = []

        if isinstance(raw_message, str):
            raw_message = [{"type": "text", "data": {"text": raw_message}}]
        elif isinstance(raw_message, dict):
            raw_message = [raw_message]
        if not isinstance(raw_message, list):
            return [], additional_config, content_format

        for seg in raw_message:
            seg_type = seg.get("type")
            seg_data = seg.get("data") or {}
            if seg_type == "text":
                text = seg_data.get("text") or ""
                if text:
                    segments.append(Seg(type="text", data=text))
                    if "text" not in content_format:
                        content_format.append("text")
            elif seg_type == "at":
                target = seg_data.get("qq") or seg_data.get("id") or "unknown"
                segments.append(Seg(type="text", data=f"@<{target}>"))
                if "text" not in content_format:
                    content_format.append("text")
            elif seg_type == "reply":
                reply_id = seg_data.get("id")
                if reply_id is not None:
                    additional_config["reply_message_id"] = reply_id
            elif seg_type == "image":
                image_data = await self._image_to_base64(seg_data)
                if image_data:
                    segments.append(Seg(type="image", data=image_data))
                    if "image" not in content_format:
                        content_format.append("image")
                else:
                    segments.append(Seg(type="text", data="[image]"))
                    if "text" not in content_format:
                        content_format.append("text")
            elif seg_type == "record":
                segments.append(Seg(type="text", data="[voice]"))
                if "text" not in content_format:
                    content_format.append("text")

        return segments, additional_config, content_format

    async def _image_to_base64(self, seg_data: Dict[str, Any]) -> Optional[str]:
        url = seg_data.get("url") or seg_data.get("file")
        if not url:
            return None
        if isinstance(url, str) and url.startswith("base64://"):
            return url[len("base64://") :]
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return await self._download_base64(url)
        return None

    async def _download_base64(self, url: str) -> Optional[str]:
        def _fetch() -> bytes:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read()

        try:
            data = await asyncio.to_thread(_fetch)
        except Exception as exc:
            self.logger.warning(f"Image download failed: {exc}")
            return None
        return base64.b64encode(data).decode("ascii")

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        raw_message_base = MessageBase.from_dict(raw_message_base_dict)
        message_segment = raw_message_base.message_segment
        if message_segment.type == "command":
            self.logger.info("Command segment ignored for OneBot")
            return

        processed_message = self._seg_to_onebot(message_segment)
        if not processed_message:
            return

        message_info = raw_message_base.message_info
        group_info = message_info.group_info
        user_info = message_info.user_info

        params: Dict[str, Any] = {"message": processed_message}
        if group_info and group_info.group_id:
            params["message_type"] = "group"
            params["group_id"] = self._maybe_int(group_info.group_id)
        elif user_info and user_info.user_id:
            fallback = self._get_recent_group_for_user(str(user_info.user_id))
            if fallback:
                fallback_group_id, _ = fallback
                params["message_type"] = "group"
                params["group_id"] = self._maybe_int(fallback_group_id)
                self.logger.warning(
                    f"Outgoing message missing group_info, fallback to group_id={fallback_group_id} "
                    f"for user_id={user_info.user_id}"
                )
            else:
                params["message_type"] = "private"
                params["user_id"] = self._maybe_int(user_info.user_id)
        else:
            self.logger.warning("Missing target info for outgoing message")
            return

        self.logger.info(
            f"Forward NachoBot -> OneBot: message_type={params.get('message_type')} "
            f"group_id={params.get('group_id')} user_id={params.get('user_id')}"
        )
        await self._onebot_send("send_msg", params)

    def _seg_to_onebot(self, seg_data: Seg) -> List[Dict[str, Any]]:
        payload: List[Dict[str, Any]] = []
        if seg_data.type == "seglist" and isinstance(seg_data.data, list):
            for seg in seg_data.data:
                payload.extend(self._seg_to_onebot(seg))
            return payload

        if seg_data.type == "text":
            text = str(seg_data.data or "")
            if text:
                payload.append({"type": "text", "data": {"text": text}})
        elif seg_data.type == "reply":
            target_id = seg_data.data
            if target_id:
                payload.append({"type": "reply", "data": {"id": target_id}})
        elif seg_data.type == "image":
            if seg_data.data:
                payload.append(
                    {"type": "image", "data": {"file": f"base64://{seg_data.data}", "subtype": 0}}
                )
        elif seg_data.type == "emoji":
            if seg_data.data:
                payload.append(
                    {"type": "image", "data": {"file": f"base64://{seg_data.data}", "subtype": 1}}
                )
        elif seg_data.type in ("voice", "voice_stream"):
            if self.config.use_tts and seg_data.data:
                payload.append({"type": "record", "data": {"file": f"base64://{seg_data.data}"}})
        elif seg_data.type == "imageurl":
            if seg_data.data:
                payload.append({"type": "image", "data": {"file": str(seg_data.data)}})
        elif seg_data.type == "voiceurl":
            if seg_data.data:
                payload.append({"type": "record", "data": {"file": str(seg_data.data)}})

        return payload

    async def _onebot_send(self, action: str, params: Dict[str, Any]) -> None:
        if not self.onebot_ws or self.onebot_ws.closed:
            self.logger.warning("OneBot not connected, drop message")
            return
        echo = str(uuid.uuid4())
        payload = {
            "action": action,
            "params": params,
            "echo": echo,
        }
        async with self.onebot_send_lock:
            await self.onebot_ws.send(json.dumps(payload, ensure_ascii=True))
        self.logger.info(f"OneBot action sent: {action} echo={echo}")

    @staticmethod
    def _maybe_int(value: Any) -> Any:
        try:
            return int(value)
        except (TypeError, ValueError):
            return value


async def main() -> None:
    config_path = Path(__file__).parent / "config.toml"
    config = load_config(config_path)
    logger = setup_logging(config.log_level)
    adapter = KoishiOneBotAdapter(config, logger)
    await adapter.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
