from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Signals that indicate a real listing page (not a JS-rendered skeleton).
_LISTING_KEYWORDS = {"£", "bedroom", "bed ", "sq ft", "sqm", "per week", "per month", "pcm"}


class FetchError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _looks_like_skeleton(text: str) -> bool:
    """Return True if the extracted text looks like a JS-rendered skeleton page."""
    if len(text) < 2_000:
        return True
    lower = text.lower()
    return not any(kw in lower for kw in _LISTING_KEYWORDS)


def _fetch_httpx(url: str) -> str:
    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"HTTP {exc.response.status_code} for {url}",
            status_code=exc.response.status_code,
        ) from exc
    except httpx.TimeoutException as exc:
        raise FetchError(f"Timeout fetching {url}") from exc
    except httpx.RequestError as exc:
        raise FetchError(f"Request error for {url}: {exc}") from exc


def _text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(separator=" ", strip=True)


def _fetch_playwright(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        raise FetchError(
            "Playwright is not installed or browsers are missing. "
            "Run: playwright install chromium"
        )

    log.info("Falling back to Playwright for %s", url)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-GB",
                )
                page = context.new_page()
                # Patch the webdriver flag that Cloudflare and others detect.
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                # "load" fires once the page and its sub-resources are ready.
                # "networkidle" never arrives on portals with continuous background XHR.
                page.goto(url, wait_until="load", timeout=45_000)
                page.wait_for_timeout(2_000)  # let JS hydrate the listing content
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        raise FetchError(f"Playwright failed for {url}: {exc}") from exc


# These status codes indicate bot-blocking rather than a real error — worth retrying
# with a full browser. 404 and 5xx are real failures; don't waste a Playwright launch.
_PLAYWRIGHT_RETRY_CODES = {403, 429, 503}


def fetch_page(url: str) -> str:
    """Fetch a listing page and return raw HTML.

    Tries a lightweight httpx request first. Falls back to Playwright when:
    - The server returns a bot-blocking status (403, 429, 503), or
    - The response body looks like a JS-rendered skeleton.

    Raises FetchError on any unrecoverable failure.
    """
    try:
        html = _fetch_httpx(url)
    except FetchError as exc:
        if exc.status_code in _PLAYWRIGHT_RETRY_CODES:
            log.info("HTTP %d from %s — retrying with Playwright", exc.status_code, url)
            return _fetch_playwright(url)
        raise

    text = _text_from_html(html)
    if _looks_like_skeleton(text):
        log.debug("Skeleton page detected (%d chars), switching to Playwright", len(text))
        html = _fetch_playwright(url)
    return html
