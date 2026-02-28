-- 004_app_settings.sql
-- Persisted ML / detection tuning parameters.
-- Each row is one setting identified by its key.
-- The application reads these at pipeline start and caches them in memory.
-- Default values here must match DEFAULTS in backend/database/settings_store.py.

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,          -- stored as text, cast to correct type in Python
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed defaults (ignored if row already exists)
INSERT OR IGNORE INTO app_settings (key, value) VALUES
    ('face_detection_threshold', '0.6'),
    ('min_face_size_px',         '60'),
    ('gender_min_sharpness',     '15.0'),
    ('hdbscan_min_cluster_size', '2'),
    ('hdbscan_min_samples',      '1'),
    ('hdbscan_cluster_epsilon',  '0.04'),
    ('cluster_inertia_threshold','0.85'),
    ('high_confidence_threshold','0.92'),
    ('yolo_conf_threshold',      '0.50'),
    ('places365_top_k',          '5'),
    ('landmark_threshold',       '0.26'),
    ('species_threshold',        '0.30');
