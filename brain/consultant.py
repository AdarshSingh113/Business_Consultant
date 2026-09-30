"""The consultant: decides what to investigate, and talks to you.

- investigate(): starts diagnoses for new anomalies (client problems first) and resumes
  ones that were waiting for your data. Called by the daily job.
- Questions the agent asks you are stored in data_requests. Answer them from the
  command line (Phase 6 adds a dashboard page for this):

    python -m brain.consultant requests                          list open questions
    python -m brain.consultant answer req-1a2b3c4d "No changes"  answer in words
    python -m brain.consultant answer req-1a2b3c4d --file returns.csv
    python -m brain.consultant show                              latest diagnoses
    python -m brain.consultant diagnose <anomaly_id>             run one now
"""

import argparse
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain import diagnosis
from core import db
from core.config import Config, load_config
from core.llm import LLM
from core.text import now_iso

MAX_NEW_PER_RUN = 3  # new investigations per daily run, to stay inside free LLM quotas
MAX_ANSWER_CHARS = 20_000  # a pasted CSV is cut to this size before the agent sees it


def investigate(engine: Engine, llm: LLM, config: Config) -> dict:
    """Resume paused diagnoses, then start new ones. Returns the diagnoses touched."""
    touched: list[diagnosis.Diagnosis] = []
    with engine.connect() as conn:
        paused = [r[0] for r in conn.execute(
            text("SELECT id FROM diagnoses WHERE status IN ('waiting', 'running') ORDER BY created_at")
        )]
        client_first = "CASE WHEN a.brand_id = :client THEN 0 ELSE 1 END"
        fresh = [r[0] for r in conn.execute(
            text(f"SELECT a.id FROM anomalies a WHERE a.status IN ('new', 'alerted') "
                 f"ORDER BY {client_first}, a.z_score DESC LIMIT :limit"),
            {"client": config.client.id, "limit": MAX_NEW_PER_RUN},
        )]

    for diagnosis_id in paused:
        before = diagnosis.load_diagnosis(engine, diagnosis_id)
        after = diagnosis.resume(engine, llm, config, diagnosis_id)
        if after.status != before.status or after.steps != before.steps:
            touched.append(after)
    for anomaly_id in fresh:
        touched.append(diagnosis.start(engine, llm, config, anomaly_id))

    return {
        "resumed": len(paused),
        "started": len(fresh),
        "done": [d.id for d in touched if d.status == "done"],
        "waiting": [d.id for d in touched if d.status == "waiting"],
        "gave_up": [d.id for d in touched if d.status == "gave_up"],
        "_diagnoses": touched,
    }


def open_requests(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT r.id, r.question, r.why, r.created_at, a.brand_id, a.kind, a.issue "
            "FROM data_requests r JOIN diagnoses d ON d.id = r.diagnosis_id "
            "JOIN anomalies a ON a.id = d.anomaly_id WHERE r.status = 'open' ORDER BY r.created_at"
        ))
        return [dict(r._mapping) for r in rows]


def answer(engine: Engine, request_id: str, answer_text: str) -> None:
    answer_text = answer_text.strip()
    if not answer_text:
        raise ValueError("answer is empty")
    if len(answer_text) > MAX_ANSWER_CHARS:
        answer_text = answer_text[:MAX_ANSWER_CHARS] + "\n[truncated]"
    with engine.begin() as conn:
        updated = conn.execute(
            text("UPDATE data_requests SET status = 'answered', answer = :a, answered_at = :now "
                 "WHERE id = :id AND status = 'open'"),
            {"a": answer_text, "now": now_iso(), "id": request_id},
        ).rowcount
    if not updated:
        raise ValueError(f"no open request {request_id!r}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Talk to the consultant agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("requests", help="list questions waiting for you")
    ans = sub.add_parser("answer", help="answer a question")
    ans.add_argument("request_id")
    ans.add_argument("text", nargs="?", default="")
    ans.add_argument("--file", help="attach a CSV or text file as the answer")
    sub.add_parser("show", help="print the latest diagnoses")
    one = sub.add_parser("diagnose", help="investigate one anomaly now")
    one.add_argument("anomaly_id")
    args = parser.parse_args()

    config = load_config()
    engine = db.get_engine()
    db.init_db(engine)

    if args.command == "requests":
        pending = open_requests(engine)
        if not pending:
            print("No open questions.")
        for r in pending:
            print(f"[{r['id']}] {r['brand_id']} {r['issue'] or r['kind']}\n  Q: {r['question']}\n  Why: {r['why']}\n")
    elif args.command == "answer":
        body = args.text
        if args.file:
            body = (body + "\n\n" if body else "") + f"File {Path(args.file).name}:\n" + Path(args.file).read_text(encoding="utf-8")
        answer(engine, args.request_id, body)
        print("Saved. The investigation resumes on the next daily run, or now with:\n"
              "  python -m jobs.daily")
    elif args.command == "show":
        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(text("SELECT id FROM diagnoses ORDER BY updated_at DESC LIMIT 5"))]
        if not ids:
            print("No diagnoses yet.")
        for diagnosis_id in ids:
            print(diagnosis.format_diagnosis(engine, config, diagnosis.load_diagnosis(engine, diagnosis_id)))
            print("-" * 60)
    else:
        d = diagnosis.start(engine, LLM(engine), config, args.anomaly_id)
        print(diagnosis.format_diagnosis(engine, config, d))


if __name__ == "__main__":
    main()
