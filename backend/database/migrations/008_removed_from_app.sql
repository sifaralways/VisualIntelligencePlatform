-- 008_removed_from_app.sql
-- Soft-remove: mark a photo as hidden from the app without deleting it from
-- the database. This preserves the file_hash → vip_id mapping so that if the
-- photo is re-scanned in the future it is re-identified correctly and person
-- names / tags are re-applied.
--
-- removed_from_app: 1 = hidden from all UI queries but DB row is intact.
--                   0 = visible (default).
--
-- All media list / count queries filter with removed_from_app = 0.

ALTER TABLE media_files ADD COLUMN removed_from_app INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_media_removed ON media_files(removed_from_app);
