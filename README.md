# Brand Consultant

An AI consultant for a brand. It watches what people say about the brand and its competitors,
spots problems on its own, asks for data when it needs it, and gives evidence-backed fixes.
Built only on free tools, as a learning project.

**Status: all 6 phases built.** Data comes in from CSV files and Reddit, the LLM tags each review with
issues and sentiment, the Perception Radar compares the client with its competitors, a daily
detector emails you when complaints spike, and a consultant agent investigates each spike,
asks you for data when reviews aren't enough, reports likely root causes with evidence, and
recommends prioritized actions. A competitor teardown shows what to copy, defend and launch,
a weekly report lands in your inbox, and a Streamlit dashboard shows it all.

| Phase | Build | Status |
|---|---|---|
| 1 | Config, database, LLM wrapper, CSV and Reddit collectors, daily job | Done |
| 2 | Tagger (review → issue, sentiment) with an accuracy test set, Perception Radar | Done |
| 3 | Anomaly detector, daily alerts by email | Done |
| 4 | Consultant agent: diagnosis, asks you for data | Done |
| 5 | Competitor teardown, recommendations | Done |
| 6 | Dashboard, weekly report | Done |

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

## How Phase 2 works

```
mentions ──► brain/tagger.py ──► LLM (10 reviews per call) ──► validate ──► mention_tags + aspect_tags
                                                                  │
                     labels must be in the issue list, sentiments in the allowed set,
                     quotes must really appear in the review; anything else is dropped
                                                                  ▼
             brain/perception.py (pure math) ──► Perception Radar: per brand and issue,
                                                 net sentiment, talk share, client vs competitors
```

- A review can have several aspects: "Sound accha hai but battery 2 ghante mein khatam" is
  `sound_quality: positive` and `battery_life: negative`.
- Failed batches are recorded and retried on the next run, up to 3 times.
- Changing the prompt? Bump `PROMPT_VERSION` in `brain/tagger.py` and everything gets re-tagged.
- The radar flags anything built on fewer than 5 reviews as low data, and only calls a
  strength or weakness when the gap is at least 0.2 with enough reviews on both sides.

```bash
python -m brain.tagger --limit 20      # tag a few mentions
python -m brain.perception --days 90   # print the radar
python -m evals.eval_tagger            # score the tagger against hand-labeled reviews
```

**Before trusting the tagger**, add about 100 real reviews you labeled yourself to
`evals/labeled_reviews.csv` (see `evals/README.md`). Aim for an issue F1 of 0.8 or higher.

## How Phase 3 works

Every day, for each brand, the detector compares the **last 7 days** with the **28 days before**:

| Signal | Example |
|---|---|
| `complaint_spike` | boAt reviews complaining about battery: 5% → 40% |
| `low_rating_spike` | boAt 1-2 star reviews: 10% → 40% |

A change only counts when **all** of these hold, so "2 complaints became 4" doesn't page you:
- at least 10 recent and 20 baseline reviews for the brand
- at least 3 recent complaints
- a two-proportion z-test of 3 or more. About 57 checks run a day, so at the textbook 2, about
  one would fire by pure chance most days
- a rise of at least 5 percentage points (big enough to matter)

A spike at the client is a **problem**. A spike at a competitor is an **opportunity**. The same
signal is not raised again within 7 days. Thresholds are constants at the top of `brain/detector.py`.

Alerts go by Gmail with real review quotes as evidence. Without Gmail, they are printed in the job
log and on the GitHub Actions run summary page.

```bash
python -m brain.detector                    # what would fire today
python -m brain.detector --as-of 2026-08-31 # replay a past date
```

## How Phase 4 works: the agent

```
anomaly ──► diagnosis agent (brain/diagnosis.py), up to 10 LLM turns
              │  each turn the LLM replies with ONE JSON action:
              ├─ call_tool ──► brain/tools.py (plain SQL) ──► result added to transcript
              │     query_reviews · breakdown (by product/source) · issue_breakdown · compare_competitors
              ├─ request_user_data ──► saved + emailed to you, status = waiting, stop
              │                        (next daily run resumes once you answer)
              └─ final ──► evidence checked ──► diagnosis saved + emailed
```

- The LLM chooses what to look at. **The code decides what counts**: reviews are shown to the
  agent as refs (`m1`, `m2`), hypotheses must cite them, refs it was never shown are removed,
  and a hypothesis with no valid evidence has its confidence capped at 30%.
