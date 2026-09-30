"""Output 1: Perception Radar. How the market sees each brand, issue by issue.

Pure math over the tags; no LLM here. For each brand and issue:
- mentions: how many reviews talk about it
- net sentiment: (positive - negative) / mentions, from -1 (all bad) to +1 (all good)
- talk share: what share of the brand's reviews bring it up

Then the client is compared with the competitors' average on each issue.
Numbers built on fewer than MIN_SAMPLE reviews are flagged as low data,
because 2 complaints out of 3 reviews says little.

Run: python -m brain.perception --days 90
"""

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core import db
from core.config import Config, load_config

MIN_SAMPLE = 5
GAP_THRESHOLD = 0.2  # net-sentiment difference that counts as a real strength or weakness


@dataclass
class IssueScore:
    issue: str
    mentions: int = 0
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    talk_share: float = 0.0

    @property
    def net(self) -> float:
        return (self.positive - self.negative) / self.mentions if self.mentions else 0.0

    @property
    def low_data(self) -> bool:
        return self.mentions < MIN_SAMPLE


@dataclass
class BrandScore:
    brand_id: str
    name: str
    is_client: bool
    mentions: int = 0
    share_of_voice: float = 0.0
    avg_rating: float | None = None
    rated: int = 0
    overall: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    issues: dict[str, IssueScore] = field(default_factory=dict)

    @property
    def net(self) -> float:
        if not self.mentions:
            return 0.0
        return (self.overall.get("positive", 0) - self.overall.get("negative", 0)) / self.mentions


@dataclass
class Gap:
    issue: str
    client_net: float
    competitor_net: float
    client_mentions: int
    competitor_mentions: int

    @property
    def gap(self) -> float:
        return self.client_net - self.competitor_net

    @property
    def low_data(self) -> bool:
        return self.client_mentions < MIN_SAMPLE or self.competitor_mentions < MIN_SAMPLE


@dataclass
class Radar:
    since: str
    brands: dict[str, BrandScore]
    gaps: list[Gap]

    @property
    def strengths(self) -> list[Gap]:
        return [g for g in self.gaps if g.gap >= GAP_THRESHOLD and not g.low_data]

    @property
    def weaknesses(self) -> list[Gap]:
        return [g for g in self.gaps if g.gap <= -GAP_THRESHOLD and not g.low_data]


def build_radar(engine: Engine, config: Config, days: int = 90, today: datetime | None = None) -> Radar:
    since = ((today or datetime.now(timezone.utc)) - timedelta(days=days)).date().isoformat()
    brands = {
        b.id: BrandScore(b.id, b.name, b.is_client) for b in config.brands
    }
    params = {"since": since}
    # posted_at can be missing (some CSVs have no date), so fall back to when we collected it.
    window = "COALESCE(m.posted_at, m.collected_at) >= :since"
    relevant = "t.status = 'ok' AND t.relevant = :yes"
    params["yes"] = True

    with engine.connect() as conn:
        mention_rows = conn.execute(
            text(
                f"""
                SELECT m.brand_id, m.source, m.rating, t.overall_sentiment
                FROM mentions m JOIN mention_tags t ON t.mention_id = m.id
                WHERE {relevant} AND {window}
                """
            ),
            params,
        ).all()
        aspect_rows = conn.execute(
            text(
                f"""
                SELECT m.brand_id, a.issue, a.sentiment
                FROM mentions m
                JOIN mention_tags t ON t.mention_id = m.id
                JOIN aspect_tags a ON a.mention_id = m.id
                WHERE {relevant} AND {window}
                """
            ),
            params,
        ).all()

    rating_sums: dict[str, float] = {}
    for brand_id, source, rating, overall in mention_rows:
        b = brands.get(brand_id)
        if b is None:  # brand removed from brands.yaml
            continue
        b.mentions += 1
        b.sources[source] = b.sources.get(source, 0) + 1
        if overall:
            b.overall[overall] = b.overall.get(overall, 0) + 1
        if rating is not None:
            b.rated += 1
            rating_sums[brand_id] = rating_sums.get(brand_id, 0.0) + float(rating)

    for brand_id, issue, sentiment in aspect_rows:
        b = brands.get(brand_id)
        if b is None:
            continue
        score = b.issues.setdefault(issue, IssueScore(issue))
        score.mentions += 1
        setattr(score, sentiment, getattr(score, sentiment) + 1)

    total = sum(b.mentions for b in brands.values())
    for b in brands.values():
        b.share_of_voice = b.mentions / total if total else 0.0
        b.avg_rating = rating_sums[b.brand_id] / b.rated if b.rated else None
        for score in b.issues.values():
            score.talk_share = score.mentions / b.mentions if b.mentions else 0.0

    return Radar(since, brands, _gaps(brands, config))


