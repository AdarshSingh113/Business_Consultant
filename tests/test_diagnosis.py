import json
from datetime import date

from sqlalchemy import text

from brain import consultant, diagnosis
from brain.detector import detect, save_new
from brain.tools import Tools
from core.llm import LLM
from outputs import alerts
from tests.fakes import FakeProvider
from tests.test_detector import add

AS_OF = date(2026, 8, 31)


def battery_spike_on_one_product(engine):
    """boAt battery complaints jump, all on Airdopes 141. Noise stays calm."""
    add(engine, "boat", 2, "2026-08-10", "battery_life")
    add(engine, "boat", 38, "2026-08-10", "sound_quality", "positive")
    add(engine, "boat", 6, "2026-08-28", "battery_life")
    add(engine, "boat", 9, "2026-08-28", "sound_quality", "positive")
    add(engine, "noise", 30, "2026-08-10", "sound_quality", "positive")
    add(engine, "noise", 12, "2026-08-28", "sound_quality", "positive")
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE mentions SET product = CASE WHEN text LIKE '%battery%' AND posted_at = '2026-08-28' "
            "THEN 'Airdopes 141' ELSE 'Airdopes 311 Pro' END WHERE brand_id = 'boat'"
        ))
    [anomaly] = save_new(engine, detect(engine, _config(), AS_OF))
    return anomaly


def _config():
    from core.config import load_config
    return load_config()


class Script:
    """Fake agent: replays scripted actions and records every prompt it saw."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.actions.pop(0))


def llm_for(script):
    llm = LLM(retries=1, providers=[FakeProvider(respond=script)])
    llm.backoff = 0
    return llm


def test_tools_show_the_product_split(engine, config):
    anomaly = battery_spike_on_one_product(engine)
    tools = Tools(engine, config, anomaly, {})

    rows = tools.run("breakdown", {"by": "product", "issue": "battery_life"})["rows"]
    assert rows[0] == {"name": "Airdopes 141", "recent_reviews": 6, "recent_hits": 6,
                       "baseline_reviews": 0, "baseline_hits": 0}

    compare = tools.run("compare_competitors", {"issue": "battery_life"})["brands"]
    assert compare["boat"]["recent"] == {"reviews": 15, "complaints": 6, "share": 0.4}
    assert compare["noise"]["recent"]["complaints"] == 0

    reviews = tools.run("query_reviews", {"issue": "battery_life", "sentiment": "negative", "limit": 2})
    assert [r["ref"] for r in reviews["reviews"]] == ["m1", "m2"]
    assert tools.run("issue_breakdown", {})["negative_mentions"]["battery_life"] == {"recent": 6, "baseline": 2}

    assert "unknown issue" in tools.run("query_reviews", {"issue": "power"})["error"]
    assert "unknown tool" in tools.run("delete_everything", {})["error"]


def test_agent_asks_for_data_then_resumes_and_concludes(engine, config, monkeypatch):
    for var in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)
    anomaly = battery_spike_on_one_product(engine)
    script = Script([
        {"thought": "one product?", "action": "call_tool", "tool": "breakdown",
         "args": {"by": "product", "issue": "battery_life"}},
        {"thought": "read them", "action": "call_tool", "tool": "query_reviews",
         "args": {"issue": "battery_life", "sentiment": "negative", "limit": 3}},
        {"thought": "need ops data", "action": "request_user_data",
         "question": "Did the Airdopes 141 battery supplier change in August?",
         "why": "A supplier change would explain a single-product spike."},
        {"thought": "confirmed", "action": "final", "diagnosis": {
            "summary": "Battery complaints are confined to Airdopes 141.",
            "hypotheses": [
                {"cause": "New battery cell batch on Airdopes 141", "confidence": 0.8,
                 "evidence": ["m1", "m2", "m99"], "reasoning": "All complaints are on one SKU.",
                 "would_confirm": "Return rates by batch"},
                {"cause": "Firmware update drains battery", "confidence": 0.6,
                 "evidence": [], "reasoning": "Guess"},
            ],
            "next_checks": ["Pull return reasons for Airdopes 141"]}},
    ])
    llm = llm_for(script)

    first = consultant.investigate(engine, llm, config)
    assert (first["started"], first["waiting"]) == (1, [first["_diagnoses"][0].id])
    [request] = consultant.open_requests(engine)
    assert request["question"].startswith("Did the Airdopes 141")

    waiting_email = alerts.send_diagnoses(engine, config, first["_diagnoses"])
    assert waiting_email == "logged only (Gmail not configured)"

    # Nothing happens until the question is answered.
    assert consultant.investigate(engine, llm, config)["done"] == []
    consultant.answer(engine, request["id"], "Yes, new cell supplier from 20 August.")
    second = consultant.investigate(engine, llm, config)
    [d] = second["_diagnoses"]
    assert d.status == "done" and second["done"] == [d.id]
    assert "new cell supplier" in script.prompts[-1]  # the answer reached the agent

    top, weak = d.result["hypotheses"]
    assert top["evidence"] == ["m1", "m2"]  # m99 was never shown, so it is removed
    assert (weak["confidence"], weak["supported"]) == (0.3, False)
    assert len(d.result["problems"]) == 2

    report = diagnosis.format_diagnosis(engine, config, d)
    assert "New battery cell batch on Airdopes 141 (confidence 80%)" in report
    assert '"boat review' in report  # real review text as evidence
    assert "[no valid evidence]" in report

    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM anomalies WHERE id = :id"),
                            {"id": anomaly.id}).scalar() == "diagnosed"


def test_agent_gives_up_after_max_steps_and_recovers_from_bad_replies(engine, config):
    battery_spike_on_one_product(engine)
    script = Script(
        [{"action": "dance"}]
        + [{"action": "call_tool", "tool": "issue_breakdown", "args": {}}] * diagnosis.MAX_STEPS
    )
    outcome = consultant.investigate(engine, llm_for(script), config)
    [d] = outcome["_diagnoses"]
    assert (d.status, d.steps) == ("gave_up", diagnosis.MAX_STEPS)
    errors = [t for t in d.transcript if t["role"] == "system"]
    assert "call_tool, request_user_data or final" in errors[0]["error"]
    assert "could not reach a conclusion" in diagnosis.format_diagnosis(engine, config, d)


def test_llm_outage_keeps_progress_for_next_run(engine, config):
    battery_spike_on_one_product(engine)

    def down(prompt):
        raise RuntimeError("503")

    [d] = consultant.investigate(engine, llm_for(down), config)["_diagnoses"]
    assert d.status == "running"
    script = Script([{"action": "final", "diagnosis": {"summary": "ok", "hypotheses": []}}])
    again = consultant.investigate(engine, llm_for(script), config)
    assert again["done"] == [d.id]