- Every turn is stored in the `diagnoses.transcript` column, so you can read how it reasoned.
- If the LLM is down, progress is saved and the next run continues.
- Up to 3 new investigations per day, client problems first, to fit free LLM quotas.

```bash
python -m demo.run_demo                   # whole pipeline on made-up data with a real LLM
python -m brain.consultant requests       # questions the agent is waiting on
python -m brain.consultant answer req-1a2b3c4d "Yes, new cell supplier from 20 Aug"
python -m brain.consultant answer req-1a2b3c4d --file returns.csv
python -m brain.consultant show           # latest diagnoses
```

Known limits: the agent's confidence numbers are its own judgement, not a probability, and it can
be overconfident on a handful of near-identical reviews. Read the quotes.

## How Phase 5 works

**Competitor Teardown** (`brain/teardown.py`). Math picks the candidates from the radar, the LLM
writes the insight and action for each, citing real quotes:

| Section | Rule (enough reviews on both sides) |
|---|---|
| Copy | a competitor beats the client by 0.2+ net sentiment on an issue, **and is net positive** |
| Fight | the client beats the competitors by 0.2+, **and is net positive** |
| Gaps | every brand with data is net negative on an issue: an unmet need |

"Less bad" never counts as good: boAt at −0.3 on battery vs competitors at −0.5 is not a strength.

**Recommendations** (`brain/recommender.py`). For each finished diagnosis the LLM proposes
2-5 actions and rates impact and effort. The code sets the priority:

    score = impact (1-3) x confidence in the cause / effort (1-3)      P1 >= 1.5, P2 >= 0.6, else P3

Actions aimed at an uncertain cause (under 60%) are marked **Confirm first**. For a competitor's
problem, the actions are about winning their unhappy customers.

```bash
python -m brain.teardown --days 90     # print and save a teardown
python -m brain.teardown --no-llm      # the math part only
```

## How Phase 6 works

**Weekly report** (`jobs/weekly.py`, Mondays 09:13 IST via `.github/workflows/weekly.yml`):
this week's alerts, finished diagnoses with recommended actions, questions waiting for you,
a fresh competitor teardown, the Perception Radar, and pipeline health. Saved to the `reports`
table and emailed.

**Dashboard** (`outputs/dashboard.py`):

| View | What it shows |
|---|---|
| Perception Radar | net sentiment per brand, issue-by-brand heatmap (blue good, red bad), strengths and weaknesses |
| Alerts & diagnoses | every alert, its diagnosis and actions, and the agent's step-by-step reasoning |
| Questions for you | the agent's data requests, with a form to answer (text or CSV upload) |
| Competitor teardown | the latest saved teardown |
| Reviews | browse tagged reviews by brand, issue and sentiment |
| Pipeline health | recent runs, failures, the latest weekly report |

```bash
streamlit run outputs/dashboard.py
DATABASE_URL=sqlite:///data/demo.db streamlit run outputs/dashboard.py   # after python -m demo.run_demo
```

**Deploy it free** on Streamlit Community Cloud: share.streamlit.io → New app → this repo,
branch `main`, main file `outputs/dashboard.py`. Under the app's Settings → Secrets add:

```toml
DATABASE_URL = "postgresql://postgres.xxxx:your%40password@aws-0-....pooler.supabase.com:5432/postgres"
DASHBOARD_PASSWORD = "pick-a-long-password"
```

The app is public on the internet and can write answers to your database, so it refuses to
connect to a cloud database until `DASHBOARD_PASSWORD` is set.

## Run it locally (no accounts needed)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

python -m pytest                                     # 50 tests
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres python -m pytest   # same tests on Postgres

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
   If the password contains special characters, encode them: `@` → `%40`, `#` → `%23`,
   `/` → `%2F`, `:` → `%3A`. Save it as `DATABASE_URL`. Then run `python -m core.db init`.
   Tables get row level security turned on, so Supabase's public REST API can't read them.
2. **Gemini (AI).** Google AI Studio → Get API key → `GEMINI_API_KEY`. Check it with
   `python -m core.llm`. `GEMINI_MODEL` can list several models, tried in order; Google retires
   model names and overloads popular ones, so the default has a backup.
3. **Groq (backup AI).** console.groq.com → API Keys → `GROQ_API_KEY`.
4. **Gmail alerts.** Turn on 2-Step Verification, then Google Account → Security → App passwords.
   Set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and optionally `ALERT_TO`.
5. **Reddit.** reddit.com/prefs/apps → "create another app" → type **script**. Copy the id under
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
