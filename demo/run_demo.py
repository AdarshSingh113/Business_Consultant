"""See the whole agent work end to end on made-up data, with a real LLM.

Builds data/demo.db (separate from your real database) with invented reviews
containing a hidden problem: in the last week, Airdopes 141 battery complaints
jump. Then runs the real pipeline: tag -> detect -> diagnose.

    python -m demo.run_demo            needs GEMINI_API_KEY (about 15-20 LLM calls)
    python -m demo.run_demo --answer "Yes, new battery cell supplier from 20 Aug"
                                        answer the agent's question and let it finish

All reviews here are synthetic, generated from templates below.
"""

import argparse
import random
from datetime import date, timedelta

from dotenv import load_dotenv

from brain import consultant, detector, diagnosis, tagger
from core import db
from core.config import ROOT, load_config
from core.llm import LLM
from core.text import mention_id

DEMO_DB = f"sqlite:///{ROOT / 'data' / 'demo.db'}"
AS_OF = date(2026, 8, 31)

GOOD = [
    "Sound is crisp and the bass is punchy for the price.",
    "Comfortable fit, I wear them for hours on calls.",
    "Pairs instantly with my phone, no drops so far.",
    "Great value for money, sound is better than expected.",
    "Mic is clear, colleagues hear me fine on meetings.",
    "Battery easily lasts a full day of commuting.",
    "Bass accha hai, paisa vasool product.",
    "Build feels solid and the case is compact.",
]
MEH = [
    "Sound is okay but the fit is a bit loose while running.",
    "Decent earbuds, delivery took longer than promised.",
    "App is clunky but the sound is fine.",
]
BATTERY_BAD = [
    "Battery dies in under 2 hours now, it used to last 5.",
    "Left bud battery drains even when kept in the case.",
    "Battery 1.5 ghante mein khatam, bahut bekar.",
    "New unit, battery drops from 100 to 20 in an hour.",
    "Battery life is terrible, have to charge twice a day.",
    "Case drains the buds overnight, battery is always low.",
    "Battery backup is nowhere near what was advertised.",
]


def seed(engine, config, rng: random.Random) -> int:
    mentions = []

    def add(brand, product, body, rating, day):
        text = f"{body} ({product})"
        mentions.append(db.Mention(mention_id("amazon", brand, f"{text} {day} {len(mentions)}"),
                                   brand, "amazon", text, product=product,
                                   rating=rating, posted_at=day.isoformat()))

    def days(start, end):
        return start + timedelta(days=rng.randrange((end - start).days))

    baseline = (AS_OF - timedelta(days=34), AS_OF - timedelta(days=7))
    recent = (AS_OF - timedelta(days=6), AS_OF + timedelta(days=1))
    for brand, products in (("boat", ["Airdopes 141", "Airdopes 311 Pro"]),
                            ("noise", ["Buds VS104"]), ("jbl", ["Wave Beam"])):
        for window, n in ((baseline, 36), (recent, 12)):
            for _ in range(n):
                roll = rng.random()
                body, rating = ((rng.choice(GOOD), rng.choice([4, 5])) if roll < 0.75
                                else (rng.choice(MEH), 3) if roll < 0.95
                                else (rng.choice(BATTERY_BAD), 2))
                add(brand, rng.choice(products), body, rating, days(*window))
    # The hidden problem: a burst of battery complaints on one boAt product.
    for body in BATTERY_BAD:
        add("boat", "Airdopes 141", body, rng.choice([1, 2]), days(*recent))
    return db.insert_mentions(engine, mentions)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="End-to-end demo on synthetic data")
    parser.add_argument("--answer", help="answer the agent's open question and resume")
    args = parser.parse_args()

    config = load_config()
    engine = db.get_engine(DEMO_DB)
    db.init_db(engine)
    db.sync_brands(engine, config)
    llm = LLM(engine)

    if args.answer:
        for request in consultant.open_requests(engine):
            consultant.answer(engine, request["id"], args.answer)
    else:
        print(f"Seeded {seed(engine, config, random.Random(7))} synthetic reviews into data/demo.db")
        print("Tagging with", ", ".join(f"{p.name} ({p.model})" for p in llm.providers), "...")
        print(" ", tagger.tag_pending(engine, llm, config, limit=1000))
        found = detector.save_new(engine, detector.detect(engine, config, AS_OF))
        print(f"Detector found {len(found)} new anomalies:")
        for a in found:
            print(f"  {a.brand_id} {a.what}: {a.baseline_share:.0%} -> {a.recent_share:.0%} (z={a.z_score})")

    outcome = consultant.investigate(engine, llm, config)
    for d in outcome["_diagnoses"]:
        print("\n" + "=" * 70)
        print(diagnosis.format_diagnosis(engine, config, d))
        print(f"\n[{d.steps} agent steps; full transcript in the diagnoses table]")
    if not outcome["_diagnoses"]:
        print("Nothing to investigate. Delete data/demo.db to start over.")


if __name__ == "__main__":
    main()
