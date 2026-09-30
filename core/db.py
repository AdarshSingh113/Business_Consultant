"""Database access. SQLite locally, Postgres (Supabase) when DATABASE_URL is set.

Run `python -m core.db init` to create tables and `python -m core.db stats`
to see what has been collected.
"""

import argparse
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from core.config import ROOT, Config, load_config
from core.text import now_iso

SCHEMA_FILE = ROOT / "db" / "schema.sql"
LOCAL_DB = ROOT / "data" / "local.db"


@dataclass
class Mention:
    id: str
    brand_id: str
    source: str
    text: str
    product: str | None = None
    source_ref: str | None = None
    title: str | None = None
    rating: float | None = None
    posted_at: str | None = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        LOCAL_DB.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{LOCAL_DB}"
    # Supabase hands out postgres:// or postgresql:// URLs; point SQLAlchemy at psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), pool_pre_ping=True)


def describe(engine: Engine) -> str:
    """Engine URL with the password hidden, safe to print in logs."""
    return engine.url.render_as_string(hide_password=True)


def init_db(engine: Engine) -> None:
    """Create tables if they do not exist. Safe to run on every job."""
    sql = Path(SCHEMA_FILE).read_text(encoding="utf-8")
    # Drop "--" comments first so a ";" inside a comment can't split a statement.
    lines = [line.split("--", 1)[0] for line in sql.splitlines()]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        if engine.dialect.name == "postgresql":
            # Supabase exposes tables in the public schema through its REST API.
            # Row level security with no policies blocks that API, while this
            # direct connection (the table owner) keeps full access.
            for table in _table_names(statements):
                conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))


def _table_names(statements: list[str]) -> list[str]:
    prefix = "CREATE TABLE IF NOT EXISTS "
    return [s[len(prefix) :].split()[0] for s in statements if s.startswith(prefix)]


def sync_brands(engine: Engine, config: Config) -> None:
    """Copy brands from brands.yaml into the brands table."""
    with engine.begin() as conn:
        for b in config.brands:
            conn.execute(
                text(
                    """
                    INSERT INTO brands (id, name, category, is_client, updated_at)
                    VALUES (:id, :name, :category, :is_client, :updated_at)
                    ON CONFLICT (id) DO UPDATE SET
                        name = excluded.name,
                        category = excluded.category,
                        is_client = excluded.is_client,
                        updated_at = excluded.updated_at
                    """
                ),
                {
                    "id": b.id,
                    "name": b.name,
                    "category": config.category,
                    "is_client": b.is_client,
                    "updated_at": now_iso(),
                },
            )


def insert_mentions(engine: Engine, mentions: list[Mention], run_id: str | None = None) -> int:
    """Store new mentions and skip ones already in the database. Returns how many were new."""
    unique = list({m.id: m for m in mentions}.values())
    if not unique:
        return 0

    new: list[Mention] = []
    with engine.begin() as conn:
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            params = {f"id{i}": m.id for i, m in enumerate(chunk)}
            placeholders = ", ".join(f":{k}" for k in params)
            existing = {
                row[0]
                for row in conn.execute(
                    text(f"SELECT id FROM mentions WHERE id IN ({placeholders})"), params
                )
            }
            new.extend(m for m in chunk if m.id not in existing)

        if new:
            collected_at = now_iso()
            conn.execute(
                text(
                    """
                    INSERT INTO mentions (id, brand_id, product, source, source_ref, title,
                                          text, rating, posted_at, collected_at, run_id)
                    VALUES (:id, :brand_id, :product, :source, :source_ref, :title,
                            :text, :rating, :posted_at, :collected_at, :run_id)
                    ON CONFLICT (id) DO NOTHING
                    """
                ),
                [
                    {**m.__dict__, "collected_at": collected_at, "run_id": run_id}
                    for m in new
                ],
            )
    return len(new)


def start_run(engine: Engine, job: str) -> str:
    run_id = f"{job}-{uuid.uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, job, started_at, status) "
                "VALUES (:id, :job, :started_at, 'running')"
            ),
            {"id": run_id, "job": job, "started_at": now_iso()},
        )
    return run_id


def finish_run(engine: Engine, run_id: str, status: str, stats: dict, errors: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET finished_at = :finished_at, status = :status, "
                "stats = :stats, errors = :errors WHERE id = :id"
            ),
            {
                "id": run_id,
                "finished_at": now_iso(),
                "status": status,
                "stats": json.dumps(stats),
                "errors": json.dumps(errors),
            },
        )


def mention_counts(engine: Engine) -> list[tuple[str, str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT brand_id, source, COUNT(*) FROM mentions "
                "GROUP BY brand_id, source ORDER BY brand_id, source"
            )
        )
        return [(r[0], r[1], r[2]) for r in rows]


def recent_runs(engine: Engine, limit: int = 5) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, started_at, stats, errors FROM runs "
                "ORDER BY started_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [dict(r._mapping) for r in rows]


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Database helpers")
    parser.add_argument("command", choices=["init", "stats"])
    args = parser.parse_args()

    engine = get_engine()
    print(f"Database: {describe(engine)}")
    if args.command == "init":
        init_db(engine)
        sync_brands(engine, load_config())
        print("Tables created and brands synced.")
    else:
        counts = mention_counts(engine)
        if not counts:
            print("No mentions yet. Import a CSV or run the daily job.")
        for brand_id, source, n in counts:
            print(f"  {brand_id:<10} {source:<10} {n:>6}")
        print("\nRecent runs:")
        for run in recent_runs(engine):
            print(f"  {run['started_at']}  {run['status']:<8} {run['id']}  {run['stats']}")


if __name__ == "__main__":
    main()
