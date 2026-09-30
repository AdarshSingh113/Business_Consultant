"""Output 3: Competitor Teardown. What to copy, where to fight, which gaps to launch into.

Two steps, same split as everywhere else:
1. Math (no LLM) picks the candidates from the Perception Radar:
   - copy:  a competitor is clearly better than the client on an issue, and good at it
   - fight: the client is clearly better than the competitors: defend it, advertise it
   - gaps:  every brand is weak on an issue: an unmet need to launch into
   Only issues with enough reviews on both sides count.
2. The LLM turns each candidate into an insight and an action, citing review quotes.
   Its answer is checked: items must match the candidates, and quotes must be real refs.

Run: python -m brain.teardown --days 90
"""

import argparse
import json
import uuid

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Engine

from brain.perception import GAP_THRESHOLD, Radar, build_radar
from core import db
from core.config import Config, load_config
from core.llm import LLM, LLMError
from core.text import now_iso

MAX_PER_SECTION = 5
QUOTES_PER_ITEM = 3


def candidates(radar: Radar, config: Config) -> dict[str, list[dict]]:
    """Pick copy / fight / gap items from the radar. Pure math."""
    client = radar.brands[config.client.id]
    copy, gaps = [], []
    for comp in (radar.brands[c.id] for c in config.competitors):
        for issue, theirs in comp.issues.items():
            ours = client.issues.get(issue)
            if not ours or ours.low_data or theirs.low_data:
                continue
            # Only copy from a competitor that is actually good at it, not merely less bad.
            if theirs.net > 0 and theirs.net - ours.net >= GAP_THRESHOLD:
                copy.append({"competitor": comp.brand_id, "issue": issue,
                             "their_net": round(theirs.net, 2), "our_net": round(ours.net, 2),
                             "their_reviews": theirs.mentions, "our_reviews": ours.mentions})
    fight = [{"issue": g.issue, "our_net": round(g.client_net, 2),
              "competitor_net": round(g.competitor_net, 2), "our_reviews": g.client_mentions,
              "competitor_reviews": g.competitor_mentions} for g in radar.strengths]

    for issue in config.issues:
        if issue == "other":
            continue
        scores = [b.issues[issue] for b in radar.brands.values() if issue in b.issues]
        with_data = [s for s in scores if not s.low_data]
        if len(with_data) < 2 or any(s.net >= 0 for s in with_data):
            continue
        n = sum(s.mentions for s in scores)
        net = (sum(s.positive for s in scores) - sum(s.negative for s in scores)) / n
        if net <= -GAP_THRESHOLD:
            gaps.append({"issue": issue, "market_net": round(net, 2), "reviews": n,
                         "brands": {b.brand_id: round(b.issues[issue].net, 2)
                                    for b in radar.brands.values() if issue in b.issues}})

    copy.sort(key=lambda c: c["our_net"] - c["their_net"])
    fight.sort(key=lambda f: f["competitor_net"] - f["our_net"])
    gaps.sort(key=lambda g: g["market_net"])
    return {"copy": copy[:MAX_PER_SECTION], "fight": fight[:MAX_PER_SECTION], "gaps": gaps[:MAX_PER_SECTION]}


def _quotes(engine: Engine, since: str, brands: list[str], issue: str, sentiment: str) -> list[tuple[str, str]]:
    params = {f"b{i}": b for i, b in enumerate(brands)}
    params.update({"issue": issue, "sentiment": sentiment, "since": since, "yes": True,
                   "limit": QUOTES_PER_ITEM})
    sql = (
        "SELECT m.id, m.text FROM mentions m "
        "JOIN mention_tags t ON t.mention_id = m.id JOIN aspect_tags a ON a.mention_id = m.id "
        f"WHERE m.brand_id IN ({', '.join(':' + k for k in params if k.startswith('b'))}) "
        "AND t.status = 'ok' AND t.relevant = :yes AND a.issue = :issue AND a.sentiment = :sentiment "
        "AND COALESCE(m.posted_at, m.collected_at) >= :since "
        "ORDER BY COALESCE(m.posted_at, m.collected_at) DESC, m.id LIMIT :limit"
    )
    with engine.connect() as conn:
        return [(r[0], r[1]) for r in conn.execute(text(sql), params)]


def attach_quotes(engine: Engine, since: str, cands: dict, config: Config) -> dict[str, str]:
    """Add quote refs (q1, q2...) to each candidate. Returns ref -> mention id."""
    refs: dict[str, str] = {}

    def add(item, brands, sentiment):
        item["quotes"] = []
        for mention_id, body in _quotes(engine, since, brands, item["issue"], sentiment):
            ref = f"q{len(refs) + 1}"
            refs[ref] = mention_id
            item["quotes"].append({"ref": ref, "text": body[:240]})

    for item in cands["copy"]:
        add(item, [item["competitor"]], "positive")  # what they do well
    for item in cands["fight"]:
        add(item, [config.client.id], "positive")  # what our customers love
    for item in cands["gaps"]:
        add(item, [b.id for b in config.brands], "negative")  # what nobody gets right
    return refs


SYSTEM = """You are a competitive strategist for {client}, a {category} brand.
You get candidates found by statistics on customer reviews, each with real quotes (refs q1, q2...).
For every candidate write a short insight and one concrete action.
- copy: what the competitor does better and how {client} could match it.
- fight: a {client} strength to defend and use in marketing.
- gaps: a need no brand meets well, and a product or feature idea to launch into it.
Cite quote refs as evidence. Only use refs you were given. Keep each field under 40 words.
Reply with JSON only:
{{"headline": "one sentence on the competitive picture",
  "copy": [{{"competitor": "...", "issue": "...", "insight": "...", "action": "...", "evidence": ["q1"]}}],
  "fight": [{{"issue": "...", "insight": "...", "action": "...", "evidence": ["q4"]}}],
  "gaps": [{{"issue": "...", "insight": "...", "launch_idea": "...", "evidence": ["q7"]}}]}}"""


