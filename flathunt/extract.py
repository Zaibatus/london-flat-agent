from __future__ import annotations

import logging
import re
from typing import Literal

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel

from flathunt import config

log = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=config.GEMINI_API_KEY)
    return _client


class FlatListing(BaseModel):
    """Structured fields extracted from a UK rental listing."""

    sqm: float | None = None
    bedrooms: int | None = None
    area: str | None = None
    furnished: Literal["Yes", "No"] | None = None
    price_pcm: float | None = None
    available_from: str | None = None
    agency: str | None = None


_SYSTEM_PROMPT = """\
You are a data-extraction assistant for UK residential rental listings.
Extract the fields below from the listing text and return valid JSON matching the schema.

Extraction rules:
- price_pcm: monthly rent in £ as a plain number (no £ sign, no commas).
  If the price is given per week (pw), convert: price_pcm = price_pw × 52 / 12.
  Round to the nearest pound.
- sqm: floor area in m² as a decimal. If given in sq ft, multiply by 0.0929, round to 1 d.p.
  If no floor area is stated (only bedroom count), set sqm to null — do not guess.
- bedrooms: number of bedrooms as an integer.
- area: the neighbourhood, district, or borough only (e.g. "Hackney", "Clapham", "Islington").
  Not the full street address.
- furnished: exactly "Yes" or "No". Set to null if it is not stated or is ambiguous
  ("part-furnished" counts as null).
- available_from: the availability date or phrase exactly as stated
  (e.g. "Now", "Available immediately", "1 August 2025", "August 2025"). null if not stated.
- agency: the listing agent or agency name. null if not stated.

Be conservative: if a field is not clearly stated in the text, return null.
An empty/null field is always better than a wrong value.
"""


def clean_html(html: str) -> str:
    """Strip boilerplate tags and return plain text, truncated to 6 000 chars."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "head", "header", "aside", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:6_000]


def extract_listing(html: str, url: str) -> FlatListing:
    """Call Gemini to extract structured fields from a listing page.

    Returns a FlatListing with all fields None on any failure — never raises.
    """
    text = clean_html(html)
    prompt = f"Listing URL: {url}\n\nListing page content:\n{text}"

    try:
        client = _get_client()
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=FlatListing,
            ),
        )
        result = FlatListing.model_validate_json(response.text)
        log.debug("Extracted from %s: %s", url, result.model_dump())
        return result
    except Exception as exc:
        log.warning("Extraction failed for %s: %s", url, exc)
        return FlatListing()
