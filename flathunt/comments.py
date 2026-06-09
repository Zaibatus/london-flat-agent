from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from google import genai
from google.genai import types
from googleapiclient.discovery import build
from pydantic import BaseModel

from flathunt import config
from flathunt import enrich as enrich_module
from flathunt.extract import clean_html
from flathunt.fetch import FetchError, fetch_page
from flathunt.images import download_images, extract_image_urls, rate_images
from flathunt.sheets import SheetsClient

log = logging.getLogger(__name__)

_AGENT_TRIGGER = "@agent"

# Matches anchors like "#gid=0&range=B5:B5" — captures the first run of digits
# (the row number) after the column letter(s).
_ANCHOR_RE = re.compile(r"[?&#].*?range=\w*?(\d+)", re.IGNORECASE)

_SYSTEM_PROMPT = """\
You are a flat-hunting assistant embedded in a Google Sheets comment thread.
The user has tagged a comment with "@agent". The comment refers to one row of a
flat-listing spreadsheet. You will be shown the row data (cols A–M) and the
comment text (everything after "@agent").

Classify the user's intent as one of four values:
  qa        — A question, feedback, or observation that can be answered from the data.
  re_enrich — The user wants the listing fields (sqm, bedrooms, area, price, etc.) re-extracted.
              Trigger words: "re-fetch", "refresh", "update", "enrich".
  re_rate   — The user wants the photo quality score (Rating) recalculated.
              Trigger words: "re-rate", "rate again", "check photos", "update score".
  ignore    — The user wants to mark this listing as ignored / not interested.
              Trigger words: "ignore", "skip", "not interested", "remove".

Rules:
- For qa: write a concise, helpful reply (2–4 sentences) using only the provided row data.
  Do not invent information not present in the data.
- For re_enrich / re_rate: write a brief acknowledgement
  (e.g. "Re-fetching the listing details now — fields will update shortly.").
- For ignore: set reply to exactly "Noted — marking this listing as ignored."
- Never mention column letters or internal field names.
- Write as a helpful human assistant, not a bot.
"""


class _CommentResponse(BaseModel):
    intent: Literal["qa", "re_enrich", "re_rate", "ignore"]
    reply: str


@dataclass
class _Stats:
    seen: int = 0
    skipped_resolved: int = 0
    skipped_no_trigger: int = 0
    skipped_replied: int = 0
    skipped_no_anchor: int = 0
    processed: int = 0
    failed: int = 0
    by_intent: dict[str, int] = field(default_factory=lambda: {
        "qa": 0, "re_enrich": 0, "re_rate": 0, "ignore": 0,
    })


def _row_from_anchor(anchor: str) -> int | None:
    """Parse a Drive comment anchor to a 1-based sheet row number."""
    m = _ANCHOR_RE.search(anchor)
    return int(m.group(1)) if m else None


def _already_replied(comment: dict, bot_email: str) -> bool:
    """Return True if the service account has already replied to this comment."""
    for reply in comment.get("replies", []):
        author_email = reply.get("author", {}).get("emailAddress", "")
        if author_email.lower() == bot_email.lower():
            return True
    return False


def _classify_and_reply(comment_text: str, row_data: dict) -> _CommentResponse:
    """One Gemini call: classify intent and draft a reply for the comment thread."""
    row_summary = "\n".join(
        f"  {k}: {v}" for k, v in row_data.items()
        if k != "_row" and v
    )
    prompt = (
        f"Comment (after @agent): {comment_text}\n\n"
        f"Listing row data:\n{row_summary}"
    )
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_CommentResponse,
        ),
    )
    return _CommentResponse.model_validate_json(response.text)


