-- Migration 010: store a one-time snapshot of pre-import EXIF metadata
--
-- external_exif: JSON blob captured at first INSERT of a media file.
--   Contains whatever rich XMP/IPTC fields were already in the photo
--   at the time VIP first saw it.  Never overwritten on re-scans.
--
--   Keys present (all optional):
--     identifier   - XMP:Identifier value found in file (UUID → VIP-written previously)
--     persons      - XMP:PersonInImage array
--     keywords     - XMP:Subject / IPTC:Keywords array
--     location     - XMP:Location string
--     region_info  - XMP-mwg-rs:RegionInfo struct (face regions with names)
--
--   Display logic in the Analysis UI:
--     "VIP History"      → external_exif.identifier is non-null
--     "External History" → external_exif.identifier is null but other keys present
--     "VIP Pending"      → writeback_queue row with status='pending' exists

ALTER TABLE media_files ADD COLUMN external_exif TEXT; -- JSON, nullable, set once on INSERT
