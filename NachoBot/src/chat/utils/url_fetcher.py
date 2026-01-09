import html
import re
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
    title = None
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(title_match.group(1)).strip()

    cleaned = re.sub(r"<script[^>]*>.*?</script>", "", html_text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, title


def _truncate_for_log(text: str, max_len: int = 800) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "...[truncated]"


class UrlContentFetcher:
    def __init__(
        self,
        timeout_seconds: int = 45,
        max_bytes: int = 2_000_000,
        max_chars: int = 6000,
        min_text_chars: int = 200,
        user_agent: str = "NachoBot/1.0 (+https://github.com/Nacho-with-u/nachobot)",
        prefer_browser: bool = True,
        browser_timeout_seconds: int = 60,
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
            html_text = await self._fetch_with_browser(url)

        if html_text is None:
            html_text = await self._fetch_with_http(url)

        if html_text is None and not self.prefer_browser and _PLAYWRIGHT_AVAILABLE:
            html_text = await self._fetch_with_browser(url)

        if html_text is None:
            return None

        text, title = _strip_html(html_text)
        if not text:
            return None
        if not self.prefer_browser and _PLAYWRIGHT_AVAILABLE and len(text) < self.min_text_chars:
            browser_html = await self._fetch_with_browser(url)
            if browser_html:
                text, title = _strip_html(browser_html)

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

    async def _fetch_with_browser(self, url: str) -> Optional[str]:
        if not _PLAYWRIGHT_AVAILABLE or async_playwright is None:
            return None
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page(user_agent=self.user_agent)
                try:
                    await page.goto(url, wait_until="networkidle", timeout=self.browser_timeout_seconds * 1000)
                    return await page.content()
                finally:
                    await page.close()
                    await browser.close()
        except PlaywrightTimeoutError:
            logger.debug(f"Browser fetch timeout: {url}")
            return None
        except Exception as e:
            logger.debug(f"Browser fetch error: {url}, err={e}")
            return None

    async def build_url_info(self, urls: List[str]) -> str:
        if not urls:
            return ""
        lines = []
        for idx, url in enumerate(urls, 1):
            fetched = await self.fetch_text(url)
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
