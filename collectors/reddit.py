"""Collect Reddit posts and comments that mention each brand.

Needs a free Reddit "script" app (reddit.com/prefs/apps) with
REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT set.
If they are missing, the daily job skips Reddit instead of failing.

Try it: python -m collectors.reddit --dry-run
"""

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from core import db
from core.config import Brand, Config, load_config
from core.text import clean_text, matches_alias, mention_id

ENV_VARS = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")


def is_configured() -> bool:
    return all(os.environ.get(v) for v in ENV_VARS)


def is_relevant(text: str, brand: Brand, config: Config) -> bool:
    """The brand must be named AND the text must be about the category.

    Reddit search is fuzzy, and "boat" or "noise" are everyday words, so a
    brand-name match alone lets in posts about fishing boats or loud neighbours.
    """
    return matches_alias(text, brand.search_names) and matches_alias(text, config.context_keywords)


def search_query(brand: Brand) -> str:
    names = list(dict.fromkeys([brand.name] + brand.aliases))
    return " OR ".join(f'"{n}"' for n in names)


def _posted_at(created_utc: float) -> str:
    return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat(timespec="seconds")


def fetch(config: Config) -> tuple[list[db.Mention], dict]:
    import praw

    settings = config.sources.get("reddit", {})
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )
    reddit.read_only = True
    subreddits = reddit.subreddit("+".join(settings.get("subreddits") or ["all"]))

    mentions: list[db.Mention] = []
    stats = {"posts_seen": 0, "irrelevant": 0}
    for brand in config.brands:
        posts = subreddits.search(
            search_query(brand),
            sort="new",
            time_filter=settings.get("time_filter", "month"),
            limit=int(settings.get("posts_per_query", 25)),
        )
        for post in posts:
            stats["posts_seen"] += 1
            post_text = clean_text(f"{post.title}. {post.selftext}")
            if is_relevant(post_text, brand, config):
                mentions.append(
                    db.Mention(
                        id=mention_id("reddit", brand.id, post_text),
                        brand_id=brand.id,
                        source="reddit",
                        text=post_text,
                        title=clean_text(post.title),
                        source_ref=f"https://www.reddit.com{post.permalink}",
                        posted_at=_posted_at(post.created_utc),
                    )
                )
            else:
                stats["irrelevant"] += 1
                continue

            # Top comments often hold the real opinions ("mine died after 3 months").
            post.comment_sort = "top"
            post.comments.replace_more(limit=0)
            for comment in post.comments[: int(settings.get("comments_per_post", 5))]:
                body = clean_text(comment.body)
                # The post already set the category context, so a brand mention is enough here.
                if not matches_alias(body, brand.search_names):
                    continue
                mentions.append(
                    db.Mention(
                        id=mention_id("reddit", brand.id, body),
                        brand_id=brand.id,
                        source="reddit",
                        text=body,
                        source_ref=f"https://www.reddit.com{comment.permalink}",
                        posted_at=_posted_at(comment.created_utc),
                    )
                )
    stats["mentions"] = len(mentions)
    return mentions, stats


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Collect Reddit mentions")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args()

    if not is_configured():
        raise SystemExit(f"Reddit is not configured. Set {', '.join(ENV_VARS)} in .env")

    config = load_config()
    mentions, stats = fetch(config)
    print(f"Found {len(mentions)} relevant mentions. {stats}")
    if args.dry_run:
        for m in mentions[:10]:
            print(f"  [{m.brand_id}] {m.text[:90]}")
        return

    engine = db.get_engine()
    db.init_db(engine)
    db.sync_brands(engine, config)
    print(f"Stored {db.insert_mentions(engine, mentions)} new mentions.")


if __name__ == "__main__":
    main()
