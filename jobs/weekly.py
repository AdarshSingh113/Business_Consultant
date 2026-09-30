"""Weekly job: build the full consultant report, save it, email it.

Run: python -m jobs.weekly
"""

import sys
import traceback

from dotenv import load_dotenv

from core import db
from core.config import load_config
from core.llm import LLM, LLMError
from outputs import alerts, report


def run() -> int:
    config = load_config()
    engine = db.get_engine()
    print(f"Database: {db.describe(engine)}")
    db.init_db(engine)
    db.sync_brands(engine, config)
    run_id = db.start_run(engine, "weekly")
    try:
        try:
            llm = LLM(engine)
        except LLMError:
            llm = None  # the report still works; the teardown is math-only
        subject, body = report.build_report(engine, config, llm)
        report_id = report.save(engine, body, since="30d")
        delivery = alerts.deliver(subject, body)
        db.finish_run(engine, run_id, "ok", {"report": report_id, "delivery": delivery}, [])
        return 0
    except Exception as exc:
        traceback.print_exc()
        db.finish_run(engine, run_id, "failed", {}, [f"{type(exc).__name__}: {exc}"])
        return 1


if __name__ == "__main__":
    load_dotenv()
    sys.exit(run())
