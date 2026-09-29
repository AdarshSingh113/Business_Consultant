# Brand Consultant

An AI consultant for a brand. It watches what people say about the brand and its competitors,
spots problems on its own, asks for data when it needs it, and gives evidence-backed fixes.
Built only on free tools, as a learning project.

**Status: Phase 1 of 6.** Data comes in from CSV files and Reddit, is de-duplicated and stored.
No AI analysis yet.

| Phase | Build | Status |
|---|---|---|
| 1 | Config, database, LLM wrapper, CSV and Reddit collectors, daily job | Done |
| 2 | Tagger (review → issue, sentiment) with an accuracy test set, Perception Radar | Next |
| 3 | Anomaly detector, daily alerts by email | |
| 4 | Consultant agent: diagnosis, asks you for data | |
| 5 | Competitor teardown, recommendations | |
| 6 | Dashboard, weekly report | |

## How Phase 1 works

```
config/brands.yaml ──► which brands, competitors, subreddits, issue labels

data/imports/*.csv ──► collectors/csv_import.py ─┐
Reddit API ──────────► collectors/reddit.py ─────┤
                                                 ▼
                                  core/db.py  insert_mentions()
                                  (id = hash of source + brand + text,
                                   so duplicates are skipped)
                                                 ▼
                                  mentions table  +  runs table (log)
```

`jobs/daily.py` runs both collectors. Each one runs on its own, so a Reddit failure does not lose
the CSV data. Every run is logged in the `runs` table. The job exits with an error code if
anything failed, so GitHub Actions shows a red run instead of failing silently.

| Path | What it does |
|---|---|
| `config/brands.yaml` | Brands, aliases, subreddits, the fixed issue list. Change brands here, not in code |
| `core/config.py` | Loads and checks the config (exactly one client, no alias shared by two brands) |
| `core/db.py` | Database: SQLite locally, Supabase Postgres when `DATABASE_URL` is set |
| `core/llm.py` | Gemini first, Groq as backup, with rate limiting, retries and a response cache |
| `core/text.py` | Cleaning text, duplicate ids, parsing ratings and dates |
| `collectors/` | Get data in |
| `jobs/daily.py` | The daily run |
| `db/schema.sql` | Tables. Runs unchanged on SQLite and Postgres |
| `tests/` | `python -m pytest` |

## Run it locally (no accounts needed)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python -m pytest                                     # 19 tests

# Import the made-up sample reviews into a local SQLite file (data/local.db)
python -m collectors.csv_import data/sample/sample_reviews.csv
python -m collectors.csv_import data/sample/amazon_export_style.csv --brand boat --source amazon
python -m core.db stats
```

Run the import twice: the second time it stores 0 new reviews. That is the duplicate check working.

## Connect the free services

Add each key to `.env` for local runs, and as a **GitHub Secret** (repo → Settings → Secrets and
variables → Actions) for the daily job. Never put keys in code.

1. **Supabase (database).** Create a free project. Go to Project Settings → Database → Connection
   string, choose **URI** under **Session pooler**, and replace `[YOUR-PASSWORD]`.
   Save it as `DATABASE_URL`. Then run `python -m core.db init`.
2. **Gemini (AI).** Google AI Studio → Get API key → `GEMINI_API_KEY`. Check it with
   `python -m core.llm`. Not used by the pipeline until Phase 2.
3. **Groq (backup AI).** console.groq.com → API Keys → `GROQ_API_KEY`.
4. **Reddit.** reddit.com/prefs/apps → "create another app" → type **script**. Copy the id under
   the app name and the secret into `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`, and set
   `REDDIT_USER_AGENT` to something like `brand-consultant/0.1 by u/yourname`.
   Reddit has tightened API access, so new apps may need approval. Until then the job skips Reddit.
   Test with `python -m collectors.reddit --dry-run`.

## Your own review data

Copy public reviews into a CSV by hand, or use a public review dataset (Kaggle has Amazon and
Flipkart sets). **Don't scrape Amazon or Flipkart**: their terms forbid it, and scripts break often.

Column names are matched loosely (`review`, `review_text`, `Review Body` all work). Import with:

```bash
python -m collectors.csv_import my_reviews.csv --brand boat --source amazon --dry-run   # check first
python -m collectors.csv_import my_reviews.csv --brand boat --source amazon
```

With `DATABASE_URL` in `.env` pointing at Supabase, this goes straight into the cloud database.
CSVs in `data/imports/` are git-ignored because this repo is public.

## The daily job on GitHub Actions

`.github/workflows/daily.yml` runs every day at 08:47 IST, or on demand from the Actions tab
("Run workflow"). It fails on purpose if the `DATABASE_URL` secret is missing.

Two free-tier catches:
- **GitHub turns off scheduled workflows** in public repos after 60 days with no commits. Push
  something now and then, or re-enable it from the Actions tab.
- **Supabase pauses** free projects after a week of no activity. The daily job keeps it awake.
