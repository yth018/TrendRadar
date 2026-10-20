"""Read public news content using the project's existing Jina Reader tool."""

import html
import ipaddress
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from mcp_server.tools.article_reader import ArticleReaderTools, BATCH_INTERVAL

MAX_CONTENT_CHARS = 14000
REQUEST_TIMEOUT = 20


class _Reader(ArticleReaderTools):
    def __init__(self):
        super().__init__()
        self._start_lock = threading.Lock()

    def _throttle(self):
        # All workers share the original five-second start interval.
        with self._start_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < BATCH_INTERVAL:
                time.sleep(BATCH_INTERVAL - elapsed)
            self._last_request_time = time.monotonic()

    def _build_headers(self):
        headers = super()._build_headers()
        headers.update({"X-Remove-Selector": "nav, footer, aside, script, style",
                        "X-Retain-Images": "none"})
        return headers


def _public_url(url):
    if not isinstance(url, str) or any(ch.isspace() for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("http", "https") or parsed.username is not None or parsed.password is not None:
            return False
        if not host or parsed.port not in (None, 80, 443):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".invalid", ".test")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return ("." in host and not host.replace(".", "").isdigit()
                    and not re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host)
                    and "%" not in host and "\\" not in host)
    except ValueError:
        return False


def _clean_content(markdown):
    if not isinstance(markdown, str):
        return ""
    body = markdown.split("Markdown Content:", 1)[-1]
    body = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", body)
    body = re.sub(r"\[([^\]\n]*)\]\([^\n]*?\)", r"\1", body)
    body = re.sub(r"https?://[^\s<>]+", "", body)
    body = html.unescape(body)
    lines = []
    for line in body.splitlines():
        line = re.sub(r"^[\s#>*\-]+", "", line).strip()
        line = re.sub(r"[*_`]+", "", line)
        if line and not re.match(r"^(Title|URL Source|Published Time):", line):
            lines.append(line)
    # Navigation/login-only pages lack sustained prose, even if their HTML is long.
    prose = [line for line in lines if len(re.sub(r"\W", "", line)) >= 60
             and re.search(r"[。！？.!?；;，,]", line)]
    if sum(len(line) for line in prose) < 120:
        return ""
    text = "\n\n".join(lines)
    if len(text) < 1000 and re.search(
        r"(verify (?:you are|that you are) human|access denied|captcha|验证码|访问过于频繁|安全验证)",
        text, re.I,
    ):
        return ""
    return text[:MAX_CONTENT_CHARS]


def _matching_toutiao_article(markdown, title):
    expected = re.sub(r"\W", "", title).lower()
    if len(expected) < 6:
        return None
    best = (0.0, None)
    for match in re.finditer(r'https?://(?:www\.)?toutiao\.com/article/\d+/?(?:\s+"([^"\n]+)")?', markdown):
        url = match.group(0).split('"', 1)[0].strip()
        label = match.group(1) or markdown[max(0, match.start()-180):match.start()]
        label = re.sub(r"\W", "", label).lower()
        score = 1.0 if expected in label else SequenceMatcher(None, expected, label).ratio()
        if score >= .55 and score > best[0]:
            best = (score, url)
    return best[1]


def _fetch_one(reader, entry):
    url = entry.get("url", "")
    unavailable = {"content": "", "url": url, "status": "unavailable"}
    if not _public_url(url):
        return unavailable
    try:
        result = reader.read_article(url, timeout=REQUEST_TIMEOUT)
        if not result.get("success"):
            return unavailable
        markdown = result.get("data", {}).get("content", "")
        parsed = urlsplit(url)
        if parsed.hostname in ("toutiao.com", "www.toutiao.com") and parsed.path.startswith("/trending/"):
            article_url = _matching_toutiao_article(markdown, entry.get("title", ""))
            if not article_url or not _public_url(article_url):
                return unavailable
            result = reader.read_article(article_url, timeout=REQUEST_TIMEOUT)
            if not result.get("success"):
                return unavailable
            url = article_url
            markdown = result.get("data", {}).get("content", "")
        content = _clean_content(markdown)
        if not content:
            return unavailable
        return {"content": content, "url": url, "status": "ok"}
    except Exception:
        # Reader failures may contain request details; report only availability.
        return unavailable


def fetch_article_contents(entries):
    """Read at most two URLs concurrently, preserving input order without retries."""
    if not entries:
        return []
    reader = _Reader()
    results = [None] * len(entries)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {executor.submit(_fetch_one, reader, entry): index for index, entry in enumerate(entries)}
        for done, future in enumerate(as_completed(pending), start=1):
            results[pending[future]] = future.result()
            if done % 10 == 0 or done == len(entries):
                print(f"[正文读取] 已完成 {done}/{len(entries)} 条", flush=True)
    return results
