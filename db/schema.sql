-- Phase 1 schema. Written to run unchanged on SQLite (local) and Postgres (Supabase).
-- Later phases add tags, metrics, anomalies, diagnoses and data_requests.
-- Timestamps are stored as ISO-8601 UTC strings written by the Python code.

CREATE TABLE IF NOT EXISTS brands (
    id          TEXT PRIMARY KEY,                -- slug from config/brands.yaml
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    is_client   BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TEXT NOT NULL
);

-- One row per review, post or comment about a brand.
CREATE TABLE IF NOT EXISTS mentions (
    id            TEXT PRIMARY KEY,              -- hash of source + brand + normalized text, the dedup key
    brand_id      TEXT NOT NULL REFERENCES brands(id),
    product       TEXT,
    source        TEXT NOT NULL,                 -- amazon, flipkart, reddit, youtube, web
    source_ref    TEXT,                          -- URL or platform id, when known
    title         TEXT,
    text          TEXT NOT NULL,
    rating        REAL,                          -- 1 to 5 for marketplace reviews, NULL otherwise
    posted_at     TEXT,                          -- when the author posted it (date or datetime)
    collected_at  TEXT NOT NULL,                 -- when we stored it
    run_id        TEXT                           -- which job run brought it in
);

CREATE INDEX IF NOT EXISTS idx_mentions_brand_posted ON mentions (brand_id, posted_at);
CREATE INDEX IF NOT EXISTS idx_mentions_source ON mentions (source);

-- Every job run is logged, so failures are never silent.
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    job          TEXT NOT NULL,                  -- daily, weekly, csv_import
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,                  -- running, ok, partial, failed
    stats        TEXT,                           -- JSON: counts per collector
    errors       TEXT                            -- JSON list of error messages
);

-- Phase 2: one row per tagged mention, the review-level result.
CREATE TABLE IF NOT EXISTS mention_tags (
    mention_id         TEXT PRIMARY KEY REFERENCES mentions(id),
    status             TEXT NOT NULL,            -- ok, failed
    relevant           BOOLEAN,                  -- FALSE for off-topic text (questions, ads, other products)
    overall_sentiment  TEXT,                     -- positive, negative, mixed, neutral
    attempts           INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    provider           TEXT,
    model              TEXT,
    prompt_version     TEXT,                     -- re-tag when the prompt changes
    tagged_at          TEXT NOT NULL
);

-- Phase 2: one row per issue a review talks about. "Sound good, battery bad" gives two rows.
CREATE TABLE IF NOT EXISTS aspect_tags (
    mention_id  TEXT NOT NULL REFERENCES mentions(id),
    issue       TEXT NOT NULL,                   -- a label from the issues list in brands.yaml
    sentiment   TEXT NOT NULL,                   -- positive, negative, neutral
    quote       TEXT,                            -- words copied from the review, checked to really be there
    PRIMARY KEY (mention_id, issue)
);

CREATE INDEX IF NOT EXISTS idx_aspect_tags_issue ON aspect_tags (issue, sentiment);

-- Phase 3: a statistically unusual change the detector found.
CREATE TABLE IF NOT EXISTS anomalies (
    id              TEXT PRIMARY KEY,            -- hash of brand + kind + issue + window end
    brand_id        TEXT NOT NULL REFERENCES brands(id),
    kind            TEXT NOT NULL,               -- complaint_spike, low_rating_spike
    issue           TEXT,                        -- for complaint_spike, NULL for ratings
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    recent_hits     INTEGER NOT NULL,            -- e.g. battery complaints in the last 7 days
    recent_total    INTEGER NOT NULL,            -- reviews in the last 7 days
    baseline_hits   INTEGER NOT NULL,
    baseline_total  INTEGER NOT NULL,
    z_score         REAL NOT NULL,
    status          TEXT NOT NULL,               -- new, alerted, diagnosing, diagnosed, dismissed
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_anomalies_status ON anomalies (status, created_at);

-- LLM responses keyed by prompt hash, so the same review is never paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
