"""Output 2: Diagnosis. The consultant agent works out why an anomaly happened.

The loop, in plain terms:

    show the LLM the anomaly and the tools
    repeat up to MAX_STEPS times:
        LLM replies with one action as JSON
        "call_tool"          -> run it, add the result to the transcript, continue
        "request_user_data"  -> save the question, status = waiting, stop
        "final"              -> check the evidence, save the diagnosis, stop
    out of steps -> status = gave_up (logged, never silent)

A waiting diagnosis resumes on a later run once every question is answered.

The LLM decides *what to look at*. The code decides *what counts*: evidence
must be refs the agent was actually shown, and a hypothesis without valid
evidence has its confidence capped.
"""

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain.detector import BASELINE_DAYS, RECENT_DAYS, Anomaly
from brain.tools import TOOL_DOCS, Tools
from core.config import Config
from core.llm import LLM, LLMError
from core.text import now_iso

MAX_STEPS = 10  # LLM calls per investigation, across all resumes
MAX_REQUESTS = 2  # questions to the brand team per investigation
UNSUPPORTED_CONFIDENCE = 0.3  # cap for a hypothesis with no valid evidence


@dataclass
class Diagnosis:
    id: str
    anomaly_id: str
    status: str
    steps: int
    transcript: list[dict]
    refs: dict[str, str]
    result: dict | None = None


def system_prompt(config: Config) -> str:
    return f"""You are a brand consultant investigating a problem for {config.client.name}, \
a {config.category} brand. Competitors: {", ".join(b.name for b in config.competitors)}.

Find the most likely root causes of the anomaly using the tools. Work like an analyst:
form hypotheses, then check them against the data. Useful questions: Is it one product or
all of them? One source or all? Did the whole market move, or only this brand? What exactly
do people say?

Tools:
{TOOL_DOCS}

Rules:
- Reply with exactly one JSON object per turn, nothing else.
- To use a tool: {{"thought": "...", "action": "call_tool", "tool": "<name>", "args": {{...}}}}
- To ask the brand team: {{"thought": "...", "action": "request_user_data",
  "question": "...", "why": "how the answer would change your conclusion"}}
- To finish: {{"thought": "...", "action": "final", "diagnosis": {{
    "summary": "two or three sentences",
    "hypotheses": [{{"cause": "...", "confidence": 0.0-1.0, "evidence": ["m1", "m4"],
                     "reasoning": "...", "would_confirm": "what data would prove or disprove this"}}],
    "next_checks": ["..."]}}}}
- Evidence must be review refs (m1, m2, ...) returned by your tool calls. Never invent refs.
- Reviews show correlation, not proof. Say "likely", give honest confidence, and name what
  would confirm it. Several hypotheses are fine; order them by confidence.
- Finish within {MAX_STEPS} turns."""


def anomaly_brief(a: Anomaly, config: Config) -> str:
    brand = config.brand(a.brand_id)
    role = "our client" if brand.is_client else "a competitor"
    return (
        f"Anomaly: {a.what} for {brand.name} ({role}).\n"
        f"Last {RECENT_DAYS} days ({a.window_start} to {a.window_end}): "
        f"{a.recent_hits} of {a.recent_total} reviews ({a.recent_share:.0%}).\n"
        f"Previous {BASELINE_DAYS} days: {a.baseline_hits} of {a.baseline_total} "
        f"({a.baseline_share:.0%}). z = {a.z_score}.\n"
        + ("" if a.issue is None else f"Issue label: {a.issue}.\n")
        + "Investigate why."
    )


def build_prompt(a: Anomaly, config: Config, transcript: list[dict]) -> str:
    history = "\n".join(json.dumps(turn, ensure_ascii=False) for turn in transcript)
    return f"{anomaly_brief(a, config)}\n\nTranscript so far (oldest first):\n{history or '(empty)'}\n\nYour next action:"


