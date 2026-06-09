from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from flathunt import config
from flathunt.extract import FlatListing, extract_listing
from flathunt.fetch import FetchError, fetch_page
from flathunt.sheets import SheetsClient

log = logging.getLogger(__name__)

# Maps each FlatListing field name to its sheet column number.
_FIELD_TO_COL: dict[str, int] = {f: config.COL[f] for f in config.WRITABLE_FIELDS}


@dataclass
class RunStats:
    rows_processed: int = 0
    cells_written: int = 0
    rows_failed: int = 0
    failures: list[tuple[int, str, str]] = field(default_factory=list)


def _coerce_for_sheet(field_name: str, value: object) -> str | int | float | None:
    """Normalise extracted values before writing to the sheet."""
    if value is None:
        return None
    if field_name == "sqm" and isinstance(value, (int, float)):
        return round(float(value), 1)
    if field_name == "price_pcm" and isinstance(value, (int, float)):
        return int(round(float(value)))
    if field_name == "bedrooms" and isinstance(value, float):
        return int(value)
    return value  # type: ignore[return-value]


def _enrich_row(
    sheet: SheetsClient,
    row_num: int,
    url: str,
    existing: dict[str, str],
    force: bool,
    stats: RunStats,
) -> None:
    log.info("Row %d: %s", row_num, url)

    try:
        html = fetch_page(url)
    except FetchError as exc:
        log.warning("Row %d: fetch failed — %s", row_num, exc)
        stats.rows_failed += 1
        stats.failures.append((row_num, url, str(exc)))
        return
    except Exception as exc:
        log.warning("Row %d: unexpected error during fetch — %s", row_num, exc)
        stats.rows_failed += 1
        stats.failures.append((row_num, url, f"Unexpected: {exc}"))
        return

    listing: FlatListing = extract_listing(html, url)
    stats.rows_processed += 1

    for field_name, col_num in _FIELD_TO_COL.items():
        extracted = getattr(listing, field_name)
        if extracted is None:
            continue

        existing_val = existing.get(field_name, "").strip()
        if existing_val and not force:
            log.debug("Row %d col %d (%s): skipping — cell already filled", row_num, col_num, field_name)
            continue

        sheet_val = _coerce_for_sheet(field_name, extracted)
        if sheet_val is None:
            continue

        sheet.write_cell(row_num, col_num, sheet_val)
        log.info("  wrote %s = %r", field_name, sheet_val)
        stats.cells_written += 1


def run(
    force: bool = False,
    limit: int | None = None,
    row: int | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    sheet = SheetsClient()
    data_rows = sheet.get_data_rows()

    # Only rows where column A contains a URL.
    candidates = [r for r in data_rows if r["link"].strip().lower().startswith("http")]

    if row is not None:
        candidates = [r for r in candidates if r["_row"] == row]
        if not candidates:
            log.error(
                "Row %d not found or has no URL in column A. "
                "Remember: row 1 is the header; data rows start at 2.",
                row,
            )
            return

    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        print("No rows to process.")
        return

    stats = RunStats()

    for i, data in enumerate(candidates):
        if i > 0:
            time.sleep(2)  # polite rate-limiting between requests
        _enrich_row(sheet, data["_row"], data["link"].strip(), data, force, stats)

    _print_summary(stats)


def _print_summary(stats: RunStats) -> None:
    sep = "─" * 44
    print(f"\n{sep}")
    print("  Run summary")
    print(sep)
    print(f"  Rows processed : {stats.rows_processed}")
    print(f"  Cells written  : {stats.cells_written}")
    print(f"  Failures       : {stats.rows_failed}")
    if stats.failures:
        print()
        for row_num, url, reason in stats.failures:
            print(f"  Row {row_num}  {reason}")
            print(f"         {url}")
    print(sep)


if __name__ == "__main__":
    from flathunt.cli import main
    main()
