"""Live room WebSocket worker for Bilibili Adapter."""

import asyncio
import contextlib
import json
from loguru import logger
import os
import socket
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import brotli
import websockets

from bili_src.core.config import (
    AdapterConfig,
    _load_proxy_pool,
    _check_proxy_list,
    _proxy_dicts_to_urls,
)
from bili_src.core.utils import _normalize_text

if TYPE_CHECKING:
    from bili_src.api.api import BilibiliApi
    from adapter import BilibiliAdapter


class LiveRoomWorker:
    def __init__(
        self,
        room_id: int,
        config: AdapterConfig,
        api: "BilibiliApi",
        adapter: "BilibiliAdapter",
        logger,
    ):
        self.room_id = room_id
        self.config = config
        self.api = api
        self.adapter = adapter
        self.logger = logger
        self._stop_event = asyncio.Event()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._proxy_index: int = 0
        self._proxy_cycle: Optional[List[str]] = None
        self._authed = False

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            await self._ws.close()

    def _get_proxy_cycle(self) -> Optional[List[str]]:
        if self._proxy_cycle:
            return self._proxy_cycle
        pool_path = Path(self.config.live_proxy_pool_path)
        if not pool_path.is_absolute():
            pool_path = Path(__file__).resolve().parent / pool_path
        proxy_list = _load_proxy_pool(pool_path)
        if not proxy_list:
            self.logger.warning("Proxy pool is empty: %s", pool_path)
            return None
        check_url = (self.config.live_proxy_check_url or "").strip()
        if check_url:
            checked = _check_proxy_list(
                proxy_list,
                check_url,
                self.config.live_proxy_check_timeout,
                self.logger,
            )
            if checked:
                proxy_list = checked
        proxy_cycle = _proxy_dicts_to_urls(proxy_list)
        if not proxy_cycle:
            self.logger.warning("Proxy pool has no usable entries: %s", pool_path)
            return None
        self._proxy_cycle = proxy_cycle
        return proxy_cycle

    @staticmethod
    def _safe_get_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _safe_get_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _should_mark_mention(self, text: str) -> bool:
        if self.config.live_mention_any_at:
            if "@" in text or "＠" in text:
                return True
        for keyword in self.config.live_mention_keywords:
            if not keyword:
                continue
            if keyword in text:
                return True
            for prefix in self.config.live_mention_prefixes:
                if not prefix:
                    continue
                if f"{prefix}{keyword}" in text:
                    return True
        return False

    async def _screen_refresh_loop(self) -> None:
        """Periodically refresh the adapter's local screen-summary cache."""
        self.logger.info(f"Screen refresh loop started for room {self.room_id}")
        while not self._stop_event.is_set():
            try:
                # VLM refreshes are serial, so this interval also bounds capture rate.
                await asyncio.sleep(self.config.screen_capture_interval_seconds)
                if self._stop_event.is_set():
                    break

                # Skip refresh if screen monitoring is manually disabled (#screen_off).
                manual_state = self.adapter._get_screen_manual_state()
                if manual_state is False:
                    continue

                await self.adapter.refresh_screen_summary(self.room_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Screen refresh error: {e}")
                await asyncio.sleep(5)  # Error backoff

    async def run(self) -> None:
        # Prime the local summary cache before processing live messages.
        try:
            # Wait a bit for system to settle
            await asyncio.sleep(5)
            self.logger.info(f"Performing initial screen refresh for room {self.room_id}")
            await self.adapter.refresh_screen_summary(self.room_id)
        except Exception as e:
            self.logger.warning(f"Initial screen refresh failed: {e}")

        # Keep the local summary cache fresh for prompt construction.
        asyncio.create_task(self._screen_refresh_loop())

        backoff = self.config.reconnect_seconds
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.warning(
                    "Room %s error: %s (%s)",
                    self.room_id,
                    exc,
                    type(exc).__name__,
                    exc_info=True,
                )
            if self._stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(self.config.max_reconnect_seconds, backoff * 2)

    async def _run_once(self) -> None:
        info = await self.api.get_danmu_info(self.room_id)
        info_code = (info or {}).get("code")
        info_msg = (info or {}).get("message") or (info or {}).get("msg") or ""
        data = (info or {}).get("data", {})
        token = data.get("token")
        host_list = data.get("host_list") or []
        self.logger.info(
            "Room %s getDanmuInfo: code=%s message=%s token=%s hosts=%s",
            self.room_id,
            info_code,
            info_msg,
            bool(token),
            len(host_list),
        )
        if host_list:
            host_preview = [
                {
                    "host": item.get("host"),
                    "wss_port": item.get("wss_port"),
                    "ws_port": item.get("ws_port"),
                }
                for item in host_list[:3]
                if isinstance(item, dict)
            ]
            if host_preview:
                self.logger.debug(
                    "Room %s host_list preview: %s", self.room_id, host_preview
                )
        if not token or not host_list:
            raise RuntimeError(
                f"getDanmuInfo missing token/host_list: code={info_code} message={info_msg}"
            )
        login_valid = getattr(self.api, "login_valid", None)
        ws_uid = 0
        if login_valid is True and self.config.dede_user_id:
            try:
                ws_uid = int(self.config.dede_user_id)
            except (TypeError, ValueError):
                self.logger.warning(
                    "Room %s has an invalid DedeUserID; using anonymous danmu auth",
                    self.room_id,
                )
        elif self.config.dede_user_id:
            self.logger.warning(
                "Room %s Bilibili login is invalid; using anonymous danmu auth",
                self.room_id,
            )

        if self.config.live_max_hosts > 0:
            host_list = host_list[: self.config.live_max_hosts]
        schemes = ["wss", "ws"] if self.config.use_wss else ["ws", "wss"]
        uris: List[str] = []
        seen: set[str] = set()
        for scheme in schemes:
            for host_info in host_list:
                host = host_info.get("host")
                if not host:
                    continue
                if scheme == "wss":
                    port_candidates = [host_info.get("wss_port") or 443, 443]
                else:
                    port_candidates = [host_info.get("ws_port") or 80, 80]
                for port in port_candidates:
                    if not port:
                        continue
                    uri = f"{scheme}://{host}:{port}/sub"
                    if uri not in seen:
                        uris.append(uri)
                        seen.add(uri)

        last_exc: Optional[BaseException] = None
        ws_headers = {"Referer": f"https://live.bilibili.com/{self.room_id}"}
        if ws_uid:
            cookie_header = self.api._cookie_header()
            if cookie_header:
                ws_headers["Cookie"] = cookie_header
        proxy_value = (self.config.live_ws_proxy or "").strip()
        proxy_lower = proxy_value.lower()
        proxy_cycle: Optional[List[str]] = None
        if proxy_lower in {"", "none", "false", "off", "disable"}:
            proxy_setting: object = None
        elif proxy_lower in {"auto", "env", "true", "on"}:
            proxy_setting = True
            env_flags = {
                name: bool(os.environ.get(name))
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                )
            }
            self.logger.info("Room %s ws_proxy=auto env=%s", self.room_id, env_flags)
        elif proxy_lower in {"pool", "file", "proxy_pool"}:
            proxy_cycle = self._get_proxy_cycle()
            proxy_setting = proxy_cycle[0] if proxy_cycle else None
        else:
            proxy_setting = proxy_value
        connect_variants = [
            ("default", {}),
            ("no-compression", {"compression": None}),
            ("ipv4", {"family": socket.AF_INET}),
            ("ipv4-no-compression", {"compression": None, "family": socket.AF_INET}),
        ]

        proxy_cycle_size = len(proxy_cycle) if proxy_cycle else 0
        self.logger.info(
            "Room %s websocket proxy=%s use_wss=%s open_timeout=%s max_hosts=%s max_attempts=%s pool=%s",
            self.room_id,
            proxy_setting if proxy_setting is not True else "auto",
            self.config.use_wss,
            self.config.live_open_timeout,
            self.config.live_max_hosts,
            self.config.live_max_attempts,
            proxy_cycle_size,
        )

        attempt_count = 0
        for variant_name, variant_kwargs in connect_variants:
            for uri in uris:
                if self._stop_event.is_set():
                    return
                if proxy_cycle:
                    if not hasattr(self, "_proxy_index"):
                        self._proxy_index = 0
                    proxy_setting = proxy_cycle[self._proxy_index % len(proxy_cycle)]
                    self._proxy_index += 1
                if (
                    self.config.live_max_attempts > 0
                    and attempt_count >= self.config.live_max_attempts
                ):
                    if last_exc:
                        raise last_exc
                    raise RuntimeError("websocket connect attempts exhausted")
                attempt_count += 1
                proxy_label = (
                    "auto"
                    if proxy_setting is True
                    else (proxy_setting if proxy_setting else "none")
                )
                self.logger.info(
                    "Room %s connecting: %s (%s) proxy=%s",
                    self.room_id,
                    uri,
                    variant_name,
                    proxy_label,
                )
                auth_sent = False
                try:
                    connect = websockets.connect(
                        uri,
                        ping_interval=None,
                        origin="https://live.bilibili.com",
                        additional_headers=ws_headers,
                        user_agent_header=self.config.user_agent,
                        proxy=proxy_setting,
                        open_timeout=self.config.live_open_timeout,
                        **variant_kwargs,
                    )
                    connect_task = asyncio.create_task(connect.__aenter__())
                    if self.config.live_open_timeout > 0:
                        done, pending = await asyncio.wait(
                            {connect_task},
                            timeout=self.config.live_open_timeout,
                        )
                        if not done:
                            connect_task.cancel()
                            self.logger.warning(
                                "Room %s connect timeout after %ss uri=%s variant=%s",
                                self.room_id,
                                self.config.live_open_timeout,
                                uri,
                                variant_name,
                            )
                            continue
                    try:
                        ws = connect_task.result()
                    except Exception as exc:
                        with contextlib.suppress(Exception):
                            await connect.__aexit__(type(exc), exc, exc.__traceback__)
                        raise
                    try:
                        self._ws = ws
                        self._authed = False
                        self.logger.info(
                            "Room %s websocket connected: %s (%s) proxy=%s",
                            self.room_id,
                            uri,
                            variant_name,
                            proxy_label,
                        )
                        auth_body = {
                            "uid": ws_uid,
                            "roomid": self.room_id,
                            "protover": 3,
                            "buvid": self.config.buvid3 or "",
                            "platform": "web",
                            "type": 2,
                            "key": token,
                        }
                        await ws.send(self._pack(auth_body, op=7))
                        self.logger.debug("Room %s auth packet sent", self.room_id)
                        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                        auth_sent = True
                        try:
                            async for message in ws:
                                if isinstance(message, bytes):
                                    await self._handle_packet(message)
                        finally:
                            heartbeat_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await heartbeat_task
                        self.logger.warning(
                            "Room %s websocket closed: code=%s reason=%s",
                            self.room_id,
                            ws.close_code,
                            ws.close_reason,
                        )
                    finally:
                        self._ws = None
                        with contextlib.suppress(Exception):
                            await connect.__aexit__(None, None, None)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if auth_sent and not self._authed and ws_uid:
                        self.logger.warning(
                            "Room %s authenticated danmu identity was rejected; "
                            "retrying anonymously",
                            self.room_id,
                        )
                        ws_uid = 0
                        ws_headers.pop("Cookie", None)
                        self.api.login_valid = False

                    last_exc = exc
                    self.logger.warning(
                        "Room %s connect failed: %s (%s) uri=%s variant=%s",
                        self.room_id,
                        exc,
                        type(exc).__name__,
                        uri,
                        variant_name,
                        exc_info=True,
                    )

        if last_exc:
            raise last_exc

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.config.heartbeat_interval)
            try:
                await ws.send(self._pack({}, op=2))
            except Exception as exc:
                self.logger.warning(
                    "Room %s heartbeat error: %s (%s)",
                    self.room_id,
                    exc,
                    type(exc).__name__,
                    exc_info=True,
                )
                break

    async def _handle_packet(self, data: bytes) -> None:
        for op, body in self._unpack(data):
            if op == 5:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                asyncio.create_task(self._handle_event(payload))
            elif op == 8 and not self._authed:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = {}
                self._authed = True
                self.logger.info("Room %s auth ok: %s", self.room_id, payload)

    async def _handle_gift_event(self, payload: Dict[str, Any], cmd: str) -> None:
        data = payload.get("data") or {}
        # B站不同协议版本 giftName 的字段名可能不同
        gift_name = str(data.get("giftName") or data.get("gift_name") or "")

        # combo_num 是连击礼物时的数量
        num = self._safe_get_int(data.get("num") or data.get("combo_num"), 1)

        user_name = str(data.get("uname") or "")
        user_id = str(data.get("uid") or "")

        timestamp = time.time()

        coin_type = str(data.get("coin_type") or "")
        total_coin = self._safe_get_int(data.get("total_coin"), 0)

        # 1000 gold coins = 1 CNY
        price = 0
        if coin_type == "gold" and total_coin > 0:
            price = total_coin // 1000

        self.logger.info(
            f"Gift event processed: {gift_name} x{num} from {user_name} (Price: {price} CNY, cmd: {cmd})"
        )

        await self.adapter.handle_incoming_gift(
            room_id=self.room_id,
            gift_name=gift_name,
            num=num,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp,
            price=price,
        )

    async def _handle_superchat_event(self, payload: Dict[str, Any]) -> None:
        data = payload.get("data") or {}
        user_info = data.get("user_info") or {}
        user_name = str(user_info.get("uname") or "")
        user_id = str(data.get("uid") or "")
        message = str(data.get("message") or "")

        price = self._safe_get_int(data.get("price"), 0)

        timestamp = time.time()

        await self.adapter.handle_incoming_superchat(
            room_id=self.room_id,
            message_text=message,
            price=price,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp,
        )

    async def _handle_guard_event(self, payload: Dict[str, Any]) -> None:
        data = payload.get("data") or {}
        user_name = str(data.get("username") or "")
        user_id = str(data.get("uid") or "")

        num = self._safe_get_int(data.get("num"), 1)
        guard_level = self._safe_get_int(data.get("guard_level"), 3)
        gift_name = str(data.get("gift_name") or "舰长")

        timestamp = time.time()

        price_coin = self._safe_get_int(data.get("price"), 0)
        price = price_coin // 1000

        await self.adapter.handle_incoming_guard(
            room_id=self.room_id,
            guard_name=gift_name,
            num=num,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp,
            guard_level=guard_level,
            price=price,
        )

    async def _handle_event(self, payload: Dict[str, Any]) -> None:
        cmd = payload.get("cmd") or ""
        if cmd == "HEARTBEAT_REPLY":
            return

        self.logger.debug(f"Received command: {cmd}")

        if cmd.startswith("DANMU_MSG"):
            await self._handle_danmu_event(payload)
        elif cmd == "SEND_GIFT":
            await self._handle_gift_event(payload, cmd)
        elif cmd == "COMBO_SEND":
            await self._handle_gift_event(payload, cmd)
        elif cmd == "SUPER_CHAT_MESSAGE":
            await self._handle_superchat_event(payload)
        elif cmd == "GUARD_BUY":
            await self._handle_guard_event(payload)
        elif cmd.startswith("INTERACT_WORD"):
            await self._handle_interact_word_event(payload, cmd)

    async def _handle_interact_word_event(self, payload: Dict[str, Any], cmd: str = "") -> None:
        """Handle INTERACT_WORD / INTERACT_WORD_V2 events.

        V2 uses protobuf in data.pb (base64); V1 uses plain JSON fields.
        Only guard-level (大航海) user entries (msg_type=1, privilege>0) are forwarded.
        """
        data = payload.get("data") or {}
        pb_b64 = data.get("pb")

        if pb_b64:
            # --- V2 protobuf path ---
            parsed = self._decode_interact_word_pb(pb_b64)
            if parsed is None:
                self.logger.warning(
                    "INTERACT_WORD_V2 protobuf decode failed: room=%s", self.room_id
                )
                return
            uid, uname, msg_type, privilege_type, ts = parsed
        else:
            # --- V1 JSON fallback ---
            uid = str(data.get("uid") or "")
            uname = str(data.get("uname") or "")
            msg_type = self._safe_get_int(data.get("msg_type"), 0)
            privilege_type = self._safe_get_int(data.get("privilege_type"), 0)
            ts = self._safe_get_float(data.get("timestamp"), time.time())

        # Only process room entries (msg_type=1)
        if msg_type != 1:
            return

        # Only process guard-level users (skip regular viewers)
        if privilege_type <= 0:
            return

        guard_label = {1: "总督", 2: "提督", 3: "舰长"}.get(privilege_type, "舰长")
        self.logger.info(
            "Guard entry: room=%s user=%s(%s) level=%s(%s) -> dispatching",
            self.room_id,
            uname,
            uid,
            privilege_type,
            guard_label,
        )

        await self.adapter.handle_incoming_guard_entry(
            room_id=self.room_id,
            user_id=str(uid),
            user_name=str(uname),
            guard_level=privilege_type,
            timestamp=float(ts) if ts else time.time(),
        )

    @staticmethod
    def _decode_interact_word_pb(pb_b64: str):
        """Decode INTERACT_WORD_V2 protobuf.

        Returns (uid, uname, msg_type, privilege_type, timestamp) or None on failure.
        Protobuf field mapping (community-documented):
            1:  uid (varint)
            2:  uname (string)
            5:  msg_type (varint, 1=enter room)
            7:  timestamp (varint, seconds)
            16: privilege_type (varint, 0=none, 1=总督, 2=提督, 3=舰长)
        """
        import base64

        try:
            buf = base64.b64decode(pb_b64)
        except Exception:
            return None

        uid = 0
        uname = ""
        msg_type = 0
        privilege_type = 0
        timestamp = 0

        pos = 0
        buf_len = len(buf)
        try:
            while pos < buf_len:
                # Read tag
                tag = 0
                shift = 0
                while pos < buf_len:
                    b = buf[pos]
                    tag |= (b & 0x7F) << shift
                    pos += 1
                    if not (b & 0x80):
                        break
                    shift += 7
                field = tag >> 3
                wtype = tag & 7

                if wtype == 0:  # varint
                    val = 0
                    shift = 0
                    while pos < buf_len:
                        b = buf[pos]
                        val |= (b & 0x7F) << shift
                        pos += 1
                        if not (b & 0x80):
                            break
                        shift += 7
                    if field == 1:
                        uid = val
                    elif field == 5:
                        msg_type = val
                    elif field == 7:
                        timestamp = val
                    elif field == 16:
                        privilege_type = val
                elif wtype == 2:  # length-delimited
                    length = 0
                    shift = 0
                    while pos < buf_len:
                        b = buf[pos]
                        length |= (b & 0x7F) << shift
                        pos += 1
                        if not (b & 0x80):
                            break
                        shift += 7
                    val = buf[pos : pos + length]
                    pos += length
                    if field == 2:
                        try:
                            uname = val.decode("utf-8")
                        except Exception:
                            pass
                elif wtype == 1:  # fixed64
                    pos += 8
                elif wtype == 5:  # fixed32
                    pos += 4
                else:
                    break  # Unknown wire type, stop
        except Exception:
            pass  # Best-effort: return whatever we parsed so far

        return uid, uname, msg_type, privilege_type, timestamp

    async def _handle_danmu_event(self, payload: Dict[str, Any]) -> None:
        info = payload.get("info") or []
        if len(info) < 3:
            return
        message_text = str(info[1] or "")
        user_info = info[2] if isinstance(info[2], list) else []
        user_id = str(user_info[0] or "")
        user_name = str(user_info[1] or user_id)
        if (
            self.config.live_resolve_user_nickname
            and user_id
            and (not user_name or user_name == user_id)
        ):
            user_name = await self.adapter._resolve_user_nickname(user_id)
        timestamp_ms = 0
        if isinstance(info[0], list) and len(info[0]) > 4:
            timestamp_ms = int(info[0][4] or 0)
        message_id = self._extract_message_id(payload, info, timestamp_ms)
        reply_mid = ""
        reply_dmid = ""
        extra = self._extract_extra(info)
        if extra:
            reply_mid = str(extra.get("reply_mid") or "")
            reply_dmid = str(extra.get("reply_dmid") or extra.get("reply_id") or "")

        if self.adapter.is_self_danmu(self.room_id, user_id, message_id, message_text):
            if self.config.live_log_danmu:
                safe_text = _normalize_text(message_text)
                if len(safe_text) > 120:
                    safe_text = safe_text[:117] + "..."
                self.logger.info(
                    "Danmu ignored (self): room_id=%s user_id=%s message_id=%s text=%s",
                    self.room_id,
                    user_id,
                    message_id,
                    safe_text,
                )
            return
        if self.config.live_log_danmu:
            safe_text = _normalize_text(message_text)
            if len(safe_text) > 120:
                safe_text = safe_text[:117] + "..."
            self.logger.info(
                "Danmu received: room_id=%s user_id=%s message_id=%s text=%s",
                self.room_id,
                user_id,
                message_id,
                safe_text,
            )
        is_mentioned = self._should_mark_mention(message_text)
        if is_mentioned:
            self.logger.info(
                "Danmu mention detected: room_id=%s user_id=%s message_id=%s",
                self.room_id,
                user_id,
                message_id,
            )

        # Extract Guard Level (Index 7 in info list)
        # 0: None, 1: Governor (总督), 2: Admiral (提督), 3: Captain (舰长)
        guard_level = 0
        if len(info) > 7:
            guard_level = self._safe_get_int(info[7], 0)

        self.adapter.remember_danmu(self.room_id, message_id, user_id)
        await self.adapter.handle_incoming_danmu(
            room_id=self.room_id,
            message_id=message_id,
            text=message_text,
            user_id=user_id,
            user_name=user_name,
            timestamp=time.time(),
            reply_mid=reply_mid,
            reply_dmid=reply_dmid,
            is_mentioned=is_mentioned,
            guard_level=guard_level,
        )


    @staticmethod
    def _pack(body: Any, op: int) -> bytes:
        body_bytes = (
            json.dumps(body, separators=(",", ":")).encode("utf-8") if body else b""
        )
        packet_len = 16 + len(body_bytes)
        header = packet_len.to_bytes(4, "big")
        header += (16).to_bytes(2, "big")
        header += (1).to_bytes(2, "big")
        header += op.to_bytes(4, "big")
        header += (1).to_bytes(4, "big")
        return header + body_bytes

    def _unpack(self, data: bytes) -> List[Tuple[int, bytes]]:
        packets: List[Tuple[int, bytes]] = []
        offset = 0
        data_len = len(data)
        while offset + 16 <= data_len:
            packet_len = int.from_bytes(data[offset : offset + 4], "big")
            header_len = int.from_bytes(data[offset + 4 : offset + 6], "big")
            version = int.from_bytes(data[offset + 6 : offset + 8], "big")
            op = int.from_bytes(data[offset + 8 : offset + 12], "big")
            body = data[offset + header_len : offset + packet_len]
            offset += packet_len
            if version == 2:
                decompressed = zlib.decompress(body)
                packets.extend(self._unpack(decompressed))
            elif version == 3:
                decompressed = brotli.decompress(body)
                packets.extend(self._unpack(decompressed))
            else:
                packets.append((op, body))
        return packets

    @staticmethod
    def _extract_message_id(
        payload: Dict[str, Any], info: list, timestamp_ms: int
    ) -> str:
        msg_id = payload.get("msg_id")
        if msg_id:
            return str(msg_id)
        extra = LiveRoomWorker._extract_extra(info)
        if extra and extra.get("id_str"):
            return str(extra.get("id_str"))
        if timestamp_ms:
            return f"{timestamp_ms}-{uuid.uuid4().hex[:6]}"
        return uuid.uuid4().hex

    @staticmethod
    def _extract_extra(info: list) -> Optional[Dict[str, Any]]:
        try:
            extra_raw = info[0][15].get("extra")
        except Exception:
            return None
        if not extra_raw:
            return None
        try:
            return json.loads(extra_raw)
        except json.JSONDecodeError:
            return None
