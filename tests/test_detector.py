from datetime import date, timedelta

import pytest
from sqlalchemy import text

from brain import detector
from brain.detector import detect, is_spike, save_new, z_score
from core import db
from core.text import mention_id
from outputs import alerts

AS_OF = date(2026, 8, 31)
RECENT_DAY = "2026-08-28"  # inside the last 7 days
BASELINE_DAY = "2026-08-10"  # inside the 28 days before


def add(engine, brand, n, day, issue=None, sentiment="negative", rating=None, tag=[0]):
    mentions = []
    for _ in range(n):
        tag[0] += 1  # unique text per row
        body = f"{brand} review {tag[0]} about {issue or 'nothing'}"
        mid = mention_id("amazon", brand, body)
        mentions.append(db.Mention(mid, brand, "amazon", body, rating=rating, posted_at=day))
    db.insert_mentions(engine, mentions)
    with engine.begin() as conn:
        for m in mentions:
            conn.execute(
                text(
                    "INSERT INTO mention_tags (mention_id, status, relevant, overall_sentiment, "
                    "attempts, prompt_version, tagged_at) VALUES (:id, 'ok', :yes, 'mixed', 1, 'v', 'now')"
                ),
                {"id": m.id, "yes": True},
            )
            if issue:
                conn.execute(
                    text("INSERT INTO aspect_tags (mention_id, issue, sentiment, quote) "
                         "VALUES (:id, :issue, :s, :q)"),
                    {"id": m.id, "issue": issue, "s": sentiment, "q": f"{issue} is bad {m.text[-20:]}"},
                )


def spike(engine, brand):
    """Battery complaints: 2 of 40 in the baseline, 6 of 15 recently. Low ratings rise too."""
    add(engine, brand, 2, BASELINE_DAY, "battery_life", rating=2)
    add(engine, brand, 2, BASELINE_DAY, "sound_quality", "positive", rating=2)
    add(engine, brand, 36, BASELINE_DAY, "sound_quality", "positive", rating=5)
    add(engine, brand, 6, RECENT_DAY, "battery_life", rating=1)
    add(engine, brand, 9, RECENT_DAY, "sound_quality", "positive", rating=5)


def test_z_score_matches_hand_calculation():
    assert round(z_score(6, 15, 2, 40), 2) == 3.28
    assert z_score(0, 0, 1, 10) == 0.0


def test_small_or_tiny_changes_are_not_spikes():
    assert is_spike(4, 9, 2, 40) is None  # too few recent reviews
    assert is_spike(2, 20, 0, 40) is None  # too few complaints
    assert is_spike(30, 300, 250, 3000) is None  # 10% vs 8.3%: real but too small to matter
    assert is_spike(6, 15, 2, 40) == pytest.approx(3.28, abs=0.01)


def test_detects_client_problem_and_competitor_opportunity(engine, config):
    spike(engine, "boat")
    spike(engine, "jbl")
    add(engine, "noise", 40, BASELINE_DAY, "sound_quality", "positive")
    add(engine, "noise", 5, RECENT_DAY, "battery_life")  # 5 of 5: too few reviews to judge

    found = detect(engine, config, AS_OF)
    got = {(a.brand_id, a.kind, a.issue) for a in found}
    assert got == {
        ("boat", "complaint_spike", "battery_life"),
        ("boat", "low_rating_spike", None),
        ("jbl", "complaint_spike", "battery_life"),
        ("jbl", "low_rating_spike", None),
    }
    battery = next(a for a in found if a.brand_id == "boat" and a.issue == "battery_life")
    assert (battery.recent_hits, battery.recent_total, battery.baseline_hits, battery.baseline_total) == (6, 15, 2, 40)
    assert battery.window_start == "2026-08-25"


def test_nothing_outside_the_windows_counts(engine, config):
    spike(engine, "boat")
    assert detect(engine, config, AS_OF + timedelta(days=60)) == []


def test_cooldown_stops_daily_repeats(engine, config):
    spike(engine, "boat")
    assert len(save_new(engine, detect(engine, config, AS_OF))) == 2
    assert save_new(engine, detect(engine, config, AS_OF)) == []
    tomorrow = detect(engine, config, AS_OF + timedelta(days=1))
    assert tomorrow and save_new(engine, tomorrow) == []


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        pass

    def send_message(self, message):
        FakeSMTP.sent.append(message)


def test_alert_email_marks_anomalies_alerted(engine, config, monkeypatch, tmp_path):
    spike(engine, "boat")
    spike(engine, "jbl")
    new = save_new(engine, detect(engine, config, AS_OF))
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", FakeSMTP)

    assert alerts.send_alerts(engine, config, new) == "emailed 4 alert(s)"
    message = FakeSMTP.sent[-1]
    assert message["Subject"] == "[boAt consultant] 2 problems and 2 opportunities"
    body = message.get_content()
    assert "PROBLEM: boAt battery_life complaints are rising" in body
    assert "OPPORTUNITY: competitor JBL" in body
    assert "40% of reviews (6 of 15)" in body
    assert '"battery_life is bad' in body  # quotes as evidence
    assert "PROBLEM" in (tmp_path / "summary.md").read_text()

    with engine.connect() as conn:
        statuses = {r[0] for r in conn.execute(text("SELECT status FROM anomalies"))}
    assert statuses == {"alerted"}


def test_without_gmail_alerts_are_logged_and_stay_new(engine, config, monkeypatch):
    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)
    spike(engine, "boat")
    new = save_new(engine, detect(engine, config, AS_OF))
    assert alerts.send_alerts(engine, config, new) == "logged only (Gmail not configured)"
    assert alerts.send_alerts(engine, config, []) == "nothing to send"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM anomalies WHERE status = 'new'")).scalar() == 2


def test_constants_are_sane():
    assert detector.RECENT_DAYS < detector.BASELINE_DAYS
