from datetime import datetime, timezone

from sqlalchemy import text

from brain.perception import build_radar, format_radar
from core import db
from core.text import mention_id

TODAY = datetime(2026, 9, 1, tzinfo=timezone.utc)


def add(engine, brand, n, issue, sentiment, rating=None, posted="2026-08-15", relevant=True):
    mentions = [
        db.Mention(
            id=mention_id("amazon", brand, f"{brand} {issue} {sentiment} {i} {posted}"),
            brand_id=brand,
            source="amazon",
            text=f"{brand} {issue} {sentiment} {i}",
            rating=rating,
            posted_at=posted,
        )
        for i in range(n)
    ]
    db.insert_mentions(engine, mentions)
    with engine.begin() as conn:
        for m in mentions:
            conn.execute(
                text(
                    "INSERT INTO mention_tags (mention_id, status, relevant, overall_sentiment, "
                    "attempts, prompt_version, tagged_at) VALUES (:id, 'ok', :rel, :s, 1, 'v', 'now')"
                ),
                {"id": m.id, "rel": relevant, "s": sentiment},
            )
            conn.execute(
                text("INSERT INTO aspect_tags (mention_id, issue, sentiment) VALUES (:id, :i, :s)"),
                {"id": m.id, "i": issue, "s": sentiment},
            )


def test_radar_finds_battery_weakness(engine, config):
    add(engine, "boat", 6, "battery_life", "negative", rating=2)
    add(engine, "boat", 2, "battery_life", "positive", rating=5)
    add(engine, "noise", 5, "battery_life", "positive", rating=4)
    add(engine, "jbl", 3, "battery_life", "positive")
    add(engine, "jbl", 1, "battery_life", "negative")
    add(engine, "boat", 2, "bass", "positive")  # too few for a finding
    add(engine, "noise", 2, "bass", "negative")
    add(engine, "boat", 4, "sound_quality", "negative", posted="2025-01-01")  # outside window
    add(engine, "noise", 3, "other", "neutral", relevant=False)  # not counted

    radar = build_radar(engine, config, days=90, today=TODAY)
    boat = radar.brands["boat"]
    assert boat.mentions == 10
    assert round(boat.avg_rating, 2) == 2.75
    assert round(boat.share_of_voice, 2) == round(10 / 21, 2)

    battery = boat.issues["battery_life"]
    assert (battery.mentions, round(battery.net, 2)) == (8, -0.5)
    assert "sound_quality" not in boat.issues

    [weakness] = radar.weaknesses
    assert weakness.issue == "battery_life"
    assert round(weakness.competitor_net, 2) == round((8 - 1) / 9, 2)
    assert radar.strengths == []

    report = format_radar(radar, config)
    assert "**battery_life**" in report
    assert "Possible gaps that need more data: bass." in report


def test_empty_database_still_renders(engine, config):
    report = format_radar(build_radar(engine, config), config)
    assert "None with enough data" in report
