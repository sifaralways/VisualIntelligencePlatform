-- Migration 013: backfill tags_done for files already in 'tagged' state.
--
-- Migration 012 added tags_done with DEFAULT 0, but pre-existing files that
-- had already completed Phase 4 (ingest_state = 'tagged') were not updated.
-- This caused Phase 4 to re-process the entire library on the next scan.
--
-- Set tags_done = 1 for every non-stub file already in the tagged state so
-- Phase 4 correctly skips them on future runs.

UPDATE media_files
   SET tags_done = 1
 WHERE ingest_state = 'tagged'
   AND is_stub = 0;
