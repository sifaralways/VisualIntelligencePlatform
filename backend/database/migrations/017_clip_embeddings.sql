-- Migration 017: per-photo CLIP embeddings for natural-language visual search

CREATE TABLE IF NOT EXISTS clip_embeddings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL UNIQUE REFERENCES media_files(id) ON DELETE CASCADE,
    vector        BLOB    NOT NULL,
    model_name    TEXT    NOT NULL,
    embed_dim     INTEGER NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_clip_embeddings_media
    ON clip_embeddings(media_file_id);
