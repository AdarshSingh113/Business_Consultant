from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from brain import consultant
from core import db
from jobs import weekly
from outputs import report
from tests.test_diagnosis import Script, battery_spike_on_one_product, llm_for

DASHBOARD = str(Path(__file__).resolve().parent.parent / "outputs" / "dashboard.py")


@pytest.fixture
def investigated(engine, config, monkeypatch):
    """A database with one anomaly whose diagnosis is waiting on a question."""
    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GITHUB_STEP_SUMMARY", "GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    battery_spike_on_one_product(engine)
    script = Script([{"action": "request_user_data", "question": "Supplier change?", "why": "SKU-only spike"}])
    consultant.investigate(engine, llm_for(script), config)
    return engine


def test_weekly_report_has_every_section(investigated, config):
    subject, body = report.build_report(investigated, config, llm=None)
    assert subject.startswith("[boAt consultant] Weekly report: 1 alert(s)")
    for heading in ("## This week's alerts", "## Diagnoses and recommended actions",
                    "## Questions waiting for you", "## Competitor Teardown", "## Perception Radar",
                    "## Pipeline health"):
        assert heading in body
    assert "Supplier change?" in body


def test_weekly_job_saves_and_logs(investigated, monkeypatch):
    monkeypatch.setattr(db, "get_engine", lambda url=None: investigated)
    assert weekly.run() == 0
    from sqlalchemy import text
    with investigated.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM reports WHERE kind = 'weekly'")).scalar() == 1
        assert conn.execute(text("SELECT status FROM runs WHERE job = 'weekly'")).scalar() == "ok"


def test_dashboard_pages_render_and_answer_questions(investigated, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", investigated.url.render_as_string(hide_password=False))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    app = AppTest.from_file(DASHBOARD, default_timeout=30).run()
    app.text_input[0].input("s3cret").run()
    assert not app.exception
    assert app.title[0].value == "Perception Radar"

    views = app.sidebar.radio[0].options
    for view in views:
        app.sidebar.radio[0].set_value(view).run()
        assert not app.exception, view

    app.sidebar.radio[0].set_value("Questions for you (1)").run()
    app.text_area[0].input("Yes, new supplier on 20 Aug").run()
    app.button[0].click().run()
    assert "Saved" in app.success[0].value
    assert consultant.open_requests(investigated) == []


def test_dashboard_requires_password_for_cloud_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@example.invalid:5432/db")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = AppTest.from_file(DASHBOARD, default_timeout=30).run()
    assert "DASHBOARD_PASSWORD" in app.error[0].value

    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    app = AppTest.from_file(DASHBOARD, default_timeout=30).run()
    app.text_input[0].input("wrong").run()
    assert app.error[0].value == "Wrong password."
