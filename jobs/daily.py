"""Daily job. Phase 1: collect. Later phases add tag -> detect -> diagnose.

Each collector runs on its own: if Reddit fails, CSV data still gets stored.
The run is logged to the `runs` table, and the process exits non-zero on any
error so a GitHub Actions run shows red instead of failing silently.

Run: python -m jobs.daily
"""

import sys
import traceback

from dotenv import load_dotenv

from collectors import csv_import, reddit
from core import db
from core.config import load_config


def run() -> int:
    config = load_config()
    engine = db.get_engine()
    print(f"Database: {db.describe(engine)}")
    db.init_db(engine)
    db.sync_brands(engine, config)

    run_id = db.start_run(engine, "daily")
    stats: dict = {}
    errors: list[str] = []

    # 1. Marketplace reviews from CSV files
    try:
        stats["csv"] = csv_import.import_folder(engine, config, run_id)
    except Exception as exc:
        errors.append(f"csv: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # 2. Reddit
    if not config.sources.get("reddit", {}).get("enabled", False):
        stats["reddit"] = "disabled in brands.yaml"
    elif not reddit.is_configured():
        stats["reddit"] = "skipped: no Reddit credentials"
    else:
        try:
            mentions, reddit_stats = reddit.fetch(config)
            reddit_stats["new"] = db.insert_mentions(engine, mentions, run_id)
            stats["reddit"] = reddit_stats
        except Exception as exc:
            errors.append(f"reddit: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # Phase 2+: tag new mentions, update perception scores, detect anomalies.

    status = "ok" if not errors else ("partial" if stats else "failed")
    db.finish_run(engine, run_id, status, stats, errors)

    print(f"Run {run_id}: {status}")
    for name, value in stats.items():
        print(f"  {name}: {value}")
    for error in errors:
        print(f"  ERROR {error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    load_dotenv()
    sys.exit(run())
