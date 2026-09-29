"""Import marketplace reviews (Amazon, Flipkart) from CSV files.

Column names are matched loosely, so exports from different tools work.
Required: review text, plus brand and source either as columns or passed
with --brand / --source.

Examples:
    python -m collectors.csv_import data/sample/sample_reviews.csv
    python -m collectors.csv_import my_export.csv --brand boat --source amazon
    python -m collectors.csv_import my_export.csv --brand boat --source amazon --dry-run
"""

import argparse
import csv
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import Engine

from core import db
from core.config import ROOT, Config, load_config
from core.text import clean_text, mention_id, parse_date, parse_rating

# Our field name -> column names we accept (compared lowercased, spaces/dashes as _).
COLUMN_ALIASES = {
    "text": ["text", "review", "review_text", "body", "review_body", "content", "comment"],
    "brand": ["brand", "brand_name", "company"],
    "product": ["product", "product_name", "item", "model"],
    "source": ["source", "platform", "site", "marketplace"],
    "rating": ["rating", "stars", "star_rating", "score", "review_rating"],
    "title": ["title", "review_title", "headline", "summary"],
    "posted_at": ["date", "posted_at", "review_date", "reviewed_on", "created_at"],
    "source_ref": ["url", "link", "review_url", "source_ref", "review_id"],
}

MIN_TEXT_LENGTH = 3


@dataclass
class ParseResult:
    mentions: list[db.Mention] = field(default_factory=list)
    rows_read: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _normalize_header(name: str) -> str:
    return clean_text(name).lower().replace(" ", "_").replace("-", "_")


def map_columns(headers: list[str]) -> dict[str, str]:
    """Map our field names to the CSV's actual header names."""
    normalized = {_normalize_header(h): h for h in headers if h}
    mapping = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field_name] = normalized[alias]
                break
    return mapping


def parse_csv(
    path: str | Path,
    config: Config,
    default_brand: str | None = None,
    default_source: str | None = None,
) -> ParseResult:
    result = ParseResult()
    # utf-8-sig strips the byte-order mark Excel adds to CSV exports.
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        columns = map_columns(reader.fieldnames or [])
        if "text" not in columns:
            raise ValueError(
                f"{path}: no review text column found. Expected one of {COLUMN_ALIASES['text']}, "
                f"got {reader.fieldnames}"
            )
        if "brand" not in columns and not default_brand:
            raise ValueError(f"{path}: no brand column; pass --brand")
        if "source" not in columns and not default_source:
            raise ValueError(f"{path}: no source column; pass --source (amazon, flipkart)")

        def get(row: dict, field_name: str) -> str:
            column = columns.get(field_name)
            return clean_text(row.get(column)) if column else ""

        for row in reader:
            result.rows_read += 1

            review_text = get(row, "text")
            if len(review_text) < MIN_TEXT_LENGTH:
                result.skip("empty or too short text")
                continue

            brand = config.resolve_brand(get(row, "brand") or default_brand)
            if brand is None:
                result.skip(f"unknown brand {get(row, 'brand') or default_brand!r}")
                continue

            source = (get(row, "source") or default_source or "").lower()
            if not source:
                result.skip("missing source")
                continue

            result.mentions.append(
                db.Mention(
                    id=mention_id(source, brand.id, review_text),
                    brand_id=brand.id,
                    source=source,
                    text=review_text,
                    product=get(row, "product") or None,
                    source_ref=get(row, "source_ref") or None,
                    title=get(row, "title") or None,
                    rating=parse_rating(get(row, "rating")),
                    posted_at=parse_date(get(row, "posted_at")),
                )
            )
    return result


def import_folder(engine: Engine, config: Config, run_id: str | None = None) -> dict:
    """Import every CSV in the configured folder. Re-importing is safe: duplicates are skipped."""
    folder = ROOT / config.sources.get("csv", {}).get("folder", "data/imports")
    stats = {"files": 0, "rows": 0, "new": 0, "skipped": {}}
    for path in sorted(folder.glob("*.csv")):
        parsed = parse_csv(path, config)
        stats["files"] += 1
        stats["rows"] += parsed.rows_read
        stats["new"] += db.insert_mentions(engine, parsed.mentions, run_id)
        for reason, n in parsed.skipped.items():
            stats["skipped"][reason] = stats["skipped"].get(reason, 0) + n
    return stats


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Import reviews from a CSV file")
    parser.add_argument("path")
    parser.add_argument("--brand", help="brand for every row, if the CSV has no brand column")
    parser.add_argument("--source", help="amazon, flipkart, ... if the CSV has no source column")
    parser.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = parser.parse_args()

    config = load_config()
    parsed = parse_csv(args.path, config, args.brand, args.source)
    print(f"Read {parsed.rows_read} rows, {len(parsed.mentions)} valid.")
    for reason, n in parsed.skipped.items():
        print(f"  skipped {n}: {reason}")

    if args.dry_run:
        for m in parsed.mentions[:5]:
            print(f"  [{m.brand_id}/{m.source}] {m.rating} {m.posted_at} {m.text[:70]}")
        return

    engine = db.get_engine()
    db.init_db(engine)
    db.sync_brands(engine, config)
    run_id = db.start_run(engine, "csv_import")
    new = db.insert_mentions(engine, parsed.mentions, run_id)
    db.finish_run(
        engine,
        run_id,
        "ok",
        {"file": str(args.path), "rows": parsed.rows_read, "new": new, "skipped": parsed.skipped},
        [],
    )
    print(f"Stored {new} new mentions ({len(parsed.mentions) - new} already in the database).")
    print(f"Database: {db.describe(engine)}")


if __name__ == "__main__":
    main()