def validate(raw: dict, cands: dict, refs: dict[str, str]) -> tuple[dict, list[str]]:
    """Keep only items that match a candidate, with real refs."""
    problems = []
    out = {"headline": str(raw.get("headline", "")), "copy": [], "fight": [], "gaps": []}
    for section in ("copy", "fight", "gaps"):
        allowed = {(c.get("competitor"), c["issue"]) for c in cands[section]}
        seen = set()
        for item in raw.get(section) or []:
            if not isinstance(item, dict):
                continue
            key = (item.get("competitor") if section == "copy" else None, item.get("issue"))
            if key not in allowed or key in seen:
                problems.append(f"{section}: {key} was not a candidate, dropped")
                continue
            seen.add(key)
            cited = [str(r) for r in item.get("evidence") or []]
            valid = [r for r in cited if r in refs]
            if len(valid) < len(cited):
                problems.append(f"{section} {key[1]}: invented refs removed")
            fields = ("insight", "action") if section != "gaps" else ("insight", "launch_idea")
            entry = {k: str(item.get(k, "")) for k in fields}
            entry.update({"issue": key[1], "evidence": valid})
            if section == "copy":
                entry["competitor"] = key[0]
            stats = next(c for c in cands[section] if (c.get("competitor"), c["issue"]) == key)
            entry["stats"] = {k: v for k, v in stats.items() if k not in ("quotes", "issue", "competitor")}
            out[section].append(entry)
    return out, problems


def build_teardown(engine: Engine, config: Config, llm: LLM | None, days: int = 90) -> dict:
    radar = build_radar(engine, config, days)
    cands = candidates(radar, config)
    refs = attach_quotes(engine, radar.since, cands, config)
    teardown = {"since": radar.since, "candidates": cands, "refs": refs, "llm": None, "problems": []}
    if llm is None or not any(cands.values()):
        return teardown
    prompt = "Candidates:\n" + json.dumps(cands, ensure_ascii=False)
    try:
        result = llm.complete_json(prompt, SYSTEM.format(client=config.client.name, category=config.category))
    except LLMError as exc:
        teardown["problems"].append(f"LLM unavailable, math-only teardown: {str(exc)[:200]}")
        return teardown
    teardown["llm"], teardown["problems"] = validate(result.data, cands, refs)
    return teardown


def save(engine: Engine, teardown: dict) -> str:
    report_id = f"teardown-{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO reports (id, kind, since, content, created_at) "
                 "VALUES (:id, 'teardown', :since, :content, :now)"),
            {"id": report_id, "since": teardown["since"], "content": json.dumps(teardown), "now": now_iso()},
        )
    return report_id


def format_teardown(engine: Engine, config: Config, teardown: dict) -> str:
    names = {b.id: b.name for b in config.brands}
    client = config.client.name
    texts = {}
    if teardown["refs"]:
        params = {f"i{n}": mid for n, mid in enumerate(teardown["refs"].values())}
        with engine.connect() as conn:
            texts = dict(conn.execute(
                text(f"SELECT id, text FROM mentions WHERE id IN ({', '.join(':' + k for k in params)})"), params
            ).all())

    def quotes(refs):
        return [f'  > "{texts.get(teardown["refs"][r], "?")[:140]}"' for r in refs[:2]]

    lines = [f"# Competitor Teardown: {client}", f"Reviews since {teardown['since']}.", ""]
    llm = teardown["llm"]
    cands = teardown["candidates"]
    if llm and llm["headline"]:
        lines += [llm["headline"], ""]

    lines.append("## Copy: where a competitor is better")
    rows = llm["copy"] if llm else cands["copy"]
    if not rows:
        lines.append("Nothing with enough data.")
    for item in rows:
        s = item.get("stats", item)
        lines.append(f"- **{names[item['competitor']]} on {item['issue']}**: them {s['their_net']:+.2f} "
                     f"vs {client} {s['our_net']:+.2f}")
        if llm:
            lines += [f"  {item['insight']}", f"  Action: {item['action']}"] + quotes(item["evidence"])

    lines += ["", f"## Fight: {client} strengths to defend"]
    rows = llm["fight"] if llm else cands["fight"]
    if not rows:
        lines.append("Nothing with enough data.")
    for item in rows:
        s = item.get("stats", item)
        lines.append(f"- **{item['issue']}**: {client} {s['our_net']:+.2f} vs competitors {s['competitor_net']:+.2f}")
        if llm:
            lines += [f"  {item['insight']}", f"  Action: {item['action']}"] + quotes(item["evidence"])

    lines += ["", "## Gaps: needs nobody meets"]
    rows = llm["gaps"] if llm else cands["gaps"]
    if not rows:
        lines.append("Nothing with enough data.")
    for item in rows:
        s = item.get("stats", item)
        lines.append(f"- **{item['issue']}**: market {s['market_net']:+.2f} across {s['reviews']} reviews")
        if llm:
            lines += [f"  {item['insight']}", f"  Launch idea: {item['launch_idea']}"] + quotes(item["evidence"])
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Competitor teardown")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--no-llm", action="store_true", help="math only")
    args = parser.parse_args()
    config = load_config()
    engine = db.get_engine()
    db.init_db(engine)
    llm = None if args.no_llm else LLM(engine)
    teardown = build_teardown(engine, config, llm, args.days)
    save(engine, teardown)
    print(format_teardown(engine, config, teardown))


if __name__ == "__main__":
    main()
