-- =============================================================================
-- Migration 003 — Stable photo UUID + analysis documents + user amendments
-- VIP — Visual Intelligence Platform
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Add vip_id to media_files
-- Stable UUID that survives file rename/move. Written to XMP:Identifier on
-- writeback. Backfilled by db.py init_db() for any existing rows.
-- ---------------------------------------------------------------------------
ALTER TABLE media_files ADD COLUMN vip_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_vip_id ON media_files(vip_id);

-- ---------------------------------------------------------------------------
-- Add face_attributes to faces
-- JSON blob storing rich per-face attributes from InsightFace Buffalo_L:
--   age, gender, pose (yaw/pitch/roll), landmarks (5-point kps), quality.
-- Kept as a blob rather than columns to avoid schema churn as more
-- attributes are added (e.g. emotions, eyeglasses) in future phases.
-- ---------------------------------------------------------------------------
ALTER TABLE faces ADD COLUMN face_attributes TEXT; -- JSON, nullable

-- ---------------------------------------------------------------------------
-- photo_analysis: one row per media file — the auto-generated analysis doc
--
-- model_document:  Full Rekognition-compatible JSON blob. Built by Phase 5.
--                  Contains Labels (objects/animals/scenes/places), Faces
--                  (bbox + person_id — never person_name), and Geography.
--
-- model_version:   Version string of the models that produced this document
--                  e.g. 'yolo11s/places365/bioclip/insightface-buffalo_l'.
--                  When the pipeline reruns, a changed version triggers doc
--                  rebuild; user amendments are preserved separately.
--
-- generated_at:    When Phase 5 created this row.
-- updated_at:      When Phase 5 last updated this row (re-run).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photo_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL UNIQUE REFERENCES media_files(id) ON DELETE CASCADE,
    model_document  TEXT    NOT NULL DEFAULT '{}',
    model_version   TEXT    NOT NULL DEFAULT '',
    generated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_analysis_media ON photo_analysis(media_file_id);

-- ---------------------------------------------------------------------------
-- photo_analysis_amendments: user edits to individual labels
--
-- action values:
--   'rename'   — user renamed the model label; user_value = new name
--   'delete'   — user removed the label; user_value = NULL
--   'add'      — user added a new label not found by the model;
--                label_name holds the new label, user_value = NULL
--   'confirm'  — user explicitly confirmed the model label; no change to name
--
-- Constraint: one amendment per (media_file_id, label_name) pair.
-- To fully undo, DELETE the row — the model label is restored automatically.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS photo_analysis_amendments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    label_name      TEXT    NOT NULL,   -- original model label (or new label for 'add')
    action          TEXT    NOT NULL,   -- 'rename' | 'delete' | 'add' | 'confirm'
    user_value      TEXT,               -- new name (rename); null for delete/confirm/add
    user_confidence REAL,               -- user-overridden confidence; null = keep model's
    amended_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (media_file_id, label_name)
);

CREATE INDEX IF NOT EXISTS idx_amendments_media ON photo_analysis_amendments(media_file_id);
CREATE INDEX IF NOT EXISTS idx_amendments_label ON photo_analysis_amendments(label_name);
