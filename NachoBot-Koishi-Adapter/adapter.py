import asyncio
import json
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from typing import Any, Dict, Optional

import websockets

from ncnk_message import (
    BaseMessageInfo,
    FormatInfo,
    GroupInfo,
    MessageBase,
    Router,
    RouteConfig,
    Seg,
    TargetConfig,
    UserInfo,
    get_core_token_from_env,
)

from config import AdapterConfig
from visual_policy import build_visual_policy
from utils import (
    maybe_int,
    ws_is_closed,
    is_allowed,
    allow_reply,
    extract_group_name,
    mask_bilibili_raw_data,
)
from message_parser import parse_onebot_message
from message_builder import (
    contains_reply_segment,
    get_accept_format,
    seg_to_onebot,
)


ACCEPT_FORMAT = get_accept_format(True)


class KoishiOneBotAdapter:
    _ONEBOT_SEND_TIMEOUT = 5.0
    _ONEBOT_RESPONSE_TIMEOUT = 5.0

    def __init__(self, config: AdapterConfig, logger: Any):
        self.config = config
        self.logger = logger
        self.onebot_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.onebot_send_lock = asyncio.Lock()
        self._onebot_response_waiters: Dict[str, asyncio.Future] = {}
        self._onebot_response_lock = asyncio.Lock()
        route_config = RouteConfig(
            route_config={
                self.config.platform: TargetConfig(
                    url=f"ws://{self.config.nachobot_host}:{self.config.nachobot_port}/ws",
                    token=get_core_token_from_env(),
                )
            }
        )
        self.router = Router(route_config, custom_logger=logger)
        self.router.register_class_handler(self.handle_from_nachobot)

    async def run(self) -> None:
        await asyncio.gather(self.router.run(), self.onebot_loop())

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
            connect_kwargs["extra_headers"] = {
                "Authorization": f"Bearer {self.config.onebot_token}"
            }

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
        try:
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
                    await self._resolve_onebot_response(data)
                    status = data.get("status")
                    retcode = data.get("retcode")
                    echo = data.get("echo")
                    if status != "ok":
                        self.logger.warning(
                            f"OneBot action failed: status={status} retcode={retcode} echo={echo}"
                        )
                    else:
                        self.logger.debug(
                            f"OneBot action ok: retcode={retcode} echo={echo}"
                        )
        finally:
            # A disconnected receive loop must wake senders and release every
            # echo waiter; otherwise a Core delivery can linger until timeout.
            await self._finish_onebot_response_waiters()

    async def _register_onebot_response_waiter(self, echo: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        async with self._onebot_response_lock:
            self._onebot_response_waiters[echo] = waiter
        return waiter

    async def _remove_onebot_response_waiter(
        self, echo: str, waiter: asyncio.Future
    ) -> None:
        async with self._onebot_response_lock:
            if self._onebot_response_waiters.get(echo) is waiter:
                self._onebot_response_waiters.pop(echo, None)

    async def _resolve_onebot_response(self, response: Mapping[str, Any]) -> None:
        raw_echo = response.get("echo")
        if raw_echo in (None, ""):
            return
        echo = str(raw_echo)
        async with self._onebot_response_lock:
            waiter = self._onebot_response_waiters.pop(echo, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(dict(response))

    async def _finish_onebot_response_waiters(self) -> None:
        async with self._onebot_response_lock:
            waiters = list(self._onebot_response_waiters.values())
            self._onebot_response_waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                # A None result represents a disconnected transport, rather
                # than cancelling the sender task that owns the waiter.
                waiter.set_result(None)

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
        if not is_allowed(self.config, user_id, group_id):
            self.logger.info(
                f"OneBot message blocked by list: message_type={message_type} user_id={user_id} group_id={group_id}"
            )
            return

        # Parse segments from ORIGINAL data (preserving URLs for normal usage)
        segments, additional_config, content_format = await parse_onebot_message(
            data.get("message"),
            data.get("raw_message", ""),
            self.logger,
            self.config.network_proxy,
        )
        if not segments:
            return
        if "image" in content_format:
            additional_config["visual_policy"] = build_visual_policy(
                self.config.visual.image
            )

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
            group_name = extract_group_name(data, group_id)
            group_info = GroupInfo(
                platform=self.config.platform,
                group_id=group_id,
                group_name=group_name,
            )

        # Sanitize data for raw_message construction
        # This ensures 'raw_message' field (used by Bilibili plugin) does not contain the URL
        sanitized_data = mask_bilibili_raw_data(data, self.logger)

        message_info = BaseMessageInfo(
            platform=self.config.platform,
            message_id=str(data.get("message_id") or f"ob-{int(time.time() * 1000)}"),
            time=float(data.get("time") or time.time()),
            user_info=user_info,
            group_info=group_info,
            format_info=FormatInfo(
                content_format=content_format,
                accept_format=get_accept_format(self.config.use_tts),
            ),
            additional_config=additional_config or None,
        )

        message = MessageBase(
            message_info=message_info,
            message_segment=Seg(type="seglist", data=segments),
            # Use sanitized data for raw_message
            raw_message=json.dumps(sanitized_data, ensure_ascii=True),
        )
        message_payload = message.to_dict()
        group_payload = message_payload.get("message_info", {}).get("group_info")
        if (
            group_id
            and group_payload is not None
            and not group_payload.get("group_name")
        ):
            group_payload["group_name"] = str(group_id)

        self.logger.info(
            f"Forward OneBot -> NachoBot: message_type={message_type} user_id={user_id} group_id={group_id}"
        )
        self.logger.debug(
            f"Raw incoming payload: {json.dumps(message_payload, ensure_ascii=False)}"
        )
        await self._send_to_nachobot(message, message_payload)

    async def _send_to_nachobot(
        self, message: MessageBase, payload: Dict[str, Any]
    ) -> None:
        client = self.router.clients.get(self.config.platform)
        if client is not None:
            await client.send_message(payload)
            return
        await self.router.send_message(message)

    async def handle_from_nachobot(self, raw_message_base_dict: dict) -> None:
        raw_message_base = MessageBase.from_dict(raw_message_base_dict)
        message_segment = raw_message_base.message_segment
        if message_segment.type == "command":
            self.logger.info("Command segment ignored for OneBot")
            return

        if not allow_reply(self.config) and contains_reply_segment(message_segment):
            self.logger.info(f"Drop reply segment for platform={self.config.platform}")

        processed_message = seg_to_onebot(message_segment, self.config, self.logger)
        if not processed_message:
            return

        message_info = raw_message_base.message_info
        group_info = message_info.group_info
        user_info = message_info.user_info
        params: Dict[str, Any] = {"message": processed_message}

        target_group_id = (
            maybe_int(group_info.group_id)
            if group_info and group_info.group_id
            else None
        )
        target_user_id = (
            maybe_int(user_info.user_id) if user_info and user_info.user_id else None
        )

        if target_group_id:
            params["message_type"] = "group"
            params["group_id"] = target_group_id
        elif target_user_id:
            params["message_type"] = "private"
            params["user_id"] = target_user_id
        else:
            self.logger.warning("Missing target info for outgoing message")
            return

        self.logger.info(
            f"Forward NachoBot -> OneBot: message_type={params.get('message_type')} "
            f"group_id={params.get('group_id')} user_id={params.get('user_id')}"
        )
        self.logger.info(
            f"OneBot outgoing segments: {[seg.get('type') for seg in processed_message]}"
        )
        try:
            self.logger.info("OneBot send start")
            response = await self._onebot_send("send_msg", params)
            self.logger.info("OneBot send done")
            actual_id = self._extract_message_id(response)
            source_id = message_info.message_id
            if actual_id is not None and source_id not in (None, ""):
                try:
                    await self.router.send_custom_message(
                        platform=self.config.platform,
                        message_type_name="message_id_echo",
                        message={
                            "type": "echo",
                            "echo": source_id,
                            "actual_id": actual_id,
                        },
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning(
                        f"OneBot message_id_echo handoff failed: {type(exc).__name__}"
                    )
        except asyncio.CancelledError:
            self.logger.warning("OneBot send cancelled")
            raise
        except Exception as exc:
            self.logger.error(f"OneBot send raised: {type(exc).__name__}")

    @staticmethod
    def _extract_message_id(response: Any) -> Optional[str]:
        if not isinstance(response, Mapping):
            return None
        data = response.get("data")
        if not isinstance(data, Mapping):
            return None
        actual_id = data.get("message_id")
        if actual_id in (None, ""):
            return None
        return str(actual_id)

    async def _onebot_send(
        self, action: str, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        ws = self.onebot_ws
        if ws_is_closed(ws):
            self.logger.warning(
                "OneBot not connected, drop message (ws={} closed={})",
                bool(ws),
                getattr(ws, "closed", None),
            )
            return None
        echo = str(uuid.uuid4())
        payload = {
            "action": action,
            "params": params,
            "echo": echo,
        }
        waiter = await self._register_onebot_response_waiter(echo)
        self.logger.info(f"OneBot action sending: {action} echo={echo}")
        try:
            try:
                async with self.onebot_send_lock:
                    await asyncio.wait_for(
                        ws.send(json.dumps(payload, ensure_ascii=True)),
                        timeout=self._ONEBOT_SEND_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                self.logger.error(f"OneBot action send timeout: {action} echo={echo}")
                try:
                    await ws.close()
                except Exception as close_exc:
                    self.logger.warning(
                        f"OneBot ws close failed after timeout: {type(close_exc).__name__}"
                    )
                return None
            except Exception as exc:
                self.logger.error(
                    f"OneBot action send failed: {action} echo={echo} err={type(exc).__name__}"
                )
                return None

            self.logger.info(f"OneBot action sent: {action} echo={echo}")
            try:
                response = await asyncio.wait_for(
                    waiter, timeout=self._ONEBOT_RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"OneBot action response timeout: {action} echo={echo}"
                )
                return None
            if not isinstance(response, Mapping):
                return None
            status = response.get("status")
            retcode = response.get("retcode")
            if status != "ok" or retcode not in (None, 0, "0"):
                return None
            return dict(response)
        finally:
            await self._remove_onebot_response_waiter(echo, waiter)
