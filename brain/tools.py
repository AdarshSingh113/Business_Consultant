"""Tools the diagnosis agent can call. Plain Python and SQL; the LLM only picks which to use.

Reviews are shown to the agent as short refs (m1, m2, ...) instead of database ids.
The agent must cite these refs as evidence, and only refs it was actually shown count.
"""

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain.detector import BASELINE_DAYS, Anomaly
from core.config import Config

MAX_REVIEWS = 15
POSTED = "COALESCE(m.posted_at, m.collected_at)"

TOOL_DOCS = """\
query_reviews(brand?, issue?, sentiment?, period?, product?, max_rating?, limit?)
    Read reviews. brand defaults to the anomaly's brand. period: recent | baseline | all
    (default recent). sentiment: positive | negative | neutral (needs issue).
    limit up to 15. Returns refs you can cite as evidence.
breakdown(by, issue?)
    Split the anomaly brand's reviews by "product" or "source", recent vs baseline.
    With issue, also counts negative mentions of it. Without, counts 1-2 star reviews.
issue_breakdown()
    Negative mentions per issue for the anomaly brand, recent vs baseline.
    Shows whether other problems moved at the same time.
compare_competitors(issue)
    Share of each brand's reviews complaining about the issue, recent vs baseline.
    Tells you if the whole market moved or only this brand.
request_user_data(question, why)
    Ask the brand team for data reviews can't show (return reasons by SKU, supplier or
    firmware changes, stock-outs, price changes). Pauses the investigation until answered.
    Use only when the answer would change your conclusion. At most 2 per investigation."""


