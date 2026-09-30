"""Measure tagger accuracy against hand-labeled reviews.

Run: python -m evals.eval_tagger
"""

import argparse
import csv
from pathlib import Path

from dotenv import load_dotenv

from brain.tagger import BATCH_SIZE, Tag, tag_texts
from core.config import load_config
from core.llm import LLM

LABELS = Path(__file__).resolve().parent / "labeled_reviews.csv"
TARGET_F1 = 0.8


def load_labels(path: Path = LABELS) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            aspects = {}
            for pair in filter(None, (row.get("aspects") or "").split(";")):
                issue, sentiment = pair.strip().split(":")
                aspects[issue.strip()] = sentiment.strip()
            rows.append(
                {
                    "text": row["text"],
                    "relevant": row["relevant"].strip().lower() == "true",
                    "overall": row["overall"].strip().lower(),
                    "aspects": aspects,
                }
            )
    return rows


def score(labels: list[dict], predictions: list[Tag | None]) -> dict:
    """Issue detection precision/recall/F1, plus sentiment and relevance accuracy."""
    tp = fp = fn = 0
    sentiment_right = sentiment_total = 0
    relevant_right = overall_right = answered = 0
    misses = []
    for label, tag in zip(labels, predictions):
        if tag is None:
            fn += len(label["aspects"])
            misses.append((label["text"], "no answer"))
            continue
        answered += 1
        relevant_right += tag.relevant == label["relevant"]
        overall_right += tag.overall == label["overall"]
        predicted = {a.issue: a.sentiment for a in tag.aspects}
        expected = label["aspects"]
        tp += len(predicted.keys() & expected.keys())
        fp += len(predicted.keys() - expected.keys())
        fn += len(expected.keys() - predicted.keys())
        for issue in predicted.keys() & expected.keys():
            sentiment_total += 1
            sentiment_right += predicted[issue] == expected[issue]
        if predicted != expected:
            misses.append((label["text"], f"expected {expected}, got {predicted}"))

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "reviews": len(labels),
        "answered": answered,
        "issue_precision": precision,
        "issue_recall": recall,
        "issue_f1": f1,
        "aspect_sentiment_accuracy": sentiment_right / sentiment_total if sentiment_total else 0.0,
        "relevance_accuracy": relevant_right / answered if answered else 0.0,
        "overall_accuracy": overall_right / answered if answered else 0.0,
        "misses": misses,
    }


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Score the tagger against labeled reviews")
    parser.add_argument("--path", default=str(LABELS))
    args = parser.parse_args()

    config = load_config()
    labels = load_labels(Path(args.path))
    llm = LLM()
    predictions: list[Tag | None] = []
    models = set()
    for start in range(0, len(labels), BATCH_SIZE):
        batch = labels[start : start + BATCH_SIZE]
        texts = {str(start + i): row["text"] for i, row in enumerate(batch)}
        tags, problems, provider, model = tag_texts(llm, config, texts)
        models.add(f"{provider} ({model})")
        predictions.extend(tags.get(key) for key in texts)
        for problem in problems:
            print(f"  note: {problem}")

    result = score(labels, predictions)
    print(f"\nModel: {', '.join(sorted(models))}")
    for key, value in result.items():
        if key == "misses":
            continue
        print(f"  {key:<27} {value:.2f}" if isinstance(value, float) else f"  {key:<27} {value}")
    print(f"\nDisagreements ({len(result['misses'])}):")
    for review, detail in result["misses"]:
        print(f"  - {review[:70]}\n      {detail}")
    verdict = "PASS" if result["issue_f1"] >= TARGET_F1 else "BELOW TARGET"
    print(f"\nIssue F1 {result['issue_f1']:.2f} vs target {TARGET_F1}: {verdict}")


if __name__ == "__main__":
    main()
