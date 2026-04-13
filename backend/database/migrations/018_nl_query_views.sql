-- =============================================================================
-- Migration 018 — NL query views for Ollama-backed natural-language search
-- =============================================================================
-- These CREATE VIEW IF NOT EXISTS statements are idempotent.
-- The views are listed in the Ollama system prompt so the LLM can use them
-- directly instead of hand-writing complex joins.

-- ---------------------------------------------------------------------------
-- 1. v_photos_active  —  Foundation: active, non-stub photos only
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photos_active AS
SELECT
    id           AS media_id,
    file_path,
    date_taken,
    camera_make,
    camera_model,
    gps_lat,
    gps_lon,
    width,
    height,
    file_format,
    vip_id
FROM media_files
WHERE removed_from_app = 0
  AND is_stub = 0;

-- ---------------------------------------------------------------------------
-- 2. v_person_photos  —  Named person → their photos (flat join)
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_person_photos AS
SELECT
    p.id         AS person_id,
    p.name       AS person_name,
    mf.id        AS media_id,
    mf.file_path,
    mf.date_taken,
    mf.camera_make,
    mf.camera_model,
    mf.gps_lat,
    mf.gps_lon
FROM persons p
JOIN faces f ON f.person_id = p.id
JOIN media_files mf ON mf.id = f.media_file_id
WHERE p.is_ignored  = 0
  AND p.is_merged   = 0
  AND mf.removed_from_app = 0
  AND mf.is_stub    = 0;

-- ---------------------------------------------------------------------------
-- 3. v_photo_tags_flat  —  One row per (active photo, tag label)
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photo_tags_flat AS
SELECT
    mf.id        AS media_id,
    mf.file_path,
    mf.date_taken,
    mf.gps_lat,
    mf.gps_lon,
    mt.category,
    mt.label,
    mt.confidence,
    mt.model
FROM media_files mf
JOIN media_tags mt ON mt.media_file_id = mf.id
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0;

-- ---------------------------------------------------------------------------
-- 4. v_photo_persons_agg  —  Per photo: aggregated person names + count
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photo_persons_agg AS
SELECT
    mf.id        AS media_id,
    mf.file_path,
    mf.date_taken,
    mf.camera_make,
    mf.camera_model,
    mf.gps_lat,
    mf.gps_lon,
    COUNT(DISTINCT f.person_id)          AS person_count,
    GROUP_CONCAT(DISTINCT p.name)        AS person_names
FROM media_files mf
LEFT JOIN faces   f  ON f.media_file_id = mf.id
LEFT JOIN persons p  ON p.id = f.person_id
                    AND p.is_ignored = 0
                    AND p.is_merged  = 0
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0
GROUP BY mf.id;

-- ---------------------------------------------------------------------------
-- 5. v_person_cooccurrence_named  —  Co-occurrence with real names
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_person_cooccurrence_named AS
SELECT
    pa.name      AS person_a,
    pb.name      AS person_b,
    co.count     AS shared_photo_count,
    co.last_seen_at
FROM person_cooccurrence co
JOIN persons pa ON pa.id = co.person_a_id
JOIN persons pb ON pb.id = co.person_b_id
WHERE pa.is_merged  = 0 AND pb.is_merged  = 0
  AND pa.is_ignored = 0 AND pb.is_ignored = 0;

-- ---------------------------------------------------------------------------
-- 6. v_photos_with_location  —  GPS photos + geography/place tags
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photos_with_location AS
SELECT
    mf.id        AS media_id,
    mf.file_path,
    mf.date_taken,
    mf.gps_lat,
    mf.gps_lon,
    mt.label     AS place_label,
    mt.category  AS place_category
FROM media_files mf
JOIN media_tags mt ON mt.media_file_id = mf.id
                  AND mt.category IN ('geography', 'place')
WHERE mf.removed_from_app = 0
  AND mf.is_stub  = 0
  AND mf.gps_lat  IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 7. v_person_photo_count  —  Person ranked by active photo count
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_person_photo_count AS
SELECT
    p.id         AS person_id,
    p.name,
    COUNT(DISTINCT f.media_file_id) AS photo_count
FROM persons p
JOIN faces f ON f.person_id = p.id
JOIN media_files mf ON mf.id = f.media_file_id
                   AND mf.removed_from_app = 0
                   AND mf.is_stub = 0
WHERE p.is_ignored = 0
  AND p.is_merged  = 0
GROUP BY p.id;

-- ---------------------------------------------------------------------------
-- 8. v_photos_by_year_month  —  Pre-extracted year/month integer columns
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photos_by_year_month AS
SELECT
    id                                                   AS media_id,
    file_path,
    date_taken,
    CAST(strftime('%Y', date_taken) AS INTEGER)          AS year,
    CAST(strftime('%m', date_taken) AS INTEGER)          AS month,
    camera_make,
    camera_model,
    gps_lat,
    gps_lon
FROM media_files
WHERE removed_from_app = 0
  AND is_stub     = 0
  AND date_taken  IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 9. v_unidentified_faces  —  Photos with at least one unresolved face
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_unidentified_faces AS
SELECT
    mf.id        AS media_id,
    mf.file_path,
    mf.date_taken,
    COUNT(f.id)  AS unidentified_face_count
FROM media_files mf
JOIN faces f ON f.media_file_id = mf.id
            AND f.person_id IS NULL
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0
GROUP BY mf.id;

-- ---------------------------------------------------------------------------
-- 10. v_photo_full_context  —  Wide view: photo + persons + objects + places
--     Use this first for complex multi-faceted NL queries.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_photo_full_context AS
SELECT
    mf.id            AS media_id,
    mf.file_path,
    mf.date_taken,
    mf.camera_make,
    mf.camera_model,
    mf.gps_lat,
    mf.gps_lon,
    mf.width,
    mf.height,
    (SELECT GROUP_CONCAT(DISTINCT p.name)
     FROM faces f
     JOIN persons p ON p.id = f.person_id
     WHERE f.media_file_id = mf.id
       AND p.is_ignored = 0
       AND p.is_merged  = 0)             AS persons,
    (SELECT GROUP_CONCAT(DISTINCT mt.label)
     FROM media_tags mt
     WHERE mt.media_file_id = mf.id
       AND mt.category = 'object')       AS objects,
    (SELECT GROUP_CONCAT(DISTINCT mt.label)
     FROM media_tags mt
     WHERE mt.media_file_id = mf.id
       AND mt.category IN ('place', 'geography')) AS places,
    (SELECT GROUP_CONCAT(DISTINCT mt.label)
     FROM media_tags mt
     WHERE mt.media_file_id = mf.id
       AND mt.category = 'animal')       AS animals,
    (SELECT GROUP_CONCAT(DISTINCT mt.label)
     FROM media_tags mt
     WHERE mt.media_file_id = mf.id
       AND mt.category = 'scene')        AS scenes
FROM media_files mf
WHERE mf.removed_from_app = 0
  AND mf.is_stub = 0;
