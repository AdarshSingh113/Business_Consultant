from pathlib import Path

from collectors.csv_import import map_columns, parse_csv
from core import db

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample"


def test_map_columns_accepts_common_header_names():
    mapping = map_columns(["Review Title", "Review Body", "Stars", "Reviewed On"])
    assert mapping == {
        "text": "Review Body",
        "rating": "Stars",
        "title": "Review Title",
        "posted_at": "Reviewed On",
    }


def test_sample_file_skips_bad_rows(config):
    result = parse_csv(SAMPLES / "sample_reviews.csv", config)
    assert result.rows_read == 23
    assert result.skipped == {"unknown brand 'Sony'": 1, "empty or too short text": 1}
    assert len(result.mentions) == 21  # includes one duplicate, removed at insert time
    assert {m.brand_id for m in result.mentions} == {"boat", "noise", "jbl"}


def test_amazon_style_export_with_defaults(config):
    result = parse_csv(SAMPLES / "amazon_export_style.csv", config, "boat", "amazon")
    first = result.mentions[0]
    assert (first.brand_id, first.source, first.rating, first.posted_at) == (
        "boat", "amazon", 1.0, "2026-08-21"
    )
    assert first.product == "Airdopes 141"


def test_reimport_stores_nothing_new(engine, config):
    mentions = parse_csv(SAMPLES / "sample_reviews.csv", config).mentions
    assert db.insert_mentions(engine, mentions) == 20  # 21 parsed minus 1 duplicate
    assert db.insert_mentions(engine, mentions) == 0
    counts = {(b, s): n for b, s, n in db.mention_counts(engine)}
    assert counts[("boat", "amazon")] == 5
