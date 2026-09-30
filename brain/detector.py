"""Spot unusual changes. Pure math, no LLM.

For each brand, compare the last RECENT_DAYS with the BASELINE_DAYS before:
- complaint_spike: share of reviews complaining about an issue went up
- low_rating_spike: share of 1-2 star reviews went up

A change only counts when all of these hold:
1. enough reviews in both windows (small numbers swing wildly)
2. a two-proportion z-test says it is unlikely to be chance (z >= MIN_Z)
3. the change is big enough to matter (MIN_LIFT percentage points)
4. there were at least MIN_HITS complaints recently

A spike at a competitor is reported as an opportunity, not a problem.
The same brand/kind/issue is not re-raised within COOLDOWN_DAYS.

Run: python -m brain.detector                   (detect as of today)
     python -m brain.detector --as-of 2026-08-31 (replay a past date)
"""

import argparse
import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core import db
from core.config import Config, load_config
from core.text import now_iso

RECENT_DAYS = 7
BASELINE_DAYS = 28
MIN_RECENT = 10  # reviews of the brand in the recent window
MIN_BASELINE = 20  # reviews of the brand in the baseline window
MIN_HITS = 3  # complaints in the recent window
# About 57 checks run per day (18 issues x 3 brands, plus ratings). At z >= 2, roughly 1 in 40
# fires by pure chance, so false alarms would arrive most days. z >= 3 is about 1 in 750.
MIN_Z = 3.0
MIN_LIFT = 0.05  # at least +5 percentage points
COOLDOWN_DAYS = 7


@dataclass
class Anomaly:
    brand_id: str
    kind: str
    issue: str | None
    window_start: str
    window_end: str
    recent_hits: int
    recent_total: int
    baseline_hits: int
    baseline_total: int
    z_score: float

    @property
    def id(self) -> str:
        raw = f"{self.brand_id}|{self.kind}|{self.issue}|{self.window_end}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    @property
    def recent_share(self) -> float:
        return self.recent_hits / self.recent_total

    @property
    def baseline_share(self) -> float:
        return self.baseline_hits / self.baseline_total if self.baseline_total else 0.0

    @property
    def what(self) -> str:
        return f"{self.issue} complaints" if self.kind == "complaint_spike" else "1-2 star reviews"


def z_score(recent_hits: int, recent_total: int, baseline_hits: int, baseline_total: int) -> float:
    """Two-proportion z-test: how many standard errors the recent share is above the baseline."""
    if not recent_total or not baseline_total:
        return 0.0
    p1 = recent_hits / recent_total
    p0 = baseline_hits / baseline_total
    pooled = (recent_hits + baseline_hits) / (recent_total + baseline_total)
    se = math.sqrt(pooled * (1 - pooled) * (1 / recent_total + 1 / baseline_total))
    return (p1 - p0) / se if se else 0.0


def is_spike(recent_hits: int, recent_total: int, baseline_hits: int, baseline_total: int) -> float | None:
    """Return the z-score if this counts as a spike, else None."""
    if recent_total < MIN_RECENT or baseline_total < MIN_BASELINE or recent_hits < MIN_HITS:
        return None
    lift = recent_hits / recent_total - baseline_hits / baseline_total
    z = z_score(recent_hits, recent_total, baseline_hits, baseline_total)
    return z if lift >= MIN_LIFT and z >= MIN_Z else None


def _windows(as_of: date) -> tuple[str, str, str]:
    """ISO dates: baseline start, recent start, end (exclusive, the day after as_of)."""
    end = as_of + timedelta(days=1)
    recent_start = end - timedelta(days=RECENT_DAYS)
    baseline_start = recent_start - timedelta(days=BASELINE_DAYS)
    return baseline_start.isoformat(), recent_start.isoformat(), end.isoformat()


