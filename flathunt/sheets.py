from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

from flathunt import config

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",  # drive (not readonly) — needed to post comment replies
]


class SheetsClient:
    def __init__(self) -> None:
        if config.SERVICE_ACCOUNT_FILE:
            creds = Credentials.from_service_account_file(
                config.SERVICE_ACCOUNT_FILE,
                scopes=_SCOPES,
            )
        else:
            creds = Credentials.from_service_account_info(
                config.SERVICE_ACCOUNT_INFO,
                scopes=_SCOPES,
            )
        self._creds = creds
        gc = gspread.Client(auth=creds)

        try:
            sh = gc.open(config.SPREADSHEET_NAME)
        except gspread.SpreadsheetNotFound:
            raise RuntimeError(
                f"Spreadsheet '{config.SPREADSHEET_NAME}' not found.\n"
                f"Make sure you shared it with your service account email."
            )

        try:
            self._ws = sh.worksheet(config.SHEET_TAB)
        except gspread.WorksheetNotFound:
            raise RuntimeError(
                f"Tab '{config.SHEET_TAB}' not found in '{config.SPREADSHEET_NAME}'."
            )

    @property
    def spreadsheet_id(self) -> str:
        """Google Drive file ID for this spreadsheet."""
        return self._ws.spreadsheet.id

    @property
    def credentials(self) -> Credentials:
        """Service account credentials — reuse to build Drive API client."""
        return self._creds

    def get_data_rows(self) -> list[dict[str, str]]:
        """Return all data rows (skipping the header) as dicts keyed by field name.

        Each dict also has a '_row' key with the 1-based sheet row number.
        """
        all_values: list[list[str]] = self._ws.get_all_values()
        result: list[dict[str, str]] = []
        # all_values[0] = header row (sheet row 1); data starts at index 1 (sheet row 2).
        for i, row in enumerate(all_values[config.HEADER_ROW:], start=config.HEADER_ROW + 1):
            # Pad short rows so every column is accessible.
            padded = row + [""] * max(0, 12 - len(row))
            entry: dict[str, str] = {"_row": i}  # type: ignore[assignment]
            for field, col in config.COL.items():
                entry[field] = padded[col - 1]
            result.append(entry)
        return result

    def read_cell(self, row: int, col: int) -> str:
        return self._ws.cell(row, col).value or ""

    def write_cell(self, row: int, col: int, value: str | int | float) -> None:
        if col in config.PROTECTED_COLS:
            raise ValueError(
                f"Column {col} (I–L) is protected and must never be written. "
                f"This is a programming error."
            )
        self._ws.update_cell(row, col, value)

    def get_all_urls(self) -> set[str]:
        """Return every non-empty URL already in column A (for dedup)."""
        col_values = self._ws.col_values(config.COL["link"])
        return {v.strip() for v in col_values if v.strip().startswith("http")}

    def ensure_rating_header(self) -> None:
        """Write 'Rating' to column M row 1 if the cell is blank or a smoke-test stamp."""
        current = self.read_cell(config.HEADER_ROW, config.RATING_COL)
        if not current or current.startswith("smoke-"):
            self._ws.update_cell(config.HEADER_ROW, config.RATING_COL, "Rating")

    def append_row(
        self,
        url: str,
        listing: object,
        rating: int,
        comment: str = "",
    ) -> int:
        """Append a discovered listing as a new row and return its sheet row number.

        Columns I–K are left blank (protected). L gets the comment, M the rating.
        """
        # Build a list with 14 cells (A through N).
        row: list[str | int | float] = [""] * 14
        row[config.COL["link"] - 1]          = url
        row[config.COL["sqm"] - 1]           = getattr(listing, "sqm", "") or ""
        row[config.COL["bedrooms"] - 1]      = getattr(listing, "bedrooms", "") or ""
        row[config.COL["area"] - 1]          = getattr(listing, "area", "") or ""
        row[config.COL["furnished"] - 1]     = getattr(listing, "furnished", "") or ""
        row[config.COL["price_pcm"] - 1]     = getattr(listing, "price_pcm", "") or ""
        row[config.COL["available_from"] - 1] = getattr(listing, "available_from", "") or ""
        row[config.COL["agency"] - 1]        = getattr(listing, "agency", "") or ""
        # Cols 9–11 (I–K) stay empty (protected).
        row[config.COMMENT_COL - 1]          = comment       # L
        row[config.RATING_COL - 1]           = rating        # M

        self._ws.append_row(row, value_input_option="USER_ENTERED")
        # gspread doesn't return the new row index directly; compute it.
        return len(self._ws.col_values(1))
