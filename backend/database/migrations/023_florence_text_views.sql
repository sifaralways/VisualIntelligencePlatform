-- =============================================================================
-- Migration 023 — Florence text query views for Assistant/NL SQL generation
-- =============================================================================

-- Optional composite index improves text branch filtering by category/label.
CREATE INDEX IF NOT EXISTS idx_tags_category_label_media
ON media_tags(category, label, media_file_id);

-- ---------------------------------------------------------------------------
-- v_photo_text_flat — one row per Florence text snippet
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_photo_text_flat;
CREATE VIEW v_photo_text_flat AS
SELECT
    mf.id           AS media_id,
    mf.file_path,
    mf.date_taken,
    mt.category     AS text_type,   -- caption | ocr | region
    mt.label        AS text_value,
    mt.confidence,
    mt.model
FROM media_files mf
JOIN media_tags mt ON mt.media_file_id = mf.id
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0
  AND mt.category IN ('caption', 'ocr', 'region');

-- ---------------------------------------------------------------------------
-- v_photo_text_agg — per-photo Florence text buckets + combined text blob
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_photo_text_agg;
CREATE VIEW v_photo_text_agg AS
SELECT
    mf.id            AS media_id,
    mf.file_path,
    mf.date_taken,
    (
        SELECT GROUP_CONCAT(mt1.label, ' || ')
        FROM media_tags mt1
        WHERE mt1.media_file_id = mf.id
          AND mt1.category = 'caption'
    ) AS captions,
    (
        SELECT GROUP_CONCAT(mt2.label, ' || ')
        FROM media_tags mt2
        WHERE mt2.media_file_id = mf.id
          AND mt2.category = 'ocr'
    ) AS ocr_text,
    (
        SELECT GROUP_CONCAT(mt3.label, ' || ')
        FROM media_tags mt3
        WHERE mt3.media_file_id = mf.id
          AND mt3.category = 'region'
    ) AS region_text,
    (
        SELECT GROUP_CONCAT(mt4.label, ' || ')
        FROM media_tags mt4
        WHERE mt4.media_file_id = mf.id
          AND mt4.category IN ('caption', 'ocr', 'region')
    ) AS all_text
FROM media_files mf
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0;
