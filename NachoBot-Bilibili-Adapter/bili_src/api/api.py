"""Bilibili API client and WBI signer for authentication."""

import base64
import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from bili_src.core.config import AdapterConfig


class WbiSigner:
    _mixin_key_enc_tab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
        61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
        36, 20, 34, 44, 52,
    ]

    def __init__(self, api: "BilibiliApi", logger: logging.Logger):
        self.api = api
        self.logger = logger
        self._img_key = ""
        self._sub_key = ""
        self._last_refresh = 0.0

    async def _refresh_keys(self) -> None:
        now = time.time()
        if now - self._last_refresh < 12 * 3600 and self._img_key and self._sub_key:
            return
        data = await self.api.request_json(
            "GET",
            "https://api.bilibili.com/x/web-interface/nav",
            params=None,
            data=None,
            use_wbi=False,
        )
        wbi_img = (data or {}).get("data", {}).get("wbi_img", {})
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            raise RuntimeError("Failed to fetch WBI keys")
        self._img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        self._sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        self._last_refresh = now

    def _get_mixin_key(self) -> str:
        raw = (self._img_key + self._sub_key).encode("utf-8")
        mixin = bytes(raw[i] for i in self._mixin_key_enc_tab)[:32]
        return mixin.decode("utf-8", errors="ignore")

    async def sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        await self._refresh_keys()
        mixin_key = self._get_mixin_key()
        wts = str(int(time.time()))
        params = {k: v for k, v in params.items() if v is not None}
        params["wts"] = wts
        sorted_items = sorted(params.items(), key=lambda x: x[0])
        filtered = {}
        for key, value in sorted_items:
            value_str = str(value)
            for ch in "!'()*":
                value_str = value_str.replace(ch, "")
            filtered[key] = value_str
        query = "&".join(
            f"{urllib.parse.quote(key, safe='')}"
            f"={urllib.parse.quote(str(value), safe='')}"
            for key, value in filtered.items()
        )
        sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        params["w_rid"] = sign
        return params


