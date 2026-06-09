# London Flat-Hunt Enrichment Agent

A CLI tool that reads your "London flat hunt" Google Sheet, fetches each listing page, extracts structured fields using Google Gemini, and writes them back — without ever touching your manual entries.

## Prerequisites

- Python 3.11+
- A Google Cloud project (free tier is fine)
- A Google Gemini API key
- Access to the target Google Sheet

---

## Setup

### 1. Google Cloud: service account + Sheets API

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project.
2. Enable the **Google Sheets API**: APIs & Services → Library → search "Sheets" → Enable.
3. Create a **service account**: APIs & Services → Credentials → Create Credentials → Service Account.
   - Give it any name (e.g. `flat-hunt-agent`).
   - Skip optional role assignment — the sheet share (step 5) provides access.
4. Create a JSON key: click the service account → Keys → Add Key → JSON. Download the file.
5. **Share your Google Sheet** with the service account's email address (looks like `flat-hunt-agent@your-project.iam.gserviceaccount.com`) — grant it **Editor** access.

### 2. Gemini API key

Go to [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey), create a key, and copy it.

To check available model IDs, visit [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models). The default `gemini-2.5-flash` is a cost-efficient choice for structured extraction.

### 3. Install Python dependencies

```bash
cd "Flat search agent"
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Install Playwright browsers

```bash
playwright install chromium
```

This is only used as a fallback for JS-heavy or bot-protected portals (e.g. some Rightmove pages).

### 5. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/service-account.json
GEMINI_API_KEY=your-key-here
SPREADSHEET_NAME=London flat hunt
SHEET_TAB=Flats
GEMINI_MODEL=gemini-2.5-flash
SMOKE_TEST_CELL=M1
```

The `SMOKE_TEST_CELL` is a scratch cell (default `M1`) used only by the smoke test to verify write access. It must be outside columns I–L.

---

## Phase 0: Smoke test

Run this before using the agent for the first time:

```bash
python smoke_test.py
```

Expected output:
```
Phase 0 — connectivity smoke test
==================================================
  PASS  Sheets : read A1 + write/read scratch cell
  PASS  Gemini : generate_content call
  PASS  HTTP   : GET https://httpbin.org/get
==================================================
All checks passed. Ready for Phase 1.
```

Fix any FAIL before proceeding.

---

## Phase 1: Enrichment

### Sheet schema

| Column | Field | Notes |
|--------|-------|-------|
| A | Link | Your input (URL) |
| B | # Sqm | Filled by agent; sq ft converted to m² |
| C | # Bed rooms | Filled by agent |
| D | Area | Filled by agent (neighbourhood) |
| E | Furnished? | Filled by agent; only `Yes` or `No` |
| F | Price | Filled by agent; always £/month |
| G | Available from | Filled by agent |
| H | Agency | Filled by agent |
| I–L | Visit / Comments | **Never touched** |

### Run the agent

```bash
# Test on a single row first (row 2 = first data row):
python -m flathunt.enrich --row 2

# Process up to 5 rows:
python -m flathunt.enrich --limit 5

# Process all rows with a URL:
python -m flathunt.enrich

# Re-enrich everything (overwrite existing B–H values):
python -m flathunt.enrich --force

# Combine flags:
python -m flathunt.enrich --row 3 --force
```

### Behaviour

- **Idempotent by default**: only writes to cells that are currently empty. Running twice produces no changes the second time.
- **`--force`**: overwrites B–H values. Columns I–L are protected regardless.
- **Failures are non-fatal**: if a page can't be fetched or parsed, the row is skipped and logged. A summary is printed at the end.
- **Rate-limited**: 2-second pause between rows.

### Example output

```
INFO  Row 2: https://www.rightmove.co.uk/properties/12345678
INFO    wrote price_pcm = 2200
INFO    wrote bedrooms = 2
INFO    wrote area = 'Hackney'
INFO    wrote furnished = 'Yes'
INFO    wrote available_from = 'Now'
INFO    wrote agency = 'Foxtons'

────────────────────────────────────────────
  Run summary
────────────────────────────────────────────
  Rows processed : 1
  Cells written  : 6
  Failures       : 0
────────────────────────────────────────────
```

---

## Project structure

```
flathunt/
├── config.py     — env loading, column map, constants
├── sheets.py     — gspread read/write with protected-column guard
├── fetch.py      — httpx fetch with Playwright fallback
├── extract.py    — Gemini structured extraction + Pydantic schema
├── enrich.py     — orchestration loop + run summary
└── cli.py        — argparse entry point
smoke_test.py     — Phase 0 connectivity checks
```

---

## TODOs (out of scope for this milestone)

- Listing discovery / portal search
- Deduplication across portals
- Scheduling / cron
- Notifications