def _gaps(brands: dict[str, BrandScore], config: Config) -> list[Gap]:
    client = brands[config.client.id]
    competitors = [brands[c.id] for c in config.competitors]
    gaps = []
    for issue in config.issues:
        mine = client.issues.get(issue)
        theirs = [c.issues[issue] for c in competitors if issue in c.issues]
        if not mine or not theirs:
            continue
        # Pool competitor reviews so a brand with 50 reviews outweighs one with 3.
        pos = sum(s.positive for s in theirs)
        neg = sum(s.negative for s in theirs)
        n = sum(s.mentions for s in theirs)
        gaps.append(Gap(issue, mine.net, (pos - neg) / n, mine.mentions, n))
    return sorted(gaps, key=lambda g: g.gap)


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _signed(value: float) -> str:
    return f"{value:+.2f}"


def format_radar(radar: Radar, config: Config) -> str:
    lines = [f"# Perception Radar: {config.category}", f"Reviews since {radar.since}.", ""]

    lines += ["## Brands", "", "| Brand | Reviews | Share of voice | Avg rating | Net sentiment | Sources |",
              "|---|---|---|---|---|---|"]
    for b in radar.brands.values():
        name = f"**{b.name}** (client)" if b.is_client else b.name
        rating = f"{b.avg_rating:.2f} ({b.rated})" if b.avg_rating is not None else "-"
        sources = ", ".join(f"{s} {n}" for s, n in sorted(b.sources.items())) or "-"
        lines.append(f"| {name} | {b.mentions} | {_pct(b.share_of_voice)} | {rating} | {_signed(b.net)} | {sources} |")

    lines += ["", "## Issues by brand (net sentiment, reviews)", ""]
    header = "| Issue | " + " | ".join(b.name for b in radar.brands.values()) + " |"
    lines += [header, "|---" * (len(radar.brands) + 1) + "|"]
    for issue in config.issues:
        cells = []
        for b in radar.brands.values():
            s = b.issues.get(issue)
            cells.append("-" if not s else f"{_signed(s.net)} ({s.mentions}){'*' if s.low_data else ''}")
        if any(c != "-" for c in cells):
            lines.append(f"| {issue} | " + " | ".join(cells) + " |")
    lines.append(f"\n\\* fewer than {MIN_SAMPLE} reviews: treat as a hint, not a finding.")

    client = config.client.name
    for title, items in (("Strengths", radar.strengths), ("Weaknesses", radar.weaknesses)):
        lines += ["", f"## {client} {title.lower()} vs competitors", ""]
        if not items:
            lines.append(f"None with enough data (needs {MIN_SAMPLE}+ reviews on both sides).")
        ordered = items if title == "Weaknesses" else list(reversed(items))
        for g in ordered:
            lines.append(
                f"- **{g.issue}**: {client} {_signed(g.client_net)} vs competitors "
                f"{_signed(g.competitor_net)} (gap {_signed(g.gap)}; "
                f"{g.client_mentions} vs {g.competitor_mentions} reviews)"
            )

    thin = [g.issue for g in radar.gaps if g.low_data and abs(g.gap) >= GAP_THRESHOLD]
    if thin:
        lines += ["", f"Possible gaps that need more data: {', '.join(thin)}."]
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Print the Perception Radar")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    config = load_config()
    print(format_radar(build_radar(db.get_engine(), config, args.days), config))


if __name__ == "__main__":
    main()
