"""Website ingestion: fetch a site, extract readable text, build the corpus.

MVP scope: the only supported corpus is a website reached by URL. There is no
file upload and no pasted text in the product flow; `app.scans.add_source`
stays as an internal low-level helper used by tests.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx

from app.config import get_settings
from app.errors import AppError

USER_AGENT = "StoryAuditBot/0.1 (+https://example.invalid/storyaudit)"
MAX_PAGE_BYTES = 2_000_000

SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "form", "nav", "footer", "header", "aside", "button", "select", "option",
}
BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "blockquote", "pre", "td", "th", "main", "figcaption",
}
HTML_TYPES = {"text/html", "application/xhtml+xml"}


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    paragraphs: list[str]

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.paragraphs: list[str] = []
        self.links: list[str] = []
        self._buffer: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in SKIP_TAGS:
            self._skip_depth = max(self._skip_depth - 1, 0)
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        self._buffer.append(data)

    def close(self) -> None:  # noqa: D401 - flush the trailing paragraph
        super().close()
        self._flush()

    def _flush(self) -> None:
        raw = " ".join("".join(self._buffer).split())
        if raw:
            self.paragraphs.append(raw)
        self._buffer = []


def normalize_site_url(raw: str) -> str:
    """Validate and canonicalize a user-supplied site URL."""
    value = (raw or "").strip()
    if not value:
        raise AppError(422, "validation_error", "URL обязателен")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise AppError(422, "validation_error", "Поддерживаются только http и https")
    if not parsed.hostname:
        raise AppError(422, "validation_error", "В URL отсутствует хост")
    if parsed.username or parsed.password:
        raise AppError(422, "validation_error", "Учётные данные в URL не поддерживаются")
    path = parsed.path or "/"
    return parsed._replace(path=path, fragment="", query="").geturl()


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise AppError(422, "validation_error", f"Не удалось разрешить хост: {host}") from None
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return True
    return False


def _check_host_allowed(url: str) -> None:
    host = urlparse(url).hostname or ""
    if get_settings().website_allow_private_hosts:
        return
    if _is_private_host(host):
        raise AppError(
            422, "validation_error",
            "Сайты во внутренней сети недоступны (WEBSITE_ALLOW_PRIVATE_HOSTS=false)",
        )


def _robots_parser(origin: str, timeout: float) -> robotparser.RobotFileParser | None:
    """Fetch robots.txt once per origin.

    A missing or unreachable robots.txt allows crawling (standard behaviour);
    an explicit Disallow rule is always honoured.
    """
    parser = robotparser.RobotFileParser()
    robots_url = urljoin(origin, "/robots.txt")
    try:
        response = httpx.get(
            robots_url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return None
        parser.parse(response.text.splitlines())
        return parser
    except httpx.HTTPError:
        return None


def _robots_allows(parser: robotparser.RobotFileParser | None, url: str) -> bool:
    return parser is None or parser.can_fetch(USER_AGENT, url)


def parse_html(html: str) -> tuple[str, list[str], list[str]]:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title = " ".join(parser.title.split())
    paragraphs = [p for p in parser.paragraphs if _is_content(p)]
    return title, paragraphs, parser.links


def _is_content(paragraph: str) -> bool:
    if len(paragraph) < 2 or len(paragraph) > 10_000:
        return False
    return any(char.isalnum() for char in paragraph)


def _same_site(candidate: str, root: str) -> bool:
    left, right = urlparse(candidate), urlparse(root)
    return (left.scheme, left.hostname, left.port or _default_port(left.scheme)) == (
        right.scheme, right.hostname, right.port or _default_port(right.scheme)
    )


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _read_body(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            break
        chunks.append(chunk)
    payload = b"".join(chunks)
    encoding = response.encoding or "utf-8"
    try:
        return payload.decode(encoding, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _fetch(client: httpx.Client, url: str) -> tuple[str, str] | None:
    try:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and content_type not in HTML_TYPES:
                return None
            final_url = str(response.url)
            _check_host_allowed(final_url)
            return final_url, _read_body(response)
    except httpx.HTTPError:
        return None


def crawl_site(
    raw_url: str,
    max_pages: int | None = None,
    *,
    respect_robots: bool | None = None,
    delay_seconds: float | None = None,
) -> list[Page]:
    """Fetch a website and return readable pages of the same origin."""
    settings = get_settings()
    start_url = normalize_site_url(raw_url)
    _check_host_allowed(start_url)
    budget = min(max_pages or settings.website_max_pages, settings.website_max_pages)
    respect = settings.website_respect_robots if respect_robots is None else respect_robots
    delay = settings.website_fetch_delay_seconds if delay_seconds is None else delay_seconds
    parsed_start = urlparse(start_url)
    origin = f"{parsed_start.scheme}://{parsed_start.netloc}"
    robots = _robots_parser(origin, settings.website_fetch_timeout_seconds) if respect else None

    pages: list[Page] = []
    seen: set[str] = {start_url}
    queue: list[str] = [start_url]
    deadline = time.monotonic() + settings.website_fetch_timeout_seconds * budget

    with httpx.Client(
        follow_redirects=True,
        timeout=settings.website_fetch_timeout_seconds,
    ) as client:
        while queue and len(pages) < budget and time.monotonic() < deadline:
            url = queue.pop(0)
            if not _robots_allows(robots, url):
                continue
            fetched = _fetch(client, url)
            if delay > 0:
                time.sleep(delay)
            if not fetched:
                continue
            final_url, html = fetched
            title, paragraphs, links = parse_html(html)
            if not paragraphs:
                continue
            pages.append(Page(url=final_url, title=title or url, paragraphs=paragraphs))
            for href in links:
                candidate, _ = urldefrag(urljoin(final_url, href))
                if candidate in seen or not candidate.startswith(("http://", "https://")):
                    continue
                if len(seen) >= budget * 6:
                    break
                if _same_site(candidate, start_url):
                    seen.add(candidate)
                    queue.append(candidate)

    if not pages:
        raise AppError(
            422, "validation_error",
            "Не удалось получить текст с сайта: страница недоступна, не HTML или не содержит текста",
        )
    return pages
