"""Bounded Discord CDN downloads that honor the configured media proxy."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import aiohttp


ALLOWED_MEDIA_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=20.0, connect=5.0, sock_read=10.0)
MAX_FILE_SCHEMA_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DownloadedMedia:
    content: bytes
    content_type: str
    filename: str


class DiscordMediaFetcher:
    def __init__(self, *, proxy_url: str = "", session: aiohttp.ClientSession | None = None):
        self.proxy_url = proxy_url.strip() or None
        self._session = session
        self._owns_session = session is None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=DEFAULT_TIMEOUT,
                trust_env=False,
                connector=aiohttp.TCPConnector(limit=8),
            )
        return self._session

    @staticmethod
    def _safe_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.lower() in ALLOWED_MEDIA_HOSTS
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    async def download(
        self,
        url: str,
        *,
        max_bytes: int,
        expected_size: int | None = None,
        filename: str = "attachment",
    ) -> DownloadedMedia | None:
        if not self._safe_url(url) or max_bytes <= 0:
            return None
        if expected_size is not None and (
            isinstance(expected_size, bool)
            or expected_size < 0
            or expected_size > max_bytes
        ):
            return None
        session = await self._get_session()
        try:
            async with session.get(
                url,
                proxy=self.proxy_url,
                allow_redirects=False,
            ) as response:
                if response.status < 200 or response.status >= 300:
                    return None
                if response.content_length is not None and response.content_length > max_bytes:
                    return None
                if response.url.host.lower() not in ALLOWED_MEDIA_HOSTS:
                    return None
                content = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    if len(content) + len(chunk) > max_bytes:
                        return None
                    content.extend(chunk)
                if not content:
                    return None
                mime = str(response.headers.get("Content-Type", "application/octet-stream"))
                mime = mime.split(";", 1)[0].strip().lower() or "application/octet-stream"
                safe_name = str(filename or "attachment").replace("\x00", "")
                safe_name = safe_name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:128] or "attachment"
                return DownloadedMedia(bytes(content), mime, safe_name)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            return None

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None if self._owns_session else self._session
