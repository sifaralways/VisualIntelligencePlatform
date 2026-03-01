-- Migration 009: enforce one queue row per file
-- Remove duplicate writeback_queue rows (keep the highest-priority status per file:
-- written > failed > pending), then add a UNIQUE index so INSERT OR REPLACE
-- correctly either inserts or resets an existing row back to 'pending'.

-- 1. Keep only the best row per media_file_id (written > failed > pending)
DELETE FROM writeback_queue
WHERE id NOT IN (
    SELECT id FROM (
        SELECT id,
               ROW_NUMBER() OVER (
                   PARTITION BY media_file_id
                   ORDER BY
                       CASE status
                           WHEN 'written'   THEN 0
                           WHEN 'failed'    THEN 1
                           WHEN 'confirmed' THEN 2
                           WHEN 'dry_run'   THEN 3
                           ELSE                  4   -- pending / anything else
                       END,
                       id DESC          -- tie-break: newest row wins
               ) AS rn
        FROM writeback_queue
    ) ranked
    WHERE rn = 1
);

-- 2. Now that duplicates are gone, enforce uniqueness going forward
CREATE UNIQUE INDEX IF NOT EXISTS idx_writeback_media_unique
    ON writeback_queue(media_file_id);
