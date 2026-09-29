import pytest
import yaml

from core.config import ConfigError, load_config


def test_default_config_is_valid(config):
    assert config.client.id == "boat"
    assert {b.id for b in config.competitors} == {"noise", "jbl"}
    assert "other" in config.issues


def test_resolve_brand_by_name_alias_and_id(config):
    assert config.resolve_brand("boAt").id == "boat"
    assert config.resolve_brand("AIRDOPES").id == "boat"
    assert config.resolve_brand("jbl").id == "jbl"
    assert config.resolve_brand("Sony") is None


def _write(tmp_path, data):
    path = tmp_path / "brands.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


BASE = {
    "category": "earbuds",
    "issues": ["battery_life", "other"],
    "brands": [
        {"id": "a", "name": "A", "is_client": True},
        {"id": "b", "name": "B"},
    ],
}


def test_requires_exactly_one_client(tmp_path):
    data = {**BASE, "brands": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]}
    with pytest.raises(ConfigError, match="is_client"):
        load_config(_write(tmp_path, data))


def test_rejects_shared_alias(tmp_path):
    data = {
        **BASE,
        "brands": [
            {"id": "a", "name": "A", "is_client": True, "aliases": ["buds"]},
            {"id": "b", "name": "B", "aliases": ["buds"]},
        ],
    }
    with pytest.raises(ConfigError, match="buds"):
        load_config(_write(tmp_path, data))
