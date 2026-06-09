#!/usr/bin/env python3
"""
Phase 0 — connectivity smoke test.

Checks three connections in order and prints PASS / FAIL for each.
All three must pass before running the enrichment agent.

Usage:
    python smoke_test.py
"""
import sys
import traceback
from datetime import datetime


def _check(label: str, fn) -> bool:
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except Exception as exc:
        print(f"  FAIL  {label}")
        print(f"        {exc}")
        traceback.print_exc(file=sys.stdout)
        return False


def _test_sheets() -> None:
    import gspread.utils

    from flathunt import config
    from flathunt.sheets import SheetsClient

    sheet = SheetsClient()

    # Read the header cell — it should be non-empty.
    a1 = sheet.read_cell(1, 1)
    if not a1:
        raise AssertionError(
            "Cell A1 is empty. Is the sheet set up with a header row?"
        )
    print(f"          A1 = {a1!r}")

    # Write a timestamped value to the smoke-test scratch cell, then read it back.
    cell_ref = config.SMOKE_TEST_CELL
    row, col = gspread.utils.a1_to_rowcol(cell_ref)
    stamp = f"smoke-{datetime.now().isoformat(timespec='seconds')}"
    sheet.write_cell(row, col, stamp)
    readback = sheet.read_cell(row, col)
    if readback != stamp:
        raise AssertionError(
            f"Wrote {stamp!r} to {cell_ref} but read back {readback!r}"
        )
    print(f"          {cell_ref} write/read OK")


def _test_gemini() -> None:
    from google import genai

    from flathunt import config

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents="Reply with the single word: hello",
    )
    if not response.text or not response.text.strip():
        raise AssertionError("Gemini returned an empty response.")
    print(f"          model={config.GEMINI_MODEL!r}  response={response.text.strip()!r}")


def _test_fetch() -> None:
    import httpx

    url = "https://www.rightmove.co.uk"
    resp = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    if resp.status_code not in range(200, 400):
        raise AssertionError(f"Expected 2xx/3xx, got {resp.status_code}")
    print(f"          GET {url} → {resp.status_code} OK")


if __name__ == "__main__":
    print("Phase 0 — connectivity smoke test")
    print("=" * 50)

    results = [
        _check("Sheets : read A1 + write/read scratch cell", _test_sheets),
        _check("Gemini : generate_content call            ", _test_gemini),
        _check("HTTP   : GET https://httpbin.org/get      ", _test_fetch),
    ]

    print("=" * 50)
    if all(results):
        print("All checks passed. Ready for Phase 1.")
        sys.exit(0)
    else:
        failed = results.count(False)
        print(f"{failed} check(s) failed — fix the issues above before running the agent.")
        sys.exit(1)
