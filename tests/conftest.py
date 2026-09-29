import pytest

from core import db
from core.config import load_config


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def engine(tmp_path, config):
    engine = db.get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db.init_db(engine)
    db.sync_brands(engine, config)
    return engine
