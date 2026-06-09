from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from flathunt import config
from flathunt.extract import FlatListing, extract_listing
from flathunt.fetch import FetchError, fetch_page
from flathunt.images import download_images, extract_image_urls, rate_images
from flathunt.sheets import SheetsClient

log = logging.getLogger(__name__)

_SEARCH_BASE = "https://www.spareroom.co.uk/flatshare/"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Search parameters matching the user's criteria.
_SEARCH_PARAMS: dict[str, str | int] = {
    "where":               "London",
    "flatshare_type":      "offered",   # whole flats, not rooms
    "furnished":           "Y",
    "per":                 "pcm",
    "min_rent":            2000,
    "max_rent":            2500,
    "available_from":      "2026-08-01",
    "available_search":    "Y",
    "showme_1beds":        "Y",         # 1-bedroom flats
    "showme_2beds":        "Y",         # 2-bedroom flats
    "min_term":            5,           # minimum 5 months (covers Aug→Dec)
    "mode":                "list",
}

# Minimum image quality score to include a listing.
_MIN_IMAGE_SCORE = 3

_FLATSHARE_ID_RE = re.compile(r"flatshare_id=(\d+)")


def _flatshare_id(url: str) -> str | None:
    """Extract the flatshare_id value from a SpareRoom URL for stable dedup."""
    m = _FLATSHARE_ID_RE.search(url)
    return m.group(1) if m else None


def _existing_ids(urls: set[str]) -> set[str]:
    """Return the set of flatshare_ids already present in the sheet."""
    ids = set()
    for url in urls:
        fid = _flatshare_id(url)
        if fid:
            ids.add(fid)
    return ids


@dataclass
class DiscoverStats:
    pages_scraped: int = 0
    urls_found: int = 0
    already_in_sheet: int = 0
    fetch_failed: int = 0
    filtered_out: int = 0
    low_score: int = 0
    added: int = 0
    failures: list[str] = field(default_factory=list)


def _search_page(offset: int) -> list[str]:
    """Fetch one search results page and return absolute listing URLs."""
    params = dict(_SEARCH_PARAMS)
    params["offset"] = offset

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(
                _SEARCH_BASE,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Search page fetch failed (offset=%d): %s", offset, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    urls: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        if "flatshare_detail.pl" in href and "flatshare_id=" in href:
            # Normalise to absolute URL, strip trailing search context params.
            absolute = urljoin("https://www.spareroom.co.uk", href.split("&search_results=")[0])
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)

    log.info("Search page offset=%d: found %d listing URLs", offset, len(urls))
    return urls


def passes_filters(listing: FlatListing) -> tuple[bool, str]:
    """Return (True, "") if the listing meets criteria, else (False, reason)."""
    # City check — must be in London.
    if listing.city is not None and listing.city.lower() != "london":
        return False, f"not in London (city={listing.city!r})"

    # Budget check (small buffer for borderline values).
    if listing.price_pcm is not None:
        if listing.price_pcm < 1_900 or listing.price_pcm > 2_600:
            return False, f"price £{listing.price_pcm:.0f} outside £1900–£2600 window"

    # Bedroom count (0 = studio — skip unless we can't tell).
    if listing.bedrooms is not None:
        if listing.bedrooms == 0:
            return False, "studio (0 bedrooms)"
        if listing.bedrooms > 2:
            return False, f"{listing.bedrooms} bedrooms (want 1–2)"

    return True, ""


def run(pages: int = 5, dry_run: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    sheet = SheetsClient()
    sheet.ensure_rating_header()
    existing_urls = sheet.get_all_urls()
    known_ids = _existing_ids(existing_urls)  # normalised flatshare_ids for dedup

    stats = DiscoverStats()

    # ── Collect listing URLs across all pages ────────────────────────────────
    all_urls: list[str] = []
    for page in range(pages):
        offset = page * 10
        found = _search_page(offset)
        all_urls.extend(found)
        stats.pages_scraped += 1
        if found:
            time.sleep(1)  # polite pause between search pages

    # Deduplicate across pages by flatshare_id (ignores query-string differences).
    seen_ids: set[str] = set()
    unique_urls: list[str] = []
    for u in all_urls:
        fid = _flatshare_id(u) or u
        if fid not in seen_ids:
            seen_ids.add(fid)
            unique_urls.append(u)
    stats.urls_found = len(unique_urls)
    log.info("Total unique URLs found: %d", stats.urls_found)

    # ── Process each new listing ─────────────────────────────────────────────
    new_urls = [u for u in unique_urls if (_flatshare_id(u) or u) not in known_ids]
    stats.already_in_sheet = stats.urls_found - len(new_urls)

    for i, url in enumerate(new_urls):
        if i > 0:
            time.sleep(2)

        log.info("── %d/%d  %s", i + 1, len(new_urls), url)

        # 1. Fetch page.
        try:
            html = fetch_page(url)
        except FetchError as exc:
            log.warning("  fetch failed: %s", exc)
            stats.fetch_failed += 1
            stats.failures.append(f"FETCH  {url}  ({exc})")
            continue

        # 2. Extract structured fields.
        listing = extract_listing(html, url)

        # 3. Apply hard filters.
        ok, reason = passes_filters(listing)
        if not ok:
            log.info("  filtered: %s", reason)
            stats.filtered_out += 1
            continue

        # 4. Download photos and rate with Gemini Vision.
        img_urls = extract_image_urls(html)
        images = download_images(img_urls, max_n=5)
        from flathunt.extract import clean_html
        listing_text = clean_html(html)
        score, notes = rate_images(images, listing_text)

        log.info(
            "  price=£%s  beds=%s  area=%s  city=%s  score=%d/5  %s",
            listing.price_pcm or "?",
            listing.bedrooms or "?",
            listing.area or "?",
            listing.city or "?",
            score,
            notes,
        )

        if score < _MIN_IMAGE_SCORE:
            log.info("  skipped (score %d < %d)", score, _MIN_IMAGE_SCORE)
            stats.low_score += 1
            continue

        # 5. Add to sheet.
        if dry_run:
            log.info("  [dry-run] would add row  rating=%d", score)
        else:
            row_num = sheet.append_row(url, listing, score)
            log.info("  added as row %d  rating=%d", row_num, score)

        stats.added += 1

    _print_summary(stats, dry_run)


def _print_summary(stats: DiscoverStats, dry_run: bool) -> None:
    label = "[DRY RUN] " if dry_run else ""
    sep = "─" * 48
    print(f"\n{sep}")
    print(f"  {label}Discovery summary")
    print(sep)
    print(f"  Pages scraped      : {stats.pages_scraped}")
    print(f"  Listings found     : {stats.urls_found}")
    print(f"  Already in sheet   : {stats.already_in_sheet}")
    print(f"  Fetch failures     : {stats.fetch_failed}")
    print(f"  Filtered out       : {stats.filtered_out}  (price / bedrooms)")
    print(f"  Low image score    : {stats.low_score}  (score < {_MIN_IMAGE_SCORE})")
    print(f"  {'Would add' if dry_run else 'Added'}              : {stats.added}")
    if stats.failures:
        print()
        for f in stats.failures:
            print(f"  {f}")
    print(sep)


if __name__ == "__main__":
    from flathunt.discover_cli import main
    main()