class BilibiliApi:
    def __init__(self, config: "AdapterConfig", logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.session: Optional[aiohttp.ClientSession] = None
        self.signer = WbiSigner(self, logger)

    async def start(self) -> None:
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=20)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    def _cookie_header(self) -> str:
        cookies = []
        if self.config.sessdata:
            cookies.append(f"SESSDATA={self.config.sessdata}")
        if self.config.bili_jct:
            cookies.append(f"bili_jct={self.config.bili_jct}")
        if self.config.buvid3:
            cookies.append(f"buvid3={self.config.buvid3}")
        if self.config.buvid4:
            cookies.append(f"buvid4={self.config.buvid4}")
        if self.config.dede_user_id:
            cookies.append(f"DedeUserID={self.config.dede_user_id}")
        return "; ".join(cookies)

    def _build_headers(self, referer: str = "https://www.bilibili.com/") -> Dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent,
            "Referer": referer,
        }
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def request_json(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_wbi: bool = False,
        referer: str = "https://www.bilibili.com/",
    ) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        final_params = params or {}
        if use_wbi:
            final_params = await self.signer.sign(final_params)
        headers = self._build_headers(referer=referer)
        async with self.session.request(
            method,
            url,
            params=final_params,
            data=data,
            headers=headers,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s body=%s",
                    resp.status,
                    url,
                    text[:200],
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                self.logger.warning(f"Non-JSON response from {url}: {text[:200]}")
                return {}
            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 0):
                    self.logger.warning(
                        "Bilibili API error: url=%s status=%s code=%s message=%s",
                        url,
                        resp.status,
                        code,
                        payload.get("message") or payload.get("msg"),
                    )
            return payload

    async def fetch_bytes(
        self, url: str, referer: str = "https://message.bilibili.com/"
    ) -> Optional[bytes]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        headers = self._build_headers(referer=referer)
        async with self.session.get(url, headers=headers) as resp:
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s",
                    resp.status,
                    url,
                )
                return None
            return await resp.read()

    async def fetch_base64(
        self, url: str, referer: str = "https://message.bilibili.com/"
    ) -> Optional[str]:
        data = await self.fetch_bytes(url, referer=referer)
        if not data:
            return None
        return base64.b64encode(data).decode("ascii")

    async def get_live_status(self, room_id: int) -> Optional[int]:
        payload = await self.request_json(
            "GET",
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": room_id},
            referer=f"https://live.bilibili.com/{room_id}",
        )
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return None
        status = data.get("live_status")
        try:
            return int(status)
        except (TypeError, ValueError):
            return None

    async def upload_dynamic_image(
        self,
        image_bytes: bytes,
        image_format: str = "",
        category: str = "daily",
    ) -> Dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP session not started")
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to upload images")
        if not image_bytes:
            return {}
        fmt = (image_format or "jpeg").lower()
        ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
        content_type = f"image/{fmt}" if fmt else "application/octet-stream"
        form = aiohttp.FormData()
        form.add_field(
            "file_up",
            image_bytes,
            filename=f"image.{ext}",
            content_type=content_type,
        )
        form.add_field("category", category)
        form.add_field("biz", "new_dyn")
        form.add_field("csrf", self.config.bili_jct)
        headers = self._build_headers(referer="https://t.bilibili.com/")
        async with self.session.post(
            "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
            data=form,
            headers=headers,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                self.logger.warning(
                    "HTTP error: status=%s url=%s body=%s",
                    resp.status,
                    "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs",
                    text[:200],
                )
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                self.logger.warning(
                    "Non-JSON response from upload_bfs: %s", text[:200]
                )
                return {}
            if isinstance(payload, dict):
                code = payload.get("code")
                if code not in (None, 0):
                    self.logger.warning(
                        "Upload image failed: code=%s message=%s",
                        code,
                        payload.get("message") or payload.get("msg"),
                    )
            return payload

    async def get_danmu_info(self, room_id: int) -> Dict[str, Any]:
        return await self.request_json(
            "GET",
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params={"id": room_id, "type": 0, "web_location": 444.8},
            use_wbi=False,
            referer=f"https://live.bilibili.com/{room_id}",
        )

    async def send_danmu(
        self,
        room_id: int,
        message: str,
        reply_mid: Optional[str] = None,
        reply_dmid: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send danmu")
        payload = {
            "roomid": room_id,
            "msg": message,
            "rnd": int(time.time()),
            "fontsize": 25,
            "color": 16777215,
            "mode": 1,
            "bubble": 0,
            "room_type": 0,
            "jumpfrom": 0,
            "statistics": '{"appId":100,"platform":5}',
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
        }
        if reply_mid:
            payload["reply_mid"] = reply_mid
        if reply_dmid:
            payload["reply_dmid"] = reply_dmid
        return await self.request_json(
            "POST",
            "https://api.live.bilibili.com/msg/send",
            data=payload,
            use_wbi=False,
            referer=f"https://live.bilibili.com/{room_id}",
        )

    async def send_comment_reply(
        self,
        comment_type: int,
        oid: int,
        message: str,
        root: Optional[int] = None,
        parent: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send comment replies")
        payload: Dict[str, Any] = {
            "type": comment_type,
            "oid": oid,
            "message": message,
            "plat": 1,
            "csrf": self.config.bili_jct,
        }
        if root is not None:
            payload["root"] = root
        if parent is not None:
            payload["parent"] = parent
        return await self.request_json(
            "POST",
            "https://api.bilibili.com/x/v2/reply/add",
            data=payload,
            use_wbi=False,
            referer="https://www.bilibili.com/",
        )

    async def get_reply_notifications(self, size: int) -> List[Dict[str, Any]]:
        params = {
            "build": 0,
            "mobi_app": "web",
        }
        resp = await self.request_json(
            "GET",
            "https://api.bilibili.com/x/msgfeed/reply",
            params=params,
            use_wbi=False,
        )
        items = (resp or {}).get("data", {}).get("items") or []
        return list(items)[:size]

    async def get_at_notifications(self, size: int) -> List[Dict[str, Any]]:
        params = {
            "build": 0,
            "mobi_app": "web",
        }
        resp = await self.request_json(
            "GET",
            "https://api.bilibili.com/x/msgfeed/at",
            params=params,
            use_wbi=False,
        )
        items = (resp or {}).get("data", {}).get("items") or []
        if (resp or {}).get("code") not in (None, 0):
            resp = await self.request_json(
                "GET",
                "https://api.vc.bilibili.com/x/im/web/msgfeed/at",
                params=params,
                use_wbi=False,
            )
            items = (resp or {}).get("data", {}).get("items") or []
        return list(items)[:size]

    async def get_user_info(self, mid: int) -> Dict[str, Any]:
        params = {
            "mid": mid,
            "jsonp": "jsonp",
        }
        return await self.request_json(
            "GET",
            "https://api.bilibili.com/x/space/acc/info",
            params=params,
            use_wbi=False,
        )

    async def get_sessions(
        self,
        session_type: int,
        size: int = 100,
        group_fold: int = 0,
        unfollow_fold: int = 0,
        sort_rule: int = 2,
    ) -> Dict[str, Any]:
        params = {
            "session_type": session_type,
            "group_fold": group_fold,
            "unfollow_fold": unfollow_fold,
            "sort_rule": sort_rule,
            "size": size,
            "build": 0,
            "mobi_app": "web",
        }
        return await self.request_json(
            "GET",
            "https://api.vc.bilibili.com/session_svr/v1/session_svr/get_sessions",
            params=params,
            use_wbi=False,
        )

    async def fetch_session_msgs(
        self,
        talker_id: int,
        session_type: int,
        size: int,
        begin_seqno: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "talker_id": talker_id,
            "session_type": session_type,
            "size": size,
            "sender_device_id": 1,
            "build": 0,
            "mobi_app": "web",
        }
        if begin_seqno is not None:
            params["begin_seqno"] = begin_seqno
        return await self.request_json(
            "GET",
            "https://api.vc.bilibili.com/svr_sync/v1/svr_sync/fetch_session_msgs",
            params=params,
            use_wbi=False,
        )

    async def send_private_message(
        self,
        talker_id: int,
        session_type: int,
        message: str,
        dev_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send private messages")
        if dev_id is None:
            dev_id = str(uuid.uuid4())
        sender_uid = self.config.dede_user_id
        content = json.dumps({"content": message}, ensure_ascii=False)
        params = {
            "w_sender_uid": sender_uid,
            "w_receiver_id": talker_id,
            "w_dev_id": dev_id,
        }
        payload = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": talker_id,
            "msg[receiver_type]": session_type,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[dev_id]": dev_id,
            "msg[timestamp]": int(time.time()),
            "msg[content]": content,
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
            "build": 0,
            "mobi_app": "web",
        }
        return await self.request_json(
            "POST",
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            params=params,
            data=payload,
            use_wbi=True,
            referer="https://message.bilibili.com/",
        )

    async def send_private_image_message(
        self,
        talker_id: int,
        session_type: int,
        content: Dict[str, Any],
        dev_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.config.bili_jct:
            raise RuntimeError("bili_jct is required to send private images")
        if dev_id is None:
            dev_id = str(uuid.uuid4())
        sender_uid = self.config.dede_user_id
        payload = {
            "msg[sender_uid]": sender_uid,
            "msg[receiver_id]": talker_id,
            "msg[receiver_type]": session_type,
            "msg[msg_type]": 2,
            "msg[msg_status]": 0,
            "msg[dev_id]": dev_id,
            "msg[timestamp]": int(time.time()),
            "msg[content]": json.dumps(content, ensure_ascii=False),
            "csrf": self.config.bili_jct,
            "csrf_token": self.config.bili_jct,
            "build": 0,
            "mobi_app": "web",
        }
        params = {
            "w_sender_uid": sender_uid,
            "w_receiver_id": talker_id,
            "w_dev_id": dev_id,
        }
        return await self.request_json(
            "POST",
            "https://api.vc.bilibili.com/web_im/v1/web_im/send_msg",
            params=params,
            data=payload,
            use_wbi=True,
            referer="https://message.bilibili.com/",
        )
