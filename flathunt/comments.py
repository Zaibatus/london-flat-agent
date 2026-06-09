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

# Old-style anchor like "#gid=0&range=B26:B26" — captures the row number.
_OLD_ANCHOR_RE = re.compile(r"[?&#].*?range=\w*?(\d+)", re.IGNORECASE)
# "row 26" or "row26" anywhere in the comment.
_ROW_HINT_RE = re.compile(r"\brow\s*(\d+)\b", re.IGNORECASE)
# SpareRoom flatshare_id in the comment text (user pastes a URL).
_FLATSHARE_ID_RE = re.compile(r"flatshare_id=(\d+)")
# Strip @email-style mentions (e.g. @user@domain.com) from request text.
_MENTION_RE = re.compile(r"@\S+@\S+\.[\w.]+")

_SYSTEM_PROMPT = """\
You are a flat-hunting assistant embedded in a Google Sheets comment thread.
The user has tagged a comment on a property listing. You will be shown the full row
data (cols A–M) and the comment text.

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

_NO_ROW_REPLY = (
    "I received your comment but couldn't determine which listing it's attached to "
    "(Google Sheets uses an internal cell reference I can't decode). "
    "To help me find the right row, please include the row number in your comment — "
    "e.g. \"row 26\" or paste the SpareRoom listing URL."
)


class _CommentResponse(BaseModel):
    intent: Literal["qa", "re_enrich", "re_rate", "ignore"]
    reply: str


@dataclass
class _Stats:
    seen: int = 0
    skipped_resolved: int = 0
    skipped_no_trigger: int = 0
    skipped_replied: int = 0
    no_row_found: int = 0
    processed: int = 0
    failed: int = 0
    by_intent: dict[str, int] = field(default_factory=lambda: {
        "qa": 0, "re_enrich": 0, "re_rate": 0, "ignore": 0,
    })


def _is_addressed_to_agent(comment: dict, bot_email: str) -> bool:
    """Return True if the comment is directed at the service account."""
    bot_lower = bot_email.lower()
    # Explicit @mention detected by Drive API.
    if bot_lower in [e.lower() for e in comment.get("mentionedEmailAddresses", [])]:
        return True
    # Assigned to the service account via @mention.
    if comment.get("assigneeEmailAddress", "").lower() == bot_lower:
        return True
    # Plain-text "@agent" fallback (for comments written without the @mention UI).
    if "@agent" in comment.get("content", "").lower():
        return True
    return False


def _already_replied(comment: dict, bot_email: str) -> bool:
    """Return True if the service account has already replied to this comment.

    Drive API sets author.me=True when the reply was made by the authenticated
    principal (our service account). Service accounts don't get emailAddress in
    reply author objects — only displayName — so we check me first, then fall
    back to displayName comparison.
    """
    bot_lower = bot_email.lower()
    for reply in comment.get("replies", []):
        author = reply.get("author", {})
        if author.get("me", False):
            return True
        # Fallback: displayName contains the email for service accounts.
        if author.get("displayName", "").lower() == bot_lower:
            return True
    return False


def _find_row(comment: dict, content: str, row_index: dict[int, dict]) -> int | None:
    """Try every strategy to find which sheet row this comment belongs to.

    Strategy 1: old-style Drive anchor (#gid=0&range=B26:B26)
    Strategy 2: "row N" hint in the comment text
    Strategy 3: SpareRoom flatshare_id URL pasted in the comment
    """
    anchor: str = comment.get("anchor", "")

    # Strategy 1: old anchor format.
    m = _OLD_ANCHOR_RE.search(anchor)
    if m:
        row = int(m.group(1))
        if row in row_index:
            return row

    # Strategy 2: explicit row hint from the user ("row 26").
    m = _ROW_HINT_RE.search(content)
    if m:
        row = int(m.group(1))
        if row in row_index:
            return row

    # Strategy 3: SpareRoom URL in the comment text.
    m = _FLATSHARE_ID_RE.search(content)
    if m:
        fid = m.group(1)
        for row_data in row_index.values():
            if fid in row_data.get("link", ""):
                return row_data["_row"]

    return None


def _extract_request(content: str) -> str:
    """Strip @mentions and the @agent trigger from the comment to get just the request."""
    text = _MENTION_RE.sub("", content)
    text = re.sub(r"@agent\b", "", text, flags=re.IGNORECASE)
    return text.strip()


def _classify_and_reply(comment_text: str, row_data: dict) -> _CommentResponse:
    """One Gemini call: classify intent and draft a reply for the comment thread."""
    row_summary = "\n".join(
        f"  {k}: {v}" for k, v in row_data.items()
        if k != "_row" and v
    )
    prompt = (
        f"User's comment: {comment_text}\n\n"
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


def _dispatch(intent: str, sheet_row: int, sheet: SheetsClient, row_data: dict) -> None:
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
        score, _notes = rate_images(images, clean_html(html))
        sheet.write_cell(sheet_row, config.RATING_COL, score)
        log.info("  re_rate: wrote score %d to row %d col M", score, sheet_row)

    elif intent == "ignore":
        sheet.write_cell(sheet_row, config.STATUS_COL, "Ignored")
        log.info("  ignore: wrote 'Ignored' to row %d col N", sheet_row)

    # "qa" → no sheet action


def _post_reply(drive_svc, file_id: str, comment_id: str, reply_text: str) -> None:
    drive_svc.replies().create(
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
        fields=(
            "comments("
            "id,anchor,content,resolved,"
            "mentionedEmailAddresses,assigneeEmailAddress,"
            "replies(author(me,emailAddress,displayName))"
            ")"
        ),
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

        if not _is_addressed_to_agent(comment, bot_email):
            stats.skipped_no_trigger += 1
            continue

        if _already_replied(comment, bot_email):
            stats.skipped_replied += 1
            log.debug("Already replied to comment %s — skipping", comment["id"])
            continue

        content: str = comment.get("content", "")
        request_text = _extract_request(content)

        sheet_row = _find_row(comment, content, row_index)
        if sheet_row is None:
            log.warning("Could not determine row for comment %s — sending help reply", comment["id"])
            stats.no_row_found += 1
            try:
                _post_reply(drive_svc, file_id, comment["id"], _NO_ROW_REPLY)
            except Exception as exc:
                log.error("  Failed to post no-row reply: %s", exc)
            continue

        log.info("── comment %s  row=%d  %r", comment["id"], sheet_row, request_text[:80])

        try:
            row_data = row_index[sheet_row]
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
    print(f"  Row not found      : {stats.no_row_found}")
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
