import os

import pytest
from sqlalchemy import text

from core import db
from core.config import load_config


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def engine(tmp_path, config):
    """A fresh database per test: SQLite by default, or Postgres (what Supabase runs)
    when TEST_DATABASE_URL is set, e.g. postgresql://postgres@localhost:5432/postgres"""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        engine = db.get_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        engine = db.get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    db.sync_brands(engine, config)
    return engine
