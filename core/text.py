"""Small text helpers shared by every collector: normalizing, hashing, parsing."""

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

_WHITESPACE = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(text: str | None) -> str:
    """Tidy text for storage: unicode-normalized, single spaces, trimmed."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    return _WHITESPACE.sub(" ", text).strip()


def mention_id(source: str, brand_id: str, text: str) -> str:
    """Stable id for a mention. The same review imported twice gets the same id.

    Case, spacing and punctuation differences are ignored so a re-copied
    review with an extra space still counts as a duplicate.
    """
    key_text = re.sub(r"[^\w]+", " ", clean_text(text).lower()).strip()
    raw = f"{source.lower()}|{brand_id}|{key_text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def matches_alias(text: str, aliases: list[str]) -> bool:
    """True if any alias appears in the text as a whole word or phrase."""
    lowered = clean_text(text).lower()
    return any(
        re.search(rf"(?<!\w){re.escape(alias.lower())}(?!\w)", lowered) for alias in aliases
    )


_RATING = re.compile(r"(\d+(?:\.\d+)?)")


def parse_rating(value) -> float | None:
    """'4', '4.0 out of 5 stars', '★ 3' -> float in 1..5, else None."""
    if value is None:
        return None
    match = _RATING.search(str(value))
    if not match:
        return None
    rating = float(match.group(1))
    return rating if 1 <= rating <= 5 else None


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%b, %Y",
    "%B %Y",
]


def parse_date(value) -> str | None:
    """Parse the date formats Amazon/Flipkart exports commonly use.

    Handles strings like 'Reviewed in India on 12 March 2024'. Returns an
    ISO date (YYYY-MM-DD) or None when the value cannot be understood.
    """
    text = clean_text(value)
    if not text:
        return None
    if " on " in text:  # "Reviewed in India on 12 March 2024"
        text = text.rsplit(" on ", 1)[1]
    text = text.rstrip("Z")
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None
