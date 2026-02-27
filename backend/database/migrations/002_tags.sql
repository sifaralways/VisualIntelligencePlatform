-- =============================================================================
-- Migration 002 — Media tags (objects, animals, geography, places)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- media_tags: ML-generated tags per media file
-- One row per (file, category, label) triple.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    category        TEXT    NOT NULL,   -- 'object' | 'animal' | 'geography' | 'place'
    label           TEXT    NOT NULL,
    confidence      REAL,               -- model confidence score 0–1
    model           TEXT    NOT NULL,   -- 'yolov11' | 'places365' | 'clip' | 'bioclip' | 'nominatim'
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (media_file_id, category, label)
);

CREATE INDEX IF NOT EXISTS idx_tags_file     ON media_tags(media_file_id);
CREATE INDEX IF NOT EXISTS idx_tags_category ON media_tags(category);
CREATE INDEX IF NOT EXISTS idx_tags_label    ON media_tags(label);
