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
    pass


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
        raise FetchError(f"HTTP {exc.response.status_code} for {url}") from exc
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
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_extra_http_headers({"User-Agent": _USER_AGENT})
                page.goto(url, wait_until="networkidle", timeout=30_000)
                return page.content()
            finally:
                browser.close()
    except Exception as exc:
        raise FetchError(f"Playwright failed for {url}: {exc}") from exc


def fetch_page(url: str) -> str:
    """Fetch a listing page and return raw HTML.

    Tries a lightweight httpx request first. If the result looks like a
    JS-rendered skeleton (very short or missing listing keywords), retries
    with a headless Chromium browser via Playwright.

    Raises FetchError on any unrecoverable failure.
    """
    html = _fetch_httpx(url)
    text = _text_from_html(html)
    if _looks_like_skeleton(text):
        log.debug("Skeleton page detected (%d chars), switching to Playwright", len(text))
        html = _fetch_playwright(url)
    return html
