from __future__ import annotations

import logging
import re
from typing import Literal

import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel

from flathunt import config

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# SpareRoom photo URLs typically contain these domains.
_IMAGE_URL_PATTERNS = [
    r"https?://[^\s\"']+spareroom[^\s\"']*\.(?:jpg|jpeg|png|webp)",
    r"https?://img\.spareroom\.co\.uk[^\s\"']*",
    r"https?://images\.spareroom\.co\.uk[^\s\"']*",
]


class _ImageRating(BaseModel):
    score: int          # 1–5
    notes: str          # brief justification shown in console


_RATING_SYSTEM_PROMPT = """\
You are evaluating a London rental flat to help a couple decide whether to visit it.
You will be shown up to 5 photos of the property plus a short text summary.

Rate the flat 1–5 using this scale:
  5 — Excellent: bright, spacious, modern or tastefully decorated, very well presented
  4 — Good: clean, decent space, pleasant decor, minor imperfections
  3 — Acceptable: liveable but nothing special; some dated elements or small rooms
  2 — Below average: noticeable issues (very small, dark, cluttered, poor condition)
  1 — Poor: avoid (dirty, heavily damaged, extremely cramped, or no usable photos)

Also factor in value for money at the stated price (budget is £2,000–£2,500/month).
A lovely flat at £2,000 should score higher than an identical flat at £2,450.

Be concise in your notes — one or two sentences maximum.
If no photos are provided, set score to 0 and notes to "no photos available".
"""


def extract_image_urls(html: str) -> list[str]:
    """Extract SpareRoom photo URLs from a listing page."""
    soup = BeautifulSoup(html, "lxml")
    found: set[str] = set()

    # Method 1: <img> tags with SpareRoom domains.
    for img in soup.find_all("img"):
        src = img.get("src", "") or img.get("data-src", "")
        if src and ("spareroom" in src.lower() or "/listing" in src.lower()):
            if src.startswith("//"):
                src = "https:" + src
            if src.startswith("http"):
                found.add(src)

    # Method 2: regex sweep over raw HTML for any image URL patterns.
    for pattern in _IMAGE_URL_PATTERNS:
        for match in re.finditer(pattern, html, re.IGNORECASE):
            found.add(match.group(0))

    # Prefer "full" or "large" variants over thumbnails.
    full = [u for u in found if any(s in u for s in ("/full/", "/large/", "1200", "800"))]
    rest = [u for u in found if u not in full]
    return (full + rest)[:10]  # cap at 10 candidates; download step will take max 5


def download_images(urls: list[str], max_n: int = 5) -> list[bytes]:
    """Download image bytes from a list of URLs, skipping failures."""
    images: list[bytes] = []
    with httpx.Client(follow_redirects=True, timeout=10) as client:
        for url in urls:
            if len(images) >= max_n:
                break
            try:
                resp = client.get(url, headers={"User-Agent": _USER_AGENT})
                if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
                    images.append(resp.content)
            except Exception as exc:
                log.debug("Image download failed for %s: %s", url, exc)
    return images


def _mime_type(data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] in (b"RIFF", b"WEBP"):
        return "image/webp"
    return "image/jpeg"  # safe default


def rate_images(images: list[bytes], listing_text: str) -> tuple[int, str]:
    """Rate a flat 1–5 using Gemini Vision on its photos.

    Returns (score, notes). Returns (0, "no photos available") if no images.
    On any API failure returns (3, "rating unavailable") so the listing isn't
    silently dropped.
    """
    if not images:
        return 0, "no photos available"

    try:
        client = genai.Client(api_key=config.GEMINI_API_KEY)

        parts: list[types.Part | str] = [
            f"Listing summary:\n{listing_text[:1000]}\n\nPhotos follow:",
        ]
        for img_bytes in images:
            parts.append(
                types.Part.from_bytes(data=img_bytes, mime_type=_mime_type(img_bytes))
            )

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=parts,
            config=types.GenerateContentConfig(
                system_instruction=_RATING_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_ImageRating,
            ),
        )
        result = _ImageRating.model_validate_json(response.text)
        log.debug("Image rating: %d/5 — %s", result.score, result.notes)
        return result.score, result.notes

    except Exception as exc:
        log.warning("Image rating failed: %s", exc)
        return 3, "rating unavailable"
