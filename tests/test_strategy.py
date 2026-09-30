import json
from datetime import datetime, timezone

from brain import recommender, teardown
from brain.diagnosis import Diagnosis
from brain.perception import build_radar
from core.llm import LLM
from tests.fakes import FakeProvider
from tests.test_diagnosis import battery_spike_on_one_product
from tests.test_perception import add

TODAY = datetime(2026, 9, 1, tzinfo=timezone.utc)


def market(engine):
    # Copy: Noise battery is much better than boAt's.
    add(engine, "boat", 6, "battery_life", "negative")
    add(engine, "boat", 2, "battery_life", "positive")
    add(engine, "noise", 6, "battery_life", "positive")
    # Fight: boAt bass beats everyone.
    add(engine, "boat", 6, "bass", "positive")
    add(engine, "noise", 5, "bass", "negative")
    add(engine, "jbl", 5, "bass", "negative")
    # Gap: everyone's app is bad.
    for brand in ("boat", "noise", "jbl"):
        add(engine, brand, 5, "app_software", "negative")
    add(engine, "jbl", 1, "app_software", "positive")


def test_candidates_find_copy_fight_and_gaps(engine, config):
    market(engine)
    cands = teardown.candidates(build_radar(engine, config, 90, TODAY), config)
    assert [(c["competitor"], c["issue"]) for c in cands["copy"]] == [("noise", "battery_life")]
    assert cands["copy"][0]["their_net"] == 1.0
    assert [f["issue"] for f in cands["fight"]] == ["bass"]
    assert [g["issue"] for g in cands["gaps"]] == ["app_software"]
    assert cands["gaps"][0]["reviews"] == 16


def test_teardown_validates_llm_output(engine, config, monkeypatch):
    market(engine)
    monkeypatch.setattr(teardown, "build_radar", lambda e, c, d: build_radar(e, c, d, TODAY))

    def strategist(prompt):
        return json.dumps({
            "headline": "boAt wins on bass, loses on battery.",
            "copy": [{"competitor": "noise", "issue": "battery_life", "insight": "Noise lasts longer",
                      "action": "Bigger cells", "evidence": ["q1", "q77"]},
                     {"competitor": "jbl", "issue": "anc", "insight": "made up", "action": "x"}],
            "fight": [{"issue": "bass", "insight": "Loved", "action": "Advertise bass", "evidence": ["q4"]}],
            "gaps": [{"issue": "app_software", "insight": "All apps crash",
                      "launch_idea": "Stable app", "evidence": ["q7"]}],
        })

    llm = LLM(retries=1, providers=[FakeProvider(respond=strategist)])
    result = teardown.build_teardown(engine, config, llm, 90)
    assert [c["issue"] for c in result["llm"]["copy"]] == ["battery_life"]  # invented jbl/anc item dropped
    assert result["llm"]["copy"][0]["evidence"] == ["q1"]  # invented q77 removed
    assert len(result["problems"]) == 2

    report = teardown.format_teardown(engine, config, result)
    assert "**Noise on battery_life**: them +1.00 vs boAt -0.50" in report
    assert "Launch idea: Stable app" in report
    assert '> "noise battery_life positive' in report
    assert teardown.save(engine, result).startswith("teardown-")


def test_teardown_without_llm_is_math_only(engine, config, monkeypatch):
    market(engine)
    monkeypatch.setattr(teardown, "build_radar", lambda e, c, d: build_radar(e, c, d, TODAY))
    result = teardown.build_teardown(engine, config, None, 90)
    assert result["llm"] is None
    assert "- **bass**: boAt +1.00 vs competitors -1.00" in teardown.format_teardown(engine, config, result)


def test_priority_formula():
    assert recommender.priority("high", "low", 0.9) == ("P1", 2.7)
    assert recommender.priority("high", "high", 0.9) == ("P2", 0.9)
    assert recommender.priority("medium", "medium", 0.5) == ("P3", 0.5)


def test_recommend_scores_and_flags_uncertain_causes(engine, config):
    anomaly = battery_spike_on_one_product(engine)
    d = Diagnosis("diag-1", anomaly.id, "done", 3, [], {}, {
        "summary": "One SKU.", "next_checks": [], "evidence_ids": {}, "problems": [],
        "hypotheses": [
            {"cause": "Bad cell batch", "confidence": 0.9, "evidence": ["m1"], "reasoning": "",
             "would_confirm": "Batch return rates", "supported": True},
            {"cause": "Firmware drain", "confidence": 0.3, "evidence": [], "reasoning": "",
             "would_confirm": "Firmware changelog", "supported": False},
        ]})
    from brain.diagnosis import _save
    _save(engine, d, "diagnosed")

    def advisor(prompt):
        assert "Bad cell batch (confidence 90%)" in prompt
        return json.dumps({"recommendations": [
            {"action": "Quarantine the batch", "hypothesis": 1, "owner": "quality",
             "impact": "high", "effort": "low", "risk": "Stock-out", "metric": "Returns"},
            {"action": "Roll back firmware", "hypothesis": 2, "owner": "product",
             "impact": "medium", "effort": "medium", "risk": "", "metric": ""},
            {"action": "Nonsense", "hypothesis": 9, "impact": "huge", "effort": "low"},
        ]})

    recs = recommender.recommend(engine, LLM(retries=1, providers=[FakeProvider(respond=advisor)]), config, d)
    assert [(r["action"], r["priority"]) for r in recs] == [("Quarantine the batch", "P1"),
                                                            ("Roll back firmware", "P3")]
    assert recs[0]["confirm_first"] is None
    assert recs[1]["confirm_first"] == "Firmware changelog"

    text = recommender.format_recommendations(recs)
    assert "[P1] Quarantine the batch" in text and "Confirm first: Firmware changelog" in text
    # Saved, so a second call does not ask the LLM again.
    assert recommender.recommend(engine, None, config, d) == recs


def test_recommend_reports_llm_outage_and_is_found_for_retry(engine, config):
    anomaly = battery_spike_on_one_product(engine)
    d = Diagnosis("diag-2", anomaly.id, "done", 1, [], {}, {
        "summary": "x", "next_checks": [], "evidence_ids": {}, "problems": [],
        "hypotheses": [{"cause": "c", "confidence": 0.9, "evidence": [], "reasoning": "",
                        "would_confirm": "", "supported": False}]})
    from brain.diagnosis import _save
    _save(engine, d, "diagnosed")

    def down(prompt):
        raise RuntimeError("503")

    llm = LLM(retries=1, providers=[FakeProvider(respond=down)])
    llm.backoff = 0
    assert recommender.recommend(engine, llm, config, d) is None
    assert recommender.missing(engine) == ["diag-2"]


def test_less_bad_is_not_a_strength(engine, config):
    add(engine, "boat", 6, "battery_life", "negative")
    add(engine, "boat", 4, "battery_life", "positive")  # boAt -0.2
    add(engine, "noise", 9, "battery_life", "negative")
    add(engine, "noise", 1, "battery_life", "positive")  # Noise -0.8
    radar = build_radar(engine, config, 90, TODAY)
    assert radar.strengths == []
    assert teardown.candidates(radar, config)["fight"] == []
