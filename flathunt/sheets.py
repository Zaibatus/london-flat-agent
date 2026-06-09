from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

from flathunt import config

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


class SheetsClient:
    def __init__(self) -> None:
        if config.SERVICE_ACCOUNT_FILE:
            gc = gspread.service_account(
                filename=config.SERVICE_ACCOUNT_FILE,
                scopes=_SCOPES,
            )
        else:
            creds = Credentials.from_service_account_info(
                config.SERVICE_ACCOUNT_INFO,
                scopes=_SCOPES,
            )
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
