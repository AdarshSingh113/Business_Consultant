"""Loads and validates config/brands.yaml."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "brands.yaml"

_SLUG = re.compile(r"^[a-z0-9_]+$")


class ConfigError(ValueError):
    pass


@dataclass
class Brand:
    id: str
    name: str
    is_client: bool
    aliases: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)

    @property
    def search_names(self) -> list[str]:
        """Name plus aliases, lowercased and de-duplicated, for text matching."""
        names = [self.name.lower()] + [a.lower() for a in self.aliases]
        return list(dict.fromkeys(names))


@dataclass
class Config:
    category: str
    brands: list[Brand]
    context_keywords: list[str]
    issues: list[str]
    sources: dict

    @property
    def client(self) -> Brand:
        return next(b for b in self.brands if b.is_client)

    @property
    def competitors(self) -> list[Brand]:
        return [b for b in self.brands if not b.is_client]

    def brand(self, brand_id: str) -> Brand:
        for b in self.brands:
            if b.id == brand_id:
                return b
        raise KeyError(brand_id)

    def resolve_brand(self, value: str | None) -> Brand | None:
        """Match a brand by id, name or alias, ignoring case. None if unknown."""
        if not value:
            return None
        wanted = value.strip().lower()
        for b in self.brands:
            if wanted == b.id or wanted in b.search_names:
                return b
        return None


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    brands = [
        Brand(
            id=str(b.get("id", "")),
            name=str(b.get("name", "")),
            is_client=bool(b.get("is_client", False)),
            aliases=[str(a) for a in b.get("aliases") or []],
            products=[str(p) for p in b.get("products") or []],
        )
        for b in raw.get("brands") or []
    ]
    config = Config(
        category=str(raw.get("category", "")),
        brands=brands,
        context_keywords=[str(k).lower() for k in raw.get("context_keywords") or []],
        issues=[str(i) for i in raw.get("issues") or []],
        sources=raw.get("sources") or {},
    )
    _validate(config)
    return config


def _validate(config: Config) -> None:
    if not config.category:
        raise ConfigError("'category' is required")
    if not config.brands:
        raise ConfigError("at least one brand is required")

    ids = [b.id for b in config.brands]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"brand ids must be unique: {ids}")
    for b in config.brands:
        if not _SLUG.match(b.id):
            raise ConfigError(f"brand id {b.id!r} must be lowercase letters, digits or _")
        if not b.name:
            raise ConfigError(f"brand {b.id!r} needs a name")

    clients = [b.id for b in config.brands if b.is_client]
    if len(clients) != 1:
        raise ConfigError(f"exactly one brand must have is_client: true, found {clients}")

    # An alias shared by two brands would make CSV and Reddit attribution ambiguous.
    seen: dict[str, str] = {}
    for b in config.brands:
        for name in b.search_names + [b.id]:
            if name in seen and seen[name] != b.id:
                raise ConfigError(f"alias {name!r} is used by both {seen[name]} and {b.id}")
            seen[name] = b.id

    if "other" not in config.issues:
        raise ConfigError("'issues' must include 'other' as a catch-all label")
