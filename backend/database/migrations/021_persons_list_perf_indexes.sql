-- Migration 021: performance indexes for persons listing and merge lookups
--
-- Targets hotspots seen in /api/persons list query plan:
-- 1) correlated lookup by (merged_into_id, is_merged)
-- 2) active-person filtering by (is_ignored, is_merged)

CREATE INDEX IF NOT EXISTS idx_persons_merged_into_is_merged
ON persons(merged_into_id, is_merged);

CREATE INDEX IF NOT EXISTS idx_persons_active_flags
ON persons(is_ignored, is_merged);