def detect(engine: Engine, config: Config, as_of: date | None = None) -> list[Anomaly]:
    """Find spikes as of a date. Does not write anything."""
    as_of = as_of or datetime.now(timezone.utc).date()
    baseline_start, recent_start, end = _windows(as_of)
    params = {"b": baseline_start, "r": recent_start, "e": end, "yes": True}
    posted = "COALESCE(m.posted_at, m.collected_at)"
    period = f"CASE WHEN {posted} >= :r THEN 'recent' ELSE 'baseline' END"
    joins = "FROM mentions m JOIN mention_tags t ON t.mention_id = m.id"
    where = f"WHERE t.status = 'ok' AND t.relevant = :yes AND {posted} >= :b AND {posted} < :e"
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                f"SELECT m.brand_id, {period} AS period, COUNT(*), "
                f"SUM(CASE WHEN m.rating IS NOT NULL THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN m.rating <= 2 THEN 1 ELSE 0 END) "
                f"{joins} {where} GROUP BY m.brand_id, {period}"
            ),
            params,
        ).all()
        complaints = conn.execute(
            text(
                f"SELECT m.brand_id, a.issue, {period} AS period, COUNT(*) "
                f"{joins} JOIN aspect_tags a ON a.mention_id = m.id {where} "
                f"AND a.sentiment = 'negative' GROUP BY m.brand_id, a.issue, {period}"
            ),
            params,
        ).all()

    reviews: dict[tuple[str, str], int] = {}
    rated: dict[tuple[str, str], int] = {}
    low: dict[tuple[str, str], int] = {}
    for brand_id, period_name, n, n_rated, n_low in totals:
        reviews[(brand_id, period_name)] = n
        rated[(brand_id, period_name)] = n_rated or 0
        low[(brand_id, period_name)] = n_low or 0
    hits: dict[tuple[str, str, str], int] = {
        (brand_id, issue, period_name): n for brand_id, issue, period_name, n in complaints
    }

    found = []
    window = (recent_start, as_of.isoformat())
    for brand in config.brands:
        r_total, b_total = reviews.get((brand.id, "recent"), 0), reviews.get((brand.id, "baseline"), 0)
        for issue in config.issues:
            r_hits = hits.get((brand.id, issue, "recent"), 0)
            b_hits = hits.get((brand.id, issue, "baseline"), 0)
            if (z := is_spike(r_hits, r_total, b_hits, b_total)) is not None:
                found.append(Anomaly(brand.id, "complaint_spike", issue, *window,
                                     r_hits, r_total, b_hits, b_total, round(z, 2)))

        r_rated, b_rated = rated.get((brand.id, "recent"), 0), rated.get((brand.id, "baseline"), 0)
        r_low, b_low = low.get((brand.id, "recent"), 0), low.get((brand.id, "baseline"), 0)
        if (z := is_spike(r_low, r_rated, b_low, b_rated)) is not None:
            found.append(Anomaly(brand.id, "low_rating_spike", None, *window,
                                 r_low, r_rated, b_low, b_rated, round(z, 2)))
    return sorted(found, key=lambda a: -a.z_score)


def save_new(engine: Engine, anomalies: list[Anomaly]) -> list[Anomaly]:
    """Store anomalies not already raised within the cooldown. Returns the ones stored."""
    stored = []
    with engine.begin() as conn:
        for a in anomalies:
            cooldown_start = (date.fromisoformat(a.window_end) - timedelta(days=COOLDOWN_DAYS)).isoformat()
            recent = conn.execute(
                text(
                    "SELECT 1 FROM anomalies WHERE brand_id = :brand AND kind = :kind "
                    "AND COALESCE(issue, '') = :issue AND window_end > :since AND status <> 'dismissed'"
                ),
                {"brand": a.brand_id, "kind": a.kind, "issue": a.issue or "", "since": cooldown_start},
            ).first()
            if recent:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO anomalies (id, brand_id, kind, issue, window_start, window_end,
                        recent_hits, recent_total, baseline_hits, baseline_total, z_score,
                        status, created_at)
                    VALUES (:id, :brand_id, :kind, :issue, :window_start, :window_end,
                        :recent_hits, :recent_total, :baseline_hits, :baseline_total, :z_score,
                        'new', :created_at)
                    """
                ),
                {**a.__dict__, "id": a.id, "created_at": now_iso()},
            )
            stored.append(a)
    return stored


def evidence(engine: Engine, a: Anomaly, limit: int = 3) -> list[str]:
    """Example quotes from the recent window that back up the anomaly."""
    posted = "COALESCE(m.posted_at, m.collected_at)"
    if a.kind == "complaint_spike":
        sql = (
            "SELECT COALESCE(a.quote, m.text) FROM mentions m "
            "JOIN aspect_tags a ON a.mention_id = m.id "
            f"WHERE m.brand_id = :brand AND a.issue = :issue AND a.sentiment = 'negative' "
            f"AND {posted} >= :start AND {posted} <= :end ORDER BY {posted} DESC LIMIT :limit"
        )
    else:
        sql = (
            "SELECT m.text FROM mentions m "
            f"WHERE m.brand_id = :brand AND m.rating <= 2 "
            f"AND {posted} >= :start AND {posted} <= :end ORDER BY {posted} DESC LIMIT :limit"
        )
    end = a.window_end + "T23:59:59"
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {"brand": a.brand_id, "issue": a.issue, "start": a.window_start, "end": end, "limit": limit},
        )
        return [r[0] for r in rows]


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Detect spikes (prints only, stores nothing)")
    parser.add_argument("--as-of", type=date.fromisoformat, help="YYYY-MM-DD, default today")
    args = parser.parse_args()
    config = load_config()
    found = detect(db.get_engine(), config, args.as_of)
    if not found:
        print("No anomalies.")
    for a in found:
        print(
            f"{a.brand_id:<8} {a.what:<30} {a.baseline_share:.0%} -> {a.recent_share:.0%} "
            f"({a.recent_hits}/{a.recent_total} recent), z={a.z_score}"
        )


if __name__ == "__main__":
    main()