def _dispatch(
    intent: str,
    sheet_row: int,
    sheet: SheetsClient,
    row_data: dict,
) -> None:
    """Execute the action implied by the classified intent."""
    if intent == "re_enrich":
        enrich_module.run(force=True, row=sheet_row)

    elif intent == "re_rate":
        url = row_data.get("link", "").strip()
        if not url:
            log.warning("re_rate: no URL in row %d", sheet_row)
            return
        try:
            html = fetch_page(url)
        except FetchError as exc:
            log.warning("re_rate: fetch failed for row %d: %s", sheet_row, exc)
            return
        img_urls = extract_image_urls(html)
        images = download_images(img_urls, max_n=5)
        score, notes = rate_images(images, clean_html(html))
        sheet.write_cell(sheet_row, config.RATING_COL, score)
        log.info("  re_rate: wrote score %d to row %d col M", score, sheet_row)

    elif intent == "ignore":
        sheet.write_cell(sheet_row, config.STATUS_COL, "Ignored")
        log.info("  ignore: wrote 'Ignored' to row %d col N", sheet_row)

    # "qa" → no sheet action


def _post_reply(drive_svc, file_id: str, comment_id: str, reply_text: str) -> None:
    drive_svc.comments().replies().create(
        fileId=file_id,
        commentId=comment_id,
        fields="id",
        body={"content": reply_text},
    ).execute()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    sheet = SheetsClient()
    bot_email = sheet.credentials.service_account_email
    file_id = sheet.spreadsheet_id
    drive_svc = build("drive", "v3", credentials=sheet.credentials, cache_discovery=False)

    result = drive_svc.comments().list(
        fileId=file_id,
        fields="comments(id,anchor,content,resolved,replies(author/emailAddress))",
        pageSize=100,
        includeDeleted=False,
    ).execute()
    all_comments: list[dict] = result.get("comments", [])
    log.info("Fetched %d comments from Drive", len(all_comments))

    # Load all sheet rows once — O(1) lookup by row number.
    data_rows = sheet.get_data_rows()
    row_index: dict[int, dict] = {r["_row"]: r for r in data_rows}

    stats = _Stats()

    for comment in all_comments:
        stats.seen += 1

        if comment.get("resolved", False):
            stats.skipped_resolved += 1
            continue

        content: str = comment.get("content", "")
        if _AGENT_TRIGGER not in content.lower():
            stats.skipped_no_trigger += 1
            continue

        if _already_replied(comment, bot_email):
            stats.skipped_replied += 1
            log.debug("Already replied to comment %s — skipping", comment["id"])
            continue

        anchor: str = comment.get("anchor", "")
        sheet_row = _row_from_anchor(anchor)
        if sheet_row is None:
            log.warning("Could not parse row from anchor %r — skipping", anchor)
            stats.skipped_no_anchor += 1
            continue

        row_data = row_index.get(sheet_row)
        if row_data is None:
            log.warning("Row %d not found in sheet — skipping", sheet_row)
            stats.skipped_no_anchor += 1
            continue

        # Strip "@agent" from the start of the request text.
        idx = content.lower().index(_AGENT_TRIGGER)
        request_text = content[idx + len(_AGENT_TRIGGER):].strip()

        log.info(
            "── comment %s  row=%d  %r",
            comment["id"], sheet_row, request_text[:80],
        )

        try:
            resp = _classify_and_reply(request_text, row_data)
            log.info("  intent=%s  reply=%r", resp.intent, resp.reply[:80])
            _dispatch(resp.intent, sheet_row, sheet, row_data)
            _post_reply(drive_svc, file_id, comment["id"], resp.reply)
            stats.processed += 1
            stats.by_intent[resp.intent] = stats.by_intent.get(resp.intent, 0) + 1
        except Exception as exc:
            log.error("  Failed: %s", exc)
            stats.failed += 1

    _print_summary(stats)


def _print_summary(stats: _Stats) -> None:
    sep = "─" * 48
    print(f"\n{sep}")
    print("  Comment listener summary")
    print(sep)
    print(f"  Comments fetched   : {stats.seen}")
    print(f"  Already replied    : {stats.skipped_replied}")
    print(f"  Processed          : {stats.processed}")
    if any(stats.by_intent.values()):
        for intent, count in stats.by_intent.items():
            if count:
                print(f"    {intent:<12}: {count}")
    print(f"  Failed             : {stats.failed}")
    print(sep)


if __name__ == "__main__":
    from flathunt.comments_cli import main
    main()
