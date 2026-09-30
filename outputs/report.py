"""The weekly consultant report: everything a brand manager needs in one read.

Sections: Perception Radar, this week's alerts and diagnoses (with recommended
actions), questions waiting for you, competitor teardown, pipeline health.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain import diagnosis, teardown
from brain.consultant import open_requests
from brain.perception import build_radar, format_radar
from core.config import Config
from core.llm import LLM
from core.text import now_iso


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def pipeline_health(engine: Engine, days: int = 7) -> str:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT status, started_at, errors FROM runs WHERE job = 'daily' "
                 "AND started_at >= :since ORDER BY started_at DESC"),
            {"since": _since(days)},
        ).all()
    if not rows:
        return f"**No daily runs in the last {days} days.** Check the GitHub Actions schedule."
    counts: dict[str, int] = {}
    for status, _, _ in rows:
        counts[status] = counts.get(status, 0) + 1
    lines = [f"{len(rows)} daily runs: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))]
    for status, started, errors in rows:
        for error in json.loads(errors or "[]")[:2]:
            lines.append(f"- {started[:10]} {status}: {error[:160]}")
    return "\n".join(lines[:8])


def build_report(engine: Engine, config: Config, llm: LLM | None, days: int = 30) -> tuple[str, str]:
    """Return (subject, markdown body)."""
    radar = build_radar(engine, config, days)
    client = radar.brands[config.client.id]
    week = _since(7)

    with engine.connect() as conn:
        anomalies = conn.execute(
            text("SELECT brand_id, kind, issue, recent_hits, recent_total, baseline_hits, "
                 "baseline_total, z_score, status FROM anomalies WHERE created_at >= :s "
                 "ORDER BY z_score DESC"),
            {"s": week},
        ).all()
        diagnosis_ids = [r[0] for r in conn.execute(
            text("SELECT id FROM diagnoses WHERE updated_at >= :s AND status = 'done' "
                 "ORDER BY updated_at DESC"),
            {"s": week},
        )]

    names = {b.id: b.name for b in config.brands}
    subject = (f"[{config.client.name} consultant] Weekly report: {len(anomalies)} alert(s), "
               f"net sentiment {client.net:+.2f}")
    parts = [f"# Weekly consultant report: {config.client.name}",
             f"Generated {now_iso()[:10]}. Radar covers the last {days} days.", ""]

    parts += ["## This week's alerts", ""]
    if not anomalies:
        parts.append("No unusual changes detected this week.")
    for brand_id, kind, issue, rh, rt, bh, bt, z, status in anomalies:
        role = "Problem" if brand_id == config.client.id else "Opportunity"
        what = f"{issue} complaints" if kind == "complaint_spike" else "1-2 star reviews"
        parts.append(f"- **{role}: {names.get(brand_id, brand_id)} {what}** "
                     f"{bh / bt if bt else 0:.0%} -> {rh / rt:.0%} (z {z}, {status})")

    parts += ["", "## Diagnoses and recommended actions", ""]
    if not diagnosis_ids:
        parts.append("No investigations finished this week.")
    for diagnosis_id in diagnosis_ids:
        d = diagnosis.load_diagnosis(engine, diagnosis_id)
        parts += ["```", diagnosis.format_diagnosis(engine, config, d), "```", ""]

    requests = open_requests(engine)
    parts += ["## Questions waiting for you", ""]
    if not requests:
        parts.append("None.")
    for r in requests:
        parts.append(f"- `{r['id']}` {r['question']}\n  Why it matters: {r['why']}")

    td = teardown.build_teardown(engine, config, llm, days=90)
    teardown.save(engine, td)
    parts += ["", teardown.format_teardown(engine, config, td).replace("# Competitor", "## Competitor", 1)
              .replace("\n## ", "\n### ")]

    parts += ["", format_radar(radar, config).replace("# Perception", "## Perception", 1)
              .replace("\n## ", "\n### ")]
    parts += ["", "## Pipeline health", "", pipeline_health(engine)]
    return subject, "\n".join(parts)


def save(engine: Engine, body: str, since: str) -> str:
    report_id = f"weekly-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO reports (id, kind, since, content, created_at) "
                 "VALUES (:id, 'weekly', :since, :content, :now)"),
            {"id": report_id, "since": since, "content": body, "now": now_iso()},
        )
    return report_id
