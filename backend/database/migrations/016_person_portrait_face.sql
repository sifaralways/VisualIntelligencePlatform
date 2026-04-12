-- =============================================================================
-- Migration 016 — person portrait face
--
-- Adds portrait_face_id to persons so users can pin a specific face crop
-- as the representative thumbnail instead of the auto-selected MIN() one.
-- =============================================================================

ALTER TABLE persons ADD COLUMN portrait_face_id INTEGER REFERENCES faces(id);
