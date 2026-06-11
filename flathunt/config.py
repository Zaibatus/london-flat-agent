from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var: {key}\n"
            f"Copy .env.example to .env and fill in the value."
        )
    return val


GEMINI_API_KEY: str = _require("GEMINI_API_KEY")
SPREADSHEET_NAME: str = os.getenv("SPREADSHEET_NAME", "London flat hunt")
SHEET_TAB: str = os.getenv("SHEET_TAB", "Flats")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
SMOKE_TEST_CELL: str = os.getenv("SMOKE_TEST_CELL", "Z1")

_sa_raw: str = _require("GOOGLE_SERVICE_ACCOUNT_JSON")

# Accept either a file path or raw JSON pasted inline.
if _sa_raw.strip().startswith("{"):
    SERVICE_ACCOUNT_INFO: dict = json.loads(_sa_raw)
    SERVICE_ACCOUNT_FILE: str | None = None
else:
    path = Path(_sa_raw).expanduser()
    if not path.exists():
        raise RuntimeError(
            f"Service account file not found: {path}\n"
            f"Set GOOGLE_SERVICE_ACCOUNT_JSON to the correct path."
        )
    SERVICE_ACCOUNT_FILE = str(path)
    SERVICE_ACCOUNT_INFO = {}

# Column numbers (1-indexed, matching gspread / Sheets API convention).
# Key names also match the field names in FlatListing so they can be used interchangeably.
COL: dict[str, int] = {
    "link":          1,   # A — read-only input
    "sqm":           2,   # B
    "bedrooms":      3,   # C
    "area":          4,   # D
    "furnished":     5,   # E  — only "Yes" / "No"
    "price_pcm":     6,   # F
    "available_from": 7,  # G
    "agency":        8,   # H
    # I–L (9–12) are personal/protected — never written
    "rating":        13,  # M — AI quality score 1–5 (written by discover, not enrich)
}

RATING_COL: int = 13
COMMENT_COL: int = 12  # L — human/agent provenance notes
STATUS_COL: int = 14   # N — agent writes "Ignored" here

# Columns I–K must never be written under any circumstances.
PROTECTED_COLS: frozenset[int] = frozenset({9, 10, 11})

HEADER_ROW: int = 1

# Fields that the enrichment loop may write (everything except "link").
WRITABLE_FIELDS: list[str] = [
    "sqm", "bedrooms", "area", "furnished", "price_pcm", "available_from", "agency"
]
