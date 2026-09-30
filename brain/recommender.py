"""Turn a finished diagnosis into prioritized actions.

The LLM proposes actions and rates each one's impact and effort. The code
decides the priority, so it is consistent and explainable:

    score = impact (1-3) x confidence in the cause (0-1) / effort (1-3)
    P1 >= 1.5, P2 >= 0.6, else P3

An action aimed at a cause the agent is unsure about (confidence < 0.6) is
marked "confirm first", with the check that would confirm it.
"""

import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain.diagnosis import Diagnosis, load_anomaly
from core.config import Config
from core.llm import LLM, LLMError

LEVELS = {"low": 1, "medium": 2, "high": 3}
OWNERS = {"product", "operations", "marketing", "customer_service", "quality"}
CONFIRM_BELOW = 0.6

SYSTEM = """You advise {client}, a {category} brand, on what to do about a diagnosed problem.
For the likely causes below, propose 2 to 5 concrete actions. For each give:
- hypothesis: the number of the cause it addresses
- owner: product, operations, marketing, customer_service or quality
- impact and effort: low, medium or high
- risk: what could go wrong if they do it, in one short sentence
- metric: the number to watch to know it worked
Prefer cheap, fast actions that also test the cause. Reply with JSON only:
{{"recommendations": [{{"action": "...", "hypothesis": 1, "owner": "quality", "impact": "high",
  "effort": "low", "risk": "...", "metric": "..."}}]}}"""


def priority(impact: str, effort: str, confidence: float) -> tuple[str, float]:
    score = LEVELS[impact] * confidence / LEVELS[effort]
    return ("P1" if score >= 1.5 else "P2" if score >= 0.6 else "P3"), round(score, 2)


def validate(raw: dict, hypotheses: list[dict]) -> tuple[list[dict], list[str]]:
    problems, out = [], []
    for r in raw.get("recommendations") or []:
        if not isinstance(r, dict) or not r.get("action"):
            continue
        impact, effort = str(r.get("impact", "")).lower(), str(r.get("effort", "")).lower()
        try:
            index = int(r.get("hypothesis", 0))
        except (TypeError, ValueError):
            index = 0
        if impact not in LEVELS or effort not in LEVELS or not 1 <= index <= len(hypotheses):
            problems.append(f"dropped {str(r.get('action'))[:40]!r}: bad impact/effort/hypothesis")
            continue
        h = hypotheses[index - 1]
        level, score = priority(impact, effort, h["confidence"])
        owner = str(r.get("owner", "")).lower()
        out.append({
            "action": str(r["action"]),
            "cause": h["cause"],
            "owner": owner if owner in OWNERS else "product",
            "impact": impact,
            "effort": effort,
            "priority": level,
            "score": score,
            "confirm_first": h["would_confirm"] if h["confidence"] < CONFIRM_BELOW else None,
            "risk": str(r.get("risk", "")),
            "metric": str(r.get("metric", "")),
        })
    out.sort(key=lambda r: -r["score"])
    return out, problems


def missing(engine: Engine) -> list[str]:
    """Done diagnoses that still have no recommendations (e.g. the LLM was down last time)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, result FROM diagnoses WHERE status = 'done'"))
        return [r[0] for r in rows if r[1] and "recommendations" not in json.loads(r[1])]


def recommend(engine: Engine, llm: LLM, config: Config, d: Diagnosis) -> list[dict] | None:
    """Add recommendations to a done diagnosis and save them. Returns them, or None if
    the LLM was unavailable (the daily job retries on its next run)."""
    if d.status != "done" or not d.result or not d.result.get("hypotheses"):
        return []
    if "recommendations" in d.result:
        return d.result["recommendations"]
    anomaly = load_anomaly(engine, d.anomaly_id)
    brand = config.brand(anomaly.brand_id)
    causes = "\n".join(
        f"{n}. {h['cause']} (confidence {h['confidence']:.0%}). {h['reasoning']}"
        for n, h in enumerate(d.result["hypotheses"], start=1)
    )
    framing = ("" if brand.is_client else
               f"This is a problem at competitor {brand.name}. Recommend how {config.client.name} "
               "can win their unhappy customers, not how to fix it for them.\n")
    prompt = (f"{framing}Problem: {brand.name} {anomaly.what} rose from {anomaly.baseline_share:.0%} "
              f"to {anomaly.recent_share:.0%}.\nSummary: {d.result['summary']}\nLikely causes:\n{causes}")
    try:
        raw = llm.complete_json(prompt, SYSTEM.format(client=config.client.name, category=config.category)).data
    except LLMError:
        return None
    recs, problems = validate(raw, d.result["hypotheses"])
    d.result["recommendations"] = recs
    d.result.setdefault("problems", []).extend(problems)
    with engine.begin() as conn:
        conn.execute(text("UPDATE diagnoses SET result = :r WHERE id = :id"),
                     {"r": json.dumps(d.result), "id": d.id})
    return recs


def format_recommendations(recs: list[dict]) -> str:
    if not recs:
        return ""
    lines = ["\nRecommended actions:"]
    for r in recs:
        lines.append(f"  [{r['priority']}] {r['action']}  ({r['owner']}; impact {r['impact']}, effort {r['effort']})")
        if r["confirm_first"]:
            lines.append(f"       Confirm first: {r['confirm_first']}")
        if r["risk"]:
            lines.append(f"       Risk: {r['risk']}")
        if r["metric"]:
            lines.append(f"       Watch: {r['metric']}")
    return "\n".join(lines)
