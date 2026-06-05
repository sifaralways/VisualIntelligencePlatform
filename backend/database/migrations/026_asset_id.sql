-- Add a stable asset identifier for each media row.
-- asset_id is now the canonical per-asset identifier that VIP writes to XMP:Identifier.

ALTER TABLE media_files ADD COLUMN asset_id TEXT;

-- Existing rows: keep compatibility by seeding asset_id from vip_id when available.
UPDATE media_files
SET asset_id = COALESCE(vip_id, lower(hex(randomblob(16))))
WHERE asset_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_media_asset_id ON media_files(asset_id);
