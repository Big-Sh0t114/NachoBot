import asyncio
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

import aiohttp

from src.common.logger import get_logger

logger = get_logger("url_fetcher")

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

    _PLAYWRIGHT_AVAILABLE = True
except Exception:
    async_playwright = None
    PlaywrightTimeoutError = Exception
    _PLAYWRIGHT_AVAILABLE = False


_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def extract_urls(text: str, max_urls: int = 2) -> List[str]:
    if not text:
        return []
    matches = _URL_PATTERN.findall(text)
    seen = set()
    urls = []
    for url in matches:
        cleaned = url.rstrip(").,;\"'")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
        if len(urls) >= max_urls:
            break
    return urls


def _strip_html(html_text: str) -> Tuple[str, Optional[str]]:
    parser = _HTMLTextExtractor()
    parser.feed(html_text)
    parser.close()
    cleaned = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip() or None
    return cleaned, title


def _truncate_for_log(text: str, max_len: int = 800) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "...[truncated]"


class UrlContentFetcher:
    # --- Playwright 浏览器单例 ---
    _browser = None
    _playwright_instance = None
    _browser_lock = asyncio.Lock()

    def __init__(
        self,
        timeout_seconds: int = 45,
        max_bytes: int = 2_000_000,
        max_chars: int = 6000,
        min_text_chars: int = 200,
        user_agent: str = "NachoBot/1.0 (+https://github.com/Nacho-with-u/nachobot)",
        prefer_browser: bool = False,
        browser_timeout_seconds: int = 30,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_chars = max_chars
        self.min_text_chars = min_text_chars
        self.user_agent = user_agent
        self.prefer_browser = prefer_browser
        self.browser_timeout_seconds = browser_timeout_seconds

    async def fetch_text(self, url: str) -> Optional[Tuple[str, Optional[str]]]:
        html_text: Optional[str] = None

        if self.prefer_browser and _PLAYWRIGHT_AVAILABLE:
            # 浏览器优先模式：先用浏览器，失败则 HTTP fallback
            html_text = await self._fetch_with_browser(url)
            if html_text is None:
                html_text = await self._fetch_with_http(url)
        else:
            # HTTP 优先模式：先用 HTTP，内容不足时再尝试浏览器（仅一次）
            html_text = await self._fetch_with_http(url)
            if html_text is not None:
                text, title = _strip_html(html_text)
                if text and len(text) >= self.min_text_chars:
                    # HTTP 内容足够，直接返回
                    if len(text) > self.max_chars:
                        text = text[: self.max_chars].rstrip() + "..."
                    return text, title
                # HTTP 内容不足，尝试浏览器补充
                if _PLAYWRIGHT_AVAILABLE:
                    browser_html = await self._fetch_with_browser(url)
                    if browser_html:
                        html_text = browser_html
            elif _PLAYWRIGHT_AVAILABLE:
                html_text = await self._fetch_with_browser(url)

        if html_text is None:
            return None

        text, title = _strip_html(html_text)
        if not text:
            return None

        if len(text) > self.max_chars:
            text = text[: self.max_chars].rstrip() + "..."
        return text, title

    async def _fetch_with_http(self, url: str) -> Optional[str]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {"User-Agent": self.user_agent}
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        logger.debug(f"URL fetch failed: {url}, status={resp.status}")
                        return None
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    if "text" not in content_type and "html" not in content_type:
                        logger.debug(f"URL content type not text/html: {url}, type={content_type}")
                        return None

                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) >= self.max_bytes:
                            break
                    encoding = resp.charset or "utf-8"
                    return body.decode(encoding, errors="ignore")
        except Exception as e:
            logger.debug(f"URL fetch error: {url}, err={e}")
            return None

    @staticmethod
    def browser_available() -> bool:
        """返回当前环境是否可以使用 Playwright。"""
        return _PLAYWRIGHT_AVAILABLE and async_playwright is not None

    @classmethod
    async def get_shared_browser(cls):
        """获取或创建共享的浏览器实例（单例模式）"""
        if not cls.browser_available():
            raise RuntimeError("Playwright 不可用，无法创建浏览器实例")

        async with cls._browser_lock:
            if cls._browser is not None:
                try:
                    if cls._browser.is_connected():
                        return cls._browser
                except Exception:
                    pass
                # 浏览器已断连，重新创建
                cls._browser = None

            if cls._playwright_instance is None:
                cls._playwright_instance = await async_playwright().start()
            cls._browser = await cls._playwright_instance.chromium.launch(headless=True)
            logger.info("已创建共享 Playwright 浏览器实例")
            return cls._browser

    async def _fetch_with_browser(self, url: str) -> Optional[str]:
        if not _PLAYWRIGHT_AVAILABLE or async_playwright is None:
            return None
        try:
            browser = await self.get_shared_browser()
            page = await browser.new_page(user_agent=self.user_agent)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.browser_timeout_seconds * 1000)
                return await page.content()
            finally:
                await page.close()
        except PlaywrightTimeoutError:
            logger.debug(f"Browser fetch timeout: {url}")
            return None
        except Exception as e:
            logger.debug(f"Browser fetch error: {url}, err={e}")
            return None

    async def build_url_info(self, urls: List[str]) -> str:
        if not urls:
            return ""
        # 并行抓取所有 URL
        tasks = [self.fetch_text(url) for url in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        lines = []
        for idx, (url, fetched) in enumerate(zip(urls, results, strict=True), 1):
            if isinstance(fetched, Exception):
                logger.debug(f"URL抓取异常: {url}, err={fetched}")
                continue
            if not fetched:
                continue
            text, title = fetched
            header = f"{idx}. {title}" if title else f"{idx}. {url}"
            lines.append(header)
            lines.append(text)
        content = "\n".join(lines)
        if content:
            logger.info(f"网页解析内容: {_truncate_for_log(content)}")
        return content
