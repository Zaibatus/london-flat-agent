from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from flathunt.extract import FlatListing, clean_html, extract_listing
from flathunt.fetch import FetchError, fetch_page
from flathunt.images import download_images, extract_image_urls, rate_images
from flathunt.sheets import SheetsClient

log = logging.getLogger(__name__)

# Price tolerance: differences within ±£50 are ignored (rounding / pw→pcm variation).
_PRICE_TOLERANCE = 50
# Rating tolerance: flag if re-rated score differs by 2+ points.
_RATING_TOLERANCE = 2
# Minimum tenancy we require.
_MIN_TENANCY_MONTHS = 5
# Budget window (same as discover.py).
_MIN_PRICE = 1_900
_MAX_PRICE = 2_600


@dataclass
class _Issue:
    severity: Literal["error", "warn"]
    field: str
    sheet_val: str
    found_val: str
    note: str = ""


@dataclass
class _RowResult:
    row_num: int
    url: str
    area: str
    issues: list[_Issue] = field(default_factory=list)
    unreachable: bool = False


def _safe_float(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("£", "").strip())
    except (ValueError, AttributeError):
        return None


def _safe_int(s: str) -> int | None:
    try:
        return int(float(s.strip()))
    except (ValueError, AttributeError):
        return None


def _check_row(
    row_data: dict,
    listing: FlatListing,
    new_score: int,
) -> list[_Issue]:
    issues: list[_Issue] = []

    # 1. City must be London.
    if listing.city is not None and listing.city.lower() != "london":
        issues.append(_Issue(
            severity="error",
            field="city",
            sheet_val="London",
            found_val=listing.city,
            note="listing is not in London",
        ))

    # 2. Price checks.
    sheet_price = _safe_float(row_data.get("price_pcm", ""))
    if listing.price_pcm is not None:
        if listing.price_pcm < _MIN_PRICE or listing.price_pcm > _MAX_PRICE:
            issues.append(_Issue(
                severity="error",
                field="price_pcm",
                sheet_val=f"£{sheet_price:.0f}" if sheet_price else "—",
                found_val=f"£{listing.price_pcm:.0f}",
                note=f"outside budget window £{_MIN_PRICE}–£{_MAX_PRICE}",
            ))
        elif sheet_price is not None and abs(sheet_price - listing.price_pcm) > _PRICE_TOLERANCE:
            diff = listing.price_pcm - sheet_price
            issues.append(_Issue(
                severity="warn",
                field="price_pcm",
                sheet_val=f"£{sheet_price:.0f}",
                found_val=f"£{listing.price_pcm:.0f}",
                note=f"diff {diff:+.0f}",
            ))

    # 3. Minimum tenancy.
    if listing.min_tenancy_months is not None and listing.min_tenancy_months < _MIN_TENANCY_MONTHS:
        issues.append(_Issue(
            severity="error",
            field="min_tenancy",
            sheet_val=f"≥{_MIN_TENANCY_MONTHS} months",
            found_val=f"{listing.min_tenancy_months} months",
            note="below required minimum tenancy",
        ))

    # 4. Bedroom count drift.
    sheet_beds = _safe_int(row_data.get("bedrooms", ""))
    if sheet_beds is not None and listing.bedrooms is not None and sheet_beds != listing.bedrooms:
        issues.append(_Issue(
            severity="warn",
            field="bedrooms",
            sheet_val=str(sheet_beds),
            found_val=str(listing.bedrooms),
        ))

    # 5. Availability date drift.
    sheet_avail = (row_data.get("available_from") or "").strip()
    found_avail = (listing.available_from or "").strip()
    if sheet_avail and found_avail and sheet_avail.lower() != found_avail.lower():
        issues.append(_Issue(
            severity="warn",
            field="available_from",
            sheet_val=sheet_avail,
            found_val=found_avail,
        ))

    # 6. Image rating drift.
    sheet_rating = _safe_int(row_data.get("rating", ""))
    if sheet_rating is not None and abs(new_score - sheet_rating) >= _RATING_TOLERANCE:
        issues.append(_Issue(
            severity="warn",
            field="rating",
            sheet_val=f"{sheet_rating}/5",
            found_val=f"{new_score}/5",
            note=f"diff {new_score - sheet_rating:+d}",
        ))

    return issues


