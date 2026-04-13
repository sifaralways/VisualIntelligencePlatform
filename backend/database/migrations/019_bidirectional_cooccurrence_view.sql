-- =============================================================================
-- Migration 019 — Rebuild v_person_cooccurrence_named as bidirectional
-- =============================================================================
-- The original view (018) had one row per pair with person_a_id < person_b_id.
-- Ollama always queries WHERE person_a LIKE '%X%', so if X has the higher ID
-- it appeared only in person_b and was never found.
-- This migration replaces the view with a UNION that exposes both directions,
-- so every person always appears in the person_a column.

DROP VIEW IF EXISTS v_person_cooccurrence_named;

CREATE VIEW v_person_cooccurrence_named AS
-- Forward direction: original ordering
SELECT
    pa.name  AS person_a,
    pb.name  AS person_b,
    co.count AS shared_photo_count,
    co.last_seen_at
FROM person_cooccurrence co
JOIN persons pa ON pa.id = co.person_a_id
JOIN persons pb ON pb.id = co.person_b_id
WHERE pa.is_merged  = 0 AND pb.is_merged  = 0
  AND pa.is_ignored = 0 AND pb.is_ignored = 0

UNION ALL

-- Reverse direction: swap a and b so every person can be queried as person_a
SELECT
    pb.name  AS person_a,
    pa.name  AS person_b,
    co.count AS shared_photo_count,
    co.last_seen_at
FROM person_cooccurrence co
JOIN persons pa ON pa.id = co.person_a_id
JOIN persons pb ON pb.id = co.person_b_id
WHERE pa.is_merged  = 0 AND pb.is_merged  = 0
  AND pa.is_ignored = 0 AND pb.is_ignored = 0;