class Tools:
    def __init__(self, engine: Engine, config: Config, anomaly: Anomaly, refs: dict[str, str]):
        self.engine = engine
        self.config = config
        self.anomaly = anomaly
        self.refs = refs  # ref -> mention id, shared with the agent's saved state
        start = date.fromisoformat(anomaly.window_start)
        end = date.fromisoformat(anomaly.window_end) + timedelta(days=1)
        self.periods = {
            "recent": (start.isoformat(), end.isoformat()),
            "baseline": ((start - timedelta(days=BASELINE_DAYS)).isoformat(), start.isoformat()),
        }
        self.periods["all"] = (self.periods["baseline"][0], self.periods["recent"][1])

    def run(self, name: str, args: dict) -> dict:
        handler = {
            "query_reviews": self.query_reviews,
            "breakdown": self.breakdown,
            "issue_breakdown": self.issue_breakdown,
            "compare_competitors": self.compare_competitors,
        }.get(name)
        if handler is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return handler(**(args or {}))
        except (TypeError, ValueError) as exc:  # bad arguments: tell the agent so it can retry
            return {"error": f"{name}: {exc}"}

    def _ref(self, mention_id: str) -> str:
        for ref, known in self.refs.items():
            if known == mention_id:
                return ref
        ref = f"m{len(self.refs) + 1}"
        self.refs[ref] = mention_id
        return ref

    def _brand(self, brand: str | None) -> str:
        if not brand:
            return self.anomaly.brand_id
        found = self.config.resolve_brand(brand)
        if found is None:
            raise ValueError(f"unknown brand {brand!r}; known: {[b.id for b in self.config.brands]}")
        return found.id

    def _issue(self, issue: str | None) -> str | None:
        if issue and issue not in self.config.issues:
            raise ValueError(f"unknown issue {issue!r}; use one of {self.config.issues}")
        return issue

    def _period(self, period: str) -> tuple[str, str]:
        if period not in self.periods:
            raise ValueError("period must be recent, baseline or all")
        return self.periods[period]

    def query_reviews(self, brand=None, issue=None, sentiment=None, period="recent",
                      product=None, max_rating=None, limit=10) -> dict:
        brand_id, issue = self._brand(brand), self._issue(issue)
        start, end = self._period(period)
        params = {"brand": brand_id, "start": start, "end": end, "yes": True,
                  "limit": max(1, min(int(limit), MAX_REVIEWS))}
        sql = (
            f"SELECT m.id, m.product, m.source, m.rating, {POSTED}, m.text "
            "FROM mentions m JOIN mention_tags t ON t.mention_id = m.id "
        )
        where = [f"m.brand_id = :brand", "t.status = 'ok'", "t.relevant = :yes",
                 f"{POSTED} >= :start", f"{POSTED} < :end"]
        if issue:
            sql += "JOIN aspect_tags a ON a.mention_id = m.id "
            where.append("a.issue = :issue")
            params["issue"] = issue
            if sentiment:
                if sentiment not in ("positive", "negative", "neutral"):
                    raise ValueError("sentiment must be positive, negative or neutral")
                where.append("a.sentiment = :sentiment")
                params["sentiment"] = sentiment
        if product:
            where.append("LOWER(m.product) = :product")
            params["product"] = str(product).lower()
        if max_rating is not None:
            where.append("m.rating <= :max_rating")
            params["max_rating"] = float(max_rating)
        sql += "WHERE " + " AND ".join(where) + f" ORDER BY {POSTED} DESC, m.id LIMIT :limit"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).all()
        return {
            "brand": brand_id,
            "period": period,
            "count": len(rows),
            "reviews": [
                {"ref": self._ref(r[0]), "product": r[1], "source": r[2], "rating": r[3],
                 "date": str(r[4])[:10], "text": r[5][:300]}
                for r in rows
            ],
        }

    def breakdown(self, by, issue=None) -> dict:
        if by not in ("product", "source"):
            raise ValueError('by must be "product" or "source"')
        issue = self._issue(issue)
        column = "COALESCE(m.product, 'unknown')" if by == "product" else "m.source"
        if issue:
            hit = ("CASE WHEN EXISTS (SELECT 1 FROM aspect_tags a WHERE a.mention_id = m.id "
                   "AND a.issue = :issue AND a.sentiment = 'negative') THEN 1 ELSE 0 END")
        else:
            hit = "CASE WHEN m.rating <= 2 THEN 1 ELSE 0 END"
        rows = self._by_period(f"{column}", hit, {"issue": issue})
        label = f"negative {issue}" if issue else "1-2 star"
        return {"by": by, "counting": label, "rows": rows}

    def issue_breakdown(self) -> dict:
        start, end = self.periods["all"]
        recent_start = self.periods["recent"][0]
        sql = (
            f"SELECT a.issue, CASE WHEN {POSTED} >= :recent THEN 'recent' ELSE 'baseline' END, COUNT(*) "
            "FROM mentions m JOIN mention_tags t ON t.mention_id = m.id "
            "JOIN aspect_tags a ON a.mention_id = m.id "
            f"WHERE m.brand_id = :brand AND t.status = 'ok' AND t.relevant = :yes "
            f"AND a.sentiment = 'negative' AND {POSTED} >= :start AND {POSTED} < :end "
            "GROUP BY 1, 2"
        )
        params = {"brand": self.anomaly.brand_id, "recent": recent_start, "start": start,
                  "end": end, "yes": True}
        with self.engine.connect() as conn:
            counts = conn.execute(text(sql), params).all()
        totals = self._totals(self.anomaly.brand_id)
        issues: dict[str, dict] = {}
        for issue, period, n in counts:
            issues.setdefault(issue, {"recent": 0, "baseline": 0})[period] = n
        return {
            "reviews": totals,
            "negative_mentions": dict(sorted(issues.items(), key=lambda kv: -kv[1]["recent"])),
        }

    def compare_competitors(self, issue) -> dict:
        issue = self._issue(issue)
        if not issue:
            raise ValueError("issue is required")
        result = {}
        for brand in self.config.brands:
            totals = self._totals(brand.id)
            hits = self._totals(brand.id, issue)
            result[brand.id] = {
                period: {
                    "reviews": totals[period],
                    "complaints": hits[period],
                    "share": round(hits[period] / totals[period], 2) if totals[period] else None,
                }
                for period in ("recent", "baseline")
            }
        return {"issue": issue, "brands": result}

    def _totals(self, brand_id: str, issue: str | None = None) -> dict[str, int]:
        start, end = self.periods["all"]
        sql = (
            f"SELECT CASE WHEN {POSTED} >= :recent THEN 'recent' ELSE 'baseline' END, COUNT(*) "
            "FROM mentions m JOIN mention_tags t ON t.mention_id = m.id "
        )
        params = {"brand": brand_id, "recent": self.periods["recent"][0], "start": start,
                  "end": end, "yes": True}
        if issue:
            sql += "JOIN aspect_tags a ON a.mention_id = m.id AND a.issue = :issue AND a.sentiment = 'negative' "
            params["issue"] = issue
        sql += (f"WHERE m.brand_id = :brand AND t.status = 'ok' AND t.relevant = :yes "
                f"AND {POSTED} >= :start AND {POSTED} < :end GROUP BY 1")
        with self.engine.connect() as conn:
            found = dict(conn.execute(text(sql), params).all())
        return {"recent": found.get("recent", 0), "baseline": found.get("baseline", 0)}

    def _by_period(self, column: str, hit: str, extra: dict) -> list[dict]:
        start, end = self.periods["all"]
        sql = (
            f"SELECT {column}, CASE WHEN {POSTED} >= :recent THEN 'recent' ELSE 'baseline' END, "
            f"COUNT(*), SUM({hit}) "
            "FROM mentions m JOIN mention_tags t ON t.mention_id = m.id "
            f"WHERE m.brand_id = :brand AND t.status = 'ok' AND t.relevant = :yes "
            f"AND {POSTED} >= :start AND {POSTED} < :end GROUP BY 1, 2"
        )
        params = {"brand": self.anomaly.brand_id, "recent": self.periods["recent"][0],
                  "start": start, "end": end, "yes": True, **extra}
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).all()
        grouped: dict[str, dict] = {}
        for key, period, n, hits in rows:
            entry = grouped.setdefault(key, {"name": key, "recent_reviews": 0, "recent_hits": 0,
                                             "baseline_reviews": 0, "baseline_hits": 0})
            entry[f"{period}_reviews"] = n
            entry[f"{period}_hits"] = int(hits or 0)
        return sorted(grouped.values(), key=lambda e: -e["recent_hits"])