def run(limit: int | None = None, row: int | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    sheet = SheetsClient()
    data_rows = sheet.get_data_rows()

    # Filter to rows that have a URL.
    rows_with_url = [r for r in data_rows if r.get("link", "").startswith("http")]

    # Optionally restrict to a single row.
    if row is not None:
        rows_with_url = [r for r in rows_with_url if r["_row"] == row]
        if not rows_with_url:
            log.error("Row %d not found or has no URL.", row)
            return

    # Optionally cap the number of rows.
    if limit is not None:
        rows_with_url = rows_with_url[:limit]

    total = len(rows_with_url)
    log.info("Verifying %d rows...", total)

    results: list[_RowResult] = []

    for i, row_data in enumerate(rows_with_url):
        if i > 0:
            time.sleep(2)

        row_num = row_data["_row"]
        url = row_data["link"]
        area = row_data.get("area") or row_data.get("city") or "?"
        log.info("── %d/%d  row=%d  %s", i + 1, total, row_num, url)

        result = _RowResult(row_num=row_num, url=url, area=area)
        results.append(result)

        # Fetch page.
        try:
            html = fetch_page(url)
        except FetchError as exc:
            log.warning("  unreachable: %s", exc)
            result.unreachable = True
            result.issues.append(_Issue(
                severity="error",
                field="fetch",
                sheet_val="",
                found_val="",
                note=str(exc),
            ))
            continue

        # Re-extract fields.
        listing = extract_listing(html, url)

        # Re-rate images.
        img_urls = extract_image_urls(html)
        images = download_images(img_urls, max_n=5)
        new_score, _ = rate_images(images, clean_html(html))

        log.info(
            "  price=£%s  avail=%s  min_tenancy=%sm  beds=%s  rating=%s→%s",
            listing.price_pcm or "?",
            listing.available_from or "?",
            listing.min_tenancy_months if listing.min_tenancy_months is not None else "?",
            listing.bedrooms or "?",
            row_data.get("rating") or "?",
            new_score,
        )

        result.issues = _check_row(row_data, listing, new_score)

        if result.issues:
            for issue in result.issues:
                tag = "✗ error" if issue.severity == "error" else "⚠ warn "
                log.info("  %s  %-16s sheet=%s  found=%s  %s",
                         tag, issue.field, issue.sheet_val, issue.found_val, issue.note)
        else:
            log.info("  ✓ all clear")

    _print_report(results, total)


def _print_report(results: list[_RowResult], total: int) -> None:
    sep = "─" * 56
    errors = sum(
        1 for r in results
        for i in r.issues if i.severity == "error"
    )
    warns = sum(
        1 for r in results
        for i in r.issues if i.severity == "warn"
    )
    unreachable = sum(1 for r in results if r.unreachable)
    clean = sum(1 for r in results if not r.issues)

    rows_with_issues = [r for r in results if r.issues]

    print(f"\n{sep}")
    print(f"  Verification report — {total} rows checked")
    print(sep)

    if rows_with_issues:
        for result in rows_with_issues:
            short_url = result.url[:60] + "..." if len(result.url) > 60 else result.url
            print(f"\nRow {result.row_num}  {result.area}  {short_url}")
            for issue in result.issues:
                tag = "  ✗ error" if issue.severity == "error" else "  ⚠ warn "
                note = f"  ({issue.note})" if issue.note else ""
                if issue.field == "fetch":
                    print(f"{tag}   unreachable{note}")
                else:
                    print(f"{tag}   {issue.field:<16} sheet={issue.sheet_val}  found={issue.found_val}{note}")
    else:
        print("\n  All rows passed — no issues found.")

    print(f"\n{sep}")
    print(f"  Rows checked  : {total}")
    print(f"  All clear     : {clean}")
    print(f"  Warnings      : {warns}  (value drift)")
    print(f"  Errors        : {errors}  (criteria violations)")
    print(f"  Unreachable   : {unreachable}")
    print(sep)


if __name__ == "__main__":
    from flathunt.verify_cli import main
    main()
