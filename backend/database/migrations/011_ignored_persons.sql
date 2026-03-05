-- Migration 011: add is_ignored flag to persons
--
-- Ignored persons are created when the user selects "Always ignore" on an
-- unnamed cluster tile. Their faces are never shown in the UI and new face
-- detections that match an ignored person's centroid are silently suppressed
-- during Phase 3b (auto-merge).
--
-- is_ignored = 0 (default) — normal person, shown in UI
-- is_ignored = 1           — suppressed; never surfaced to the user

ALTER TABLE persons ADD COLUMN is_ignored INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_persons_ignored ON persons(is_ignored);
