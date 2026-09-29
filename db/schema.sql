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

-- LLM responses keyed by prompt hash, so the same review is never paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
