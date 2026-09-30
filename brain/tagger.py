"""Turn messy review text into structured tags.

One review -> {relevant, overall sentiment, aspects: [{issue, sentiment, quote}]}.
"Sound accha hai but battery 2 ghante mein khatam" becomes
sound_quality/positive and battery_life/negative.

The LLM is not trusted blindly. Every answer is checked:
- issue labels must come from the fixed list in brands.yaml
- sentiments must be one of the allowed values
- quotes must really appear in the review, or they are dropped

Run: python -m brain.tagger            (tags untagged mentions)
     python -m brain.tagger --limit 20 (smaller batch to test)
"""

import argparse
import json
from dataclasses import dataclass, field

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core import db
from core.config import Config, load_config
from core.llm import LLM, LLMError
from core.text import clean_text, now_iso

# Bump this when the prompt changes: already-tagged mentions are then re-tagged.
PROMPT_VERSION = "tagger-v1"

BATCH_SIZE = 10  # reviews per LLM call; fewer calls fit the free tier
MAX_ATTEMPTS = 3  # a mention that fails this many times is left alone
ASPECT_SENTIMENTS = {"positive", "negative", "neutral"}
OVERALL_SENTIMENTS = {"positive", "negative", "mixed", "neutral"}


@dataclass
class Aspect:
    issue: str
    sentiment: str
    quote: str | None


@dataclass
class Tag:
    relevant: bool
    overall: str | None
    aspects: list[Aspect] = field(default_factory=list)


def system_prompt(config: Config) -> str:
    labels = ", ".join(config.issues)
    return f"""You tag customer reviews and social posts about {config.category} for a brand analyst.

For each item decide:
- relevant: true if it is an opinion or experience about a {config.category} product or the brand
  (including delivery and service). false for questions with no opinion, ads, or other products.
- overall: positive, negative, mixed or neutral.
- aspects: every issue the item talks about, each with a sentiment (positive, negative, neutral)
  and a short quote of at most 12 words copied exactly from the text.

Use ONLY these issue labels: {labels}.
Use "other" only when nothing else fits. Do not invent labels. One entry per label per item.
Texts may mix Hindi and English (Hinglish): "accha" = good, "bekar" = useless, "khatam" = finished.
Irrelevant items get an empty aspects list.

Reply with JSON only:
{{"items": [{{"id": "r1", "relevant": true, "overall": "mixed",
  "aspects": [{{"issue": "battery_life", "sentiment": "negative", "quote": "battery 2 ghante mein khatam"}}]}}]}}"""


def build_prompt(texts: dict[str, str]) -> str:
    items = [{"id": key, "text": value} for key, value in texts.items()]
    return "Tag these items:\n" + json.dumps({"items": items}, ensure_ascii=False)


def _normalize(value: str) -> str:
    return clean_text(value).lower()


def validate(data: dict, texts: dict[str, str], issues: list[str]) -> tuple[dict[str, Tag], list[str]]:
    """Check the LLM's answer against the rules. Returns valid tags by id, plus problems found."""
    allowed = set(issues)
    tags: dict[str, Tag] = {}
    problems: list[str] = []

    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("id") not in texts:
            problems.append(f"unknown item {item!r:.60}")
            continue
        key = item["id"]
        relevant = bool(item.get("relevant", True))
        overall = str(item.get("overall", "")).lower()
        if overall not in OVERALL_SENTIMENTS:
            problems.append(f"{key}: bad overall sentiment {overall!r}")
            overall = None

        aspects: dict[str, Aspect] = {}
        for aspect in (item.get("aspects") or []) if relevant else []:
            if not isinstance(aspect, dict):
                problems.append(f"{key}: aspect is not an object")
                continue
            issue = str(aspect.get("issue", "")).strip().lower()
            sentiment = str(aspect.get("sentiment", "")).strip().lower()
            if issue not in allowed:
                problems.append(f"{key}: label {issue!r} is not in the issue list")
                continue
            if sentiment not in ASPECT_SENTIMENTS:
                problems.append(f"{key}: bad sentiment {sentiment!r} for {issue}")
                continue
            quote = clean_text(aspect.get("quote")) or None
            if quote and _normalize(quote) not in _normalize(texts[key]):
                problems.append(f"{key}: quote for {issue} not found in text, dropped")
                quote = None
            aspects.setdefault(issue, Aspect(issue, sentiment, quote))

        tags[key] = Tag(relevant, overall, list(aspects.values()))

    missing = [k for k in texts if k not in tags]
    if missing:
        problems.append(f"no answer for {missing}")
    return tags, problems


