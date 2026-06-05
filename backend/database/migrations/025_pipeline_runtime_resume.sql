-- =============================================================================
-- Migration 025 — Durable pipeline runtime state + Florence resume tracking
-- VIP — Visual Intelligence Platform
-- =============================================================================

ALTER TABLE media_files ADD COLUMN florence_done INTEGER NOT NULL DEFAULT 0;

-- Best-effort backfill: if Florence tags already exist for a photo, consider
-- Florence enrichment complete for resume purposes.
UPDATE media_files
SET florence_done = 1
WHERE id IN (
    SELECT DISTINCT media_file_id
    FROM media_tags
    WHERE category IN ('caption', 'ocr', 'region')
);

CREATE TABLE IF NOT EXISTS pipeline_runtime_state (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    status                  TEXT NOT NULL DEFAULT 'idle',
    run_kind                TEXT,
    folder                  TEXT,
    use_existing_vip_data   INTEGER,
    last_phase              TEXT,
    resumable               INTEGER NOT NULL DEFAULT 0,
    error                   TEXT,
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO pipeline_runtime_state (id, status, resumable, updated_at)
VALUES (1, 'idle', 0, datetime('now'))
ON CONFLICT(id) DO NOTHING;
