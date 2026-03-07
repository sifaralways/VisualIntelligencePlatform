-- Migration 012: track per-file tagging completion
--
-- tags_done = 0 (default) — Phase 4 (object/animal/geo/place tagging) has not
--                            been completed for this file, or has been reset by
--                            a force-retag rescan.
-- tags_done = 1           — Phase 4 completed successfully; file will be skipped
--                            on subsequent pipeline runs unless force_retag is set.
--
-- This prevents redundant YOLO/CLIP inference on files whose content has not
-- changed since the last scan (significant speedup on large libraries).

ALTER TABLE media_files ADD COLUMN tags_done INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_media_tags_done ON media_files(tags_done);