def tag_texts(llm: LLM, config: Config, texts: dict[str, str]) -> tuple[dict[str, Tag], list[str], str, str]:
    """Tag one batch of texts keyed by any id. Returns tags, problems, provider, model."""
    short = {f"r{i}": t for i, t in enumerate(texts.values(), start=1)}
    back = dict(zip(short, texts))
    result = llm.complete_json(build_prompt(short), system=system_prompt(config))
    tags, problems = validate(result.data, short, config.issues)
    return {back[k]: t for k, t in tags.items()}, problems, result.provider, result.model


def pending_mentions(engine: Engine, limit: int) -> list[tuple[str, str]]:
    """Mentions never tagged, tagged with an older prompt, or failed fewer than MAX_ATTEMPTS times."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT m.id, m.text FROM mentions m
                LEFT JOIN mention_tags t ON t.mention_id = m.id
                WHERE t.mention_id IS NULL
                   OR (t.status = 'ok' AND t.prompt_version <> :version)
                   OR (t.status = 'failed' AND t.attempts < :max_attempts)
                ORDER BY m.collected_at, m.id
                LIMIT :limit
                """
            ),
            {"version": PROMPT_VERSION, "max_attempts": MAX_ATTEMPTS, "limit": limit},
        )
        return [(r[0], r[1]) for r in rows]


def _save(conn, mention_id: str, tag: Tag | None, error: str | None, provider: str | None, model: str | None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mention_tags (mention_id, status, relevant, overall_sentiment, attempts,
                                      error, provider, model, prompt_version, tagged_at)
            VALUES (:mention_id, :status, :relevant, :overall, 1,
                    :error, :provider, :model, :version, :tagged_at)
            ON CONFLICT (mention_id) DO UPDATE SET
                status = excluded.status,
                relevant = excluded.relevant,
                overall_sentiment = excluded.overall_sentiment,
                attempts = CASE WHEN excluded.status = 'failed'
                                THEN mention_tags.attempts + 1 ELSE mention_tags.attempts END,
                error = excluded.error,
                provider = excluded.provider,
                model = excluded.model,
                prompt_version = excluded.prompt_version,
                tagged_at = excluded.tagged_at
            """
        ),
        {
            "mention_id": mention_id,
            "status": "ok" if tag else "failed",
            "relevant": tag.relevant if tag else None,
            "overall": tag.overall if tag else None,
            "error": error,
            "provider": provider,
            "model": model,
            "version": PROMPT_VERSION,
            "tagged_at": now_iso(),
        },
    )
    if tag is None:
        return
    conn.execute(text("DELETE FROM aspect_tags WHERE mention_id = :id"), {"id": mention_id})
    if tag.aspects:
        conn.execute(
            text(
                "INSERT INTO aspect_tags (mention_id, issue, sentiment, quote) "
                "VALUES (:mention_id, :issue, :sentiment, :quote)"
            ),
            [{"mention_id": mention_id, **a.__dict__} for a in tag.aspects],
        )


def tag_pending(engine: Engine, llm: LLM, config: Config, limit: int = 200) -> dict:
    """Tag up to `limit` pending mentions. Failures are recorded and retried on later runs."""
    pending = pending_mentions(engine, limit)
    stats = {"pending": len(pending), "tagged": 0, "failed": 0, "problems": 0}
    for start in range(0, len(pending), BATCH_SIZE):
        batch = dict(pending[start : start + BATCH_SIZE])
        try:
            tags, problems, provider, model = tag_texts(llm, config, batch)
            error = None
        except LLMError as exc:
            tags, problems, provider, model = {}, [], None, None
            error = str(exc)[:500]
        stats["problems"] += len(problems)
        with engine.begin() as conn:
            for mention_id in batch:
                tag = tags.get(mention_id)
                _save(conn, mention_id, tag, None if tag else (error or "no answer for this item"), provider, model)
                stats["tagged" if tag else "failed"] += 1
    return stats


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Tag untagged mentions with the LLM")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    config = load_config()
    engine = db.get_engine()
    db.init_db(engine)
    stats = tag_pending(engine, LLM(engine), config, args.limit)
    print(stats)


if __name__ == "__main__":
    main()
