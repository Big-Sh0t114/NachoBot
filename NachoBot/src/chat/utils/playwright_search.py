import base64
import re
from typing import Dict, List, Sequence
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup

from src.chat.utils.url_fetcher import UrlContentFetcher
from src.common.logger import get_logger

logger = get_logger("playwright_search")


class PlaywrightSearchProvider:
    """使用共享 Playwright 浏览器直接查询公开搜索引擎。"""

    _SUPPORTED_ENGINES = ("bing", "duckduckgo")
    _CJK_QUERY_NOISE = (
        "帮我查一下",
        "帮我查询",
        "请帮我查",
        "多少钱",
        "怎么样",
        "是什么",
        "最新消息",
        "今日",
        "今天",
        "现在",
        "当前",
        "目前",
        "最新",
        "实时",
        "查询",
        "搜索",
        "请问",
        "帮我",
        "一下",
    )
    _LATIN_QUERY_NOISE = {
        "current",
        "latest",
        "official",
        "search",
        "today",
        "website",
    }

    def __init__(
        self,
        engines: Sequence[str] = _SUPPORTED_ENGINES,
        timeout_seconds: int = 20,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    ):
        normalized_engines = []
        for engine in engines:
            normalized = str(engine).strip().lower()
            if normalized in self._SUPPORTED_ENGINES and normalized not in normalized_engines:
                normalized_engines.append(normalized)
            elif normalized:
                logger.warning(f"忽略不支持的浏览器搜索引擎: {normalized}")

        self.engines = tuple(normalized_engines) or self._SUPPORTED_ENGINES
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.user_agent = user_agent

    @staticmethod
    def is_available() -> bool:
        """搜索 Provider 始终可用；Playwright 缺失时由 HTTP fallback 承担搜索。"""
        return True

    @staticmethod
    def browser_available() -> bool:
        """仅表示 Playwright 浏览器后端是否可用。"""
        return UrlContentFetcher.browser_available()

    async def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        query = query.strip()
        if not query or max_results <= 0:
            return []
        if not self.browser_available():
            logger.warning("Playwright 不可用，将使用 HTTP fallback 执行搜索")

        for engine in self.engines:
            try:
                results = await self._search_engine(engine, query, max_results)
            except Exception as e:
                logger.warning(f"浏览器搜索失败: engine={engine}, err={e}")
                continue
            if results:
                logger.info(f"浏览器搜索成功: engine={engine}, result_count={len(results)}")
                return results
            logger.info(f"浏览器搜索无结果，尝试下一个引擎: engine={engine}")

        return []

    async def _search_engine(self, engine: str, query: str, max_results: int) -> List[Dict[str, str]]:
        search_url = self._build_search_url(engine, query, max_results)
        html_text = ""

        if self.browser_available():
            try:
                browser = await UrlContentFetcher.get_shared_browser()
                page = await browser.new_page(user_agent=self.user_agent)
                try:
                    try:
                        await page.goto(
                            search_url,
                            wait_until="domcontentloaded",
                            timeout=self.timeout_seconds * 1000,
                        )
                    except Exception as e:
                        # 超时时页面经常已经包含可解析的结果，因此继续读取当前 DOM。
                        logger.debug(f"搜索页导航未完整完成: engine={engine}, err={e}")
                    html_text = await page.content()
                finally:
                    await page.close()
            except Exception as e:
                logger.warning(f"Playwright 搜索不可用，切换 HTTP fallback: engine={engine}, err={e}")

        if not html_text:
            fetcher = UrlContentFetcher(
                timeout_seconds=self.timeout_seconds,
                user_agent=self.user_agent,
                prefer_browser=False,
            )
            html_text = await fetcher._fetch_with_http(search_url) or ""
            if html_text:
                logger.info(f"HTTP fallback 获取搜索页成功: engine={engine}")
            else:
                logger.warning(f"HTTP fallback 获取搜索页失败: engine={engine}")
                return []

        results = self.parse_results(html_text, engine, max_results)
        if results and not self._results_match_query(query, results):
            logger.warning(f"搜索结果与查询不匹配，丢弃异常结果页: engine={engine}, query={query}")
            return []

        return results

    @staticmethod
    def _build_search_url(engine: str, query: str, max_results: int) -> str:
        encoded_query = quote_plus(query)
        if engine == "bing":
            return (
                f"https://cn.bing.com/search?q={encoded_query}&count={max_results}&ensearch=0&setlang=zh-hans"
            )
        if engine == "duckduckgo":
            return f"https://html.duckduckgo.com/html/?q={encoded_query}"
        raise ValueError(f"不支持的浏览器搜索引擎: {engine}")

    @classmethod
    def parse_results(cls, html_text: str, engine: str, max_results: int = 5) -> List[Dict[str, str]]:
        if not html_text or max_results <= 0:
            return []

        soup = BeautifulSoup(html_text, "html.parser")
        if engine == "bing":
            raw_results = cls._parse_bing_results(soup)
        elif engine == "duckduckgo":
            raw_results = cls._parse_duckduckgo_results(soup)
        else:
            return []

        results: List[Dict[str, str]] = []
        seen_urls = set()
        for item in raw_results:
            url = cls._normalize_result_url(item.get("url", ""), engine)
            if not url or url in seen_urls:
                continue
            title = item.get("title", "").strip()
            snippet = item.get("snippet", "").strip()
            if not title and not snippet:
                continue
            seen_urls.add(url)
            results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break
        return results

    @staticmethod
    def _parse_bing_results(soup: BeautifulSoup) -> List[Dict[str, str]]:
        results = []
        for node in soup.select("li.b_algo"):
            link = node.select_one("h2 a")
            if link is None:
                continue
            snippet_node = node.select_one(".b_caption p, .b_snippet, .b_lineclamp2, p")
            results.append(
                {
                    "title": link.get_text(" ", strip=True),
                    "url": str(link.get("href") or ""),
                    "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
                }
            )
        return results

    @staticmethod
    def _parse_duckduckgo_results(soup: BeautifulSoup) -> List[Dict[str, str]]:
        results = []
        for node in soup.select(".result"):
            link = node.select_one("a.result__a, .result__title a")
            if link is None:
                continue
            snippet_node = node.select_one(".result__snippet")
            results.append(
                {
                    "title": link.get_text(" ", strip=True),
                    "url": str(link.get("href") or ""),
                    "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
                }
            )
        return results

    @staticmethod
    def _normalize_result_url(url: str, engine: str) -> str:
        normalized = url.strip()
        if not normalized:
            return ""
        if normalized.startswith("//"):
            normalized = f"https:{normalized}"

        parsed = urlparse(normalized)
        if engine == "duckduckgo" and PlaywrightSearchProvider._is_host_or_subdomain(parsed, "duckduckgo.com"):
            redirect_target = parse_qs(parsed.query).get("uddg")
            if redirect_target:
                normalized = redirect_target[0]
                parsed = urlparse(normalized)

        if engine == "bing" and PlaywrightSearchProvider._is_host_or_subdomain(parsed, "bing.com"):
            redirect_target = PlaywrightSearchProvider._decode_bing_redirect(parsed)
            if redirect_target:
                normalized = redirect_target
                parsed = urlparse(normalized)

        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return ""
        return normalized

    @staticmethod
    def _is_host_or_subdomain(parsed_url, domain: str) -> bool:
        host = (parsed_url.hostname or "").casefold().rstrip(".")
        domain = domain.casefold()
        return host == domain or host.endswith(f".{domain}")

    @staticmethod
    def _decode_bing_redirect(parsed_url) -> str:
        encoded_target = parse_qs(parsed_url.query).get("u")
        if not encoded_target:
            return ""

        encoded = encoded_target[0]
        if not encoded.startswith("a1"):
            return ""

        payload = encoded[2:]
        payload += "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

        target = urlparse(decoded)
        if target.scheme not in ("http", "https") or not target.netloc:
            return ""
        return decoded

    @classmethod
    def _query_relevance_tokens(cls, query: str) -> List[str]:
        normalized_query = query.casefold()
        for noise in cls._CJK_QUERY_NOISE:
            normalized_query = normalized_query.replace(noise, " ")

        raw_tokens = re.findall(r"[a-z0-9][a-z0-9._+-]+|[\u4e00-\u9fff]+", normalized_query)
        relevance_tokens = []
        for token in raw_tokens:
            if token in cls._LATIN_QUERY_NOISE:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
                relevance_tokens.extend(token[index : index + 2] for index in range(len(token) - 1))
            else:
                relevance_tokens.append(token)

        # 保持顺序去重，避免一个重复词被多次计入命中数。
        return list(dict.fromkeys(relevance_tokens))

    @classmethod
    def _results_match_query(cls, query: str, results: List[Dict[str, str]]) -> bool:
        relevance_tokens = cls._query_relevance_tokens(query)

        if not relevance_tokens:
            return True

        result_text = " ".join(
            f"{item.get('title', '')} {item.get('snippet', '')} {item.get('url', '')}" for item in results
        ).casefold()
        matched_count = sum(token in result_text for token in relevance_tokens)
        required_matches = 1 if len(relevance_tokens) == 1 else 2
        return matched_count >= required_matches