def validate_diagnosis(raw: dict, refs: dict[str, str]) -> tuple[dict, list[str]]:
    """Keep only evidence the agent was shown; cap confidence where evidence is missing."""
    problems = []
    hypotheses = []
    for h in raw.get("hypotheses") or []:
        if not isinstance(h, dict) or not h.get("cause"):
            problems.append("hypothesis without a cause dropped")
            continue
        cited = [str(r) for r in h.get("evidence") or []]
        valid = [r for r in cited if r in refs]
        if len(valid) < len(cited):
            problems.append(f"invented refs removed: {sorted(set(cited) - set(valid))}")
        try:
            confidence = min(max(float(h.get("confidence", 0)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not valid and confidence > UNSUPPORTED_CONFIDENCE:
            problems.append(f"no valid evidence for {h['cause'][:40]!r}, confidence capped")
            confidence = UNSUPPORTED_CONFIDENCE
        hypotheses.append({
            "cause": str(h["cause"]),
            "confidence": round(confidence, 2),
            "evidence": valid,
            "reasoning": str(h.get("reasoning", "")),
            "would_confirm": str(h.get("would_confirm", "")),
            "supported": bool(valid),
        })
    hypotheses.sort(key=lambda h: -h["confidence"])
    return {
        "summary": str(raw.get("summary", "")),
        "hypotheses": hypotheses,
        "next_checks": [str(c) for c in raw.get("next_checks") or []],
        "evidence_ids": {r: refs[r] for h in hypotheses for r in h["evidence"]},
        "problems": problems,
    }, problems


# ---- storage ---------------------------------------------------------------------------

def load_anomaly(engine: Engine, anomaly_id: str) -> Anomaly:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT brand_id, kind, issue, window_start, window_end, recent_hits, recent_total, "
                 "baseline_hits, baseline_total, z_score FROM anomalies WHERE id = :id"),
            {"id": anomaly_id},
        ).one()
    return Anomaly(*row)


def load_diagnosis(engine: Engine, diagnosis_id: str) -> Diagnosis:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, anomaly_id, status, steps, transcript, refs, result "
                 "FROM diagnoses WHERE id = :id"),
            {"id": diagnosis_id},
        ).one()
    return Diagnosis(row[0], row[1], row[2], row[3], json.loads(row[4]), json.loads(row[5]),
                     json.loads(row[6]) if row[6] else None)


