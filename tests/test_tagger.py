import json
from pathlib import Path

from sqlalchemy import text

from brain import tagger
from brain.tagger import validate
from collectors.csv_import import parse_csv
from core import db
from core.llm import LLM
from evals.eval_tagger import load_labels, score
from tests.fakes import FakeProvider

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample"
ISSUES = ["battery_life", "sound_quality", "other"]


def test_validate_keeps_good_tags_and_checks_quotes():
    texts = {"r1": "Sound accha hai but battery 2 ghante mein khatam."}
    data = {
        "items": [
            {
                "id": "r1",
                "relevant": True,
                "overall": "Mixed",
                "aspects": [
                    {"issue": "sound_quality", "sentiment": "positive", "quote": "Sound accha hai"},
                    {"issue": "battery_life", "sentiment": "negative", "quote": "battery dies fast"},
                ],
            }
        ]
    }
    tags, problems = validate(data, texts, ISSUES)
    tag = tags["r1"]
    assert tag.overall == "mixed"
    assert [(a.issue, a.quote) for a in tag.aspects] == [
        ("sound_quality", "Sound accha hai"),
        ("battery_life", None),  # quote was not in the text, so it is dropped
    ]
    assert problems == ["r1: quote for battery_life not found in text, dropped"]


def test_validate_rejects_invented_labels_and_reports_missing_items():
    texts = {"r1": "Battery is bad", "r2": "Nice"}
    data = {
        "items": [
            {
                "id": "r1",
                "overall": "negative",
                "aspects": [
                    {"issue": "power", "sentiment": "negative"},
                    {"issue": "battery_life", "sentiment": "awful"},
                ],
            }
        ]
    }
    tags, problems = validate(data, texts, ISSUES)
    assert tags["r1"].aspects == []
    assert "r2" not in tags
    assert len(problems) == 3


def test_irrelevant_items_get_no_aspects():
    data = {"items": [{"id": "r1", "relevant": False, "overall": "neutral",
                       "aspects": [{"issue": "other", "sentiment": "neutral"}]}]}
    tags, _ = validate(data, {"r1": "Bought a fishing boat"}, ISSUES)
    assert tags["r1"].relevant is False and tags["r1"].aspects == []


def keyword_tagger(prompt: str) -> str:
    """Fake LLM: tags battery/sound by keyword."""
    items = json.loads(prompt.split("\n", 1)[1])["items"]
    out = []
    for item in items:
        lowered = item["text"].lower()
        aspects = []
        if "battery" in lowered:
            sentiment = "positive" if "good" in lowered or "lasts all week" in lowered else "negative"
            aspects.append({"issue": "battery_life", "sentiment": sentiment})
        if "sound" in lowered:
            aspects.append({"issue": "sound_quality", "sentiment": "positive"})
        out.append({"id": item["id"], "relevant": True, "overall": "mixed", "aspects": aspects})
    return json.dumps({"items": out})


def fake_llm(engine=None, respond=keyword_tagger):
    llm = LLM(engine, retries=1, providers=[FakeProvider(respond=respond)])
    llm.backoff = 0
    return llm


def test_tag_pending_stores_tags_once(engine, config):
    db.insert_mentions(engine, parse_csv(SAMPLES / "sample_reviews.csv", config).mentions)
    llm = fake_llm(engine)

    stats = tagger.tag_pending(engine, llm, config)
    assert (stats["pending"], stats["tagged"], stats["failed"]) == (20, 20, 0)
    assert tagger.tag_pending(engine, llm, config)["pending"] == 0

    with engine.connect() as conn:
        battery = conn.execute(
            text("SELECT COUNT(*) FROM aspect_tags WHERE issue = 'battery_life'")
        ).scalar()
    assert battery == 6


def test_failed_batches_are_retried_then_given_up(engine, config):
    db.insert_mentions(engine, parse_csv(SAMPLES / "sample_reviews.csv", config).mentions[:3])

    def broken(prompt):
        raise RuntimeError("quota exceeded")

    llm = fake_llm(respond=broken)
    for _ in range(tagger.MAX_ATTEMPTS):
        assert tagger.tag_pending(engine, llm, config)["failed"] == 3
    assert tagger.tag_pending(engine, llm, config)["pending"] == 0

    stats = tagger.tag_pending(engine, fake_llm(), config)  # a newer prompt version would retry
    assert stats["pending"] == 0


def test_eval_scoring():
    labels = load_labels()
    assert len(labels) == 28
    assert labels[0]["aspects"] == {"sound_quality": "positive", "battery_life": "negative"}

    texts = {str(i): row["text"] for i, row in enumerate(labels[:2])}
    tags, _ = validate(json.loads(keyword_tagger("x\n" + json.dumps(
        {"items": [{"id": k, "text": v} for k, v in texts.items()]}))), texts, ISSUES)
    result = score(labels[:2], [tags["0"], tags["1"]])
    # Review 1: both issues right. Review 2: missed charging and customer_service.
    assert (result["issue_precision"], result["issue_recall"]) == (1.0, 0.5)