def _save(engine: Engine, d: Diagnosis, anomaly_status: str) -> None:
    now = now_iso()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO diagnoses (id, anomaly_id, status, steps, transcript, refs, result,
                                       created_at, updated_at)
                VALUES (:id, :anomaly_id, :status, :steps, :transcript, :refs, :result, :now, :now)
                ON CONFLICT (id) DO UPDATE SET
                    status = excluded.status, steps = excluded.steps,
                    transcript = excluded.transcript, refs = excluded.refs,
                    result = excluded.result, updated_at = excluded.updated_at
                """
            ),
            {"id": d.id, "anomaly_id": d.anomaly_id, "status": d.status, "steps": d.steps,
             "transcript": json.dumps(d.transcript, ensure_ascii=False),
             "refs": json.dumps(d.refs), "result": json.dumps(d.result) if d.result else None,
             "now": now},
        )
        conn.execute(text("UPDATE anomalies SET status = :s WHERE id = :id"),
                     {"s": anomaly_status, "id": d.anomaly_id})


def _add_request(engine: Engine, d: Diagnosis, question: str, why: str) -> str:
    request_id = f"req-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO data_requests (id, diagnosis_id, question, why, status, created_at) "
                 "VALUES (:id, :d, :q, :w, 'open', :now)"),
            {"id": request_id, "d": d.id, "q": question, "w": why, "now": now_iso()},
        )
    return request_id


def requests_for(engine: Engine, diagnosis_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, question, why, status, answer FROM data_requests "
                 "WHERE diagnosis_id = :d ORDER BY created_at, id"),
            {"d": diagnosis_id},
        )
        return [dict(r._mapping) for r in rows]


# ---- the loop --------------------------------------------------------------------------

def start(engine: Engine, llm: LLM, config: Config, anomaly_id: str) -> Diagnosis:
    d = Diagnosis(f"diag-{uuid.uuid4().hex[:10]}", anomaly_id, "running", 0, [], {})
    _save(engine, d, "diagnosing")
    return run(engine, llm, config, d)


def resume(engine: Engine, llm: LLM, config: Config, diagnosis_id: str) -> Diagnosis:
    """Continue a diagnosis: waiting ones once every question is answered, and
    running ones that stopped early because the LLM was unavailable."""
    d = load_diagnosis(engine, diagnosis_id)
    requests = requests_for(engine, d.id)
    if d.status not in ("waiting", "running") or any(r["status"] != "answered" for r in requests):
        return d
    answered = {t.get("request_id") for t in d.transcript if t.get("role") == "brand_team"}
    for r in requests:
        if r["id"] not in answered:
            d.transcript.append({"role": "brand_team", "request_id": r["id"],
                                 "question": r["question"], "answer": r["answer"]})
    d.status = "running"
    return run(engine, llm, config, d)


def run(engine: Engine, llm: LLM, config: Config, d: Diagnosis) -> Diagnosis:
    anomaly = load_anomaly(engine, d.anomaly_id)
    tools = Tools(engine, config, anomaly, d.refs)
    system = system_prompt(config)

    while d.steps < MAX_STEPS:
        d.steps += 1
        try:
            reply = llm.complete_json(build_prompt(anomaly, config, d.transcript), system,
                                      use_cache=False).data
        except LLMError as exc:
            d.transcript.append({"role": "system", "error": str(exc)[:300]})
            break  # save progress; the next run continues from here
        action = reply.get("action")
        d.transcript.append({"role": "agent", **reply})

        if action == "call_tool":
            if reply.get("tool") == "request_user_data":  # accept either spelling
                action = "request_user_data"
                reply = {**reply, **(reply.get("args") or {})}
            else:
                result = tools.run(str(reply.get("tool")), reply.get("args") or {})
                d.transcript.append({"role": "tool", "tool": reply.get("tool"), "result": result})
                continue

        if action == "request_user_data":
            asked = sum(1 for t in d.transcript if t.get("role") == "request")
            if asked >= MAX_REQUESTS or not reply.get("question"):
                d.transcript.append({"role": "system", "error":
                                     f"No more data requests allowed ({MAX_REQUESTS} max). Finish with what you have."})
                continue
            request_id = _add_request(engine, d, str(reply["question"]), str(reply.get("why", "")))
            d.transcript.append({"role": "request", "request_id": request_id})
            d.status = "waiting"
            _save(engine, d, "diagnosing")
            return d

        if action == "final" and isinstance(reply.get("diagnosis"), dict):
            d.result, problems = validate_diagnosis(reply["diagnosis"], d.refs)
            d.status = "done"
            _save(engine, d, "diagnosed")
            return d

        d.transcript.append({"role": "system", "error":
                             'Reply with one JSON object whose "action" is call_tool, request_user_data or final.'})

    if d.steps >= MAX_STEPS:
        d.status = "gave_up"
        _save(engine, d, "diagnosed")
    else:
        _save(engine, d, "diagnosing")  # LLM error: stays running, retried next run
    return d


def format_diagnosis(engine: Engine, config: Config, d: Diagnosis) -> str:
    """Human-readable report, with the quoted evidence text."""
    anomaly = load_anomaly(engine, d.anomaly_id)
    lines = [f"DIAGNOSIS: {config.brand(anomaly.brand_id).name} {anomaly.what} "
             f"({anomaly.baseline_share:.0%} -> {anomaly.recent_share:.0%})"]
    if d.status == "waiting":
        lines.append("Waiting for data from you:")
        for r in requests_for(engine, d.id):
            if r["status"] == "open":
                lines.append(f"  [{r['id']}] {r['question']}\n      Why: {r['why']}")
        lines.append("Answer with: python -m brain.consultant answer <id> \"your answer\" "
                     "(or --file data.csv)")
        return "\n".join(lines)
    if d.status == "gave_up" or not d.result:
        lines.append(f"The agent could not reach a conclusion in {d.steps} steps ({d.status}).")
        return "\n".join(lines)

    ids = d.result.get("evidence_ids", {})
    texts = {}
    if ids:
        params = {f"i{n}": mid for n, mid in enumerate(ids.values())}
        with engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT id, text FROM mentions WHERE id IN ({', '.join(':' + k for k in params)})"),
                params,
            )
            texts = dict(rows.all())
    lines.append(d.result["summary"])
    for n, h in enumerate(d.result["hypotheses"], start=1):
        flag = "" if h["supported"] else "  [no valid evidence]"
        lines.append(f"\n{n}. {h['cause']} (confidence {h['confidence']:.0%}){flag}")
        if h["reasoning"]:
            lines.append(f"   Why: {h['reasoning']}")
        for ref in h["evidence"][:3]:
            lines.append(f'   - "{texts.get(ids.get(ref), "?")[:150]}"')
        if h["would_confirm"]:
            lines.append(f"   To confirm: {h['would_confirm']}")
    if d.result["next_checks"]:
        lines.append("\nNext checks: " + "; ".join(d.result["next_checks"]))
    return "\n".join(lines)
