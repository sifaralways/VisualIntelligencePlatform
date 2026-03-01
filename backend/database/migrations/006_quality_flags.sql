-- 006_quality_flags.sql
-- Per-image quality signals computed during Phase 2 (embed).
--
-- blur_score:      Laplacian variance of the preview, normalised 0–100.
--                  Lower = blurrier. NULL = not yet assessed.
-- is_blurry:       1 = photo is out of focus (blur_score below threshold AND
--                  shutter speed was fast — rules out long exposure).
--                  0 = not blurry (or long exposure intentionally blurred).
--                  NULL = not yet assessed.
-- long_exposure:   1 = ExifTool reported ExposureTime >= 1/30 s. These photos
--                  may be blurry by design (light trails, panning, night shots)
--                  and are never flagged as defocus blur.
-- exposure_time_s: Shutter speed in seconds from EXIF (e.g. 0.005 = 1/200 s).
-- has_closed_eyes: 1 = at least one named face in the photo has EyesOpen=False.
--                  0 = all detected faces have open eyes (or no faces).
--                  NULL = not yet assessed.

ALTER TABLE media_files ADD COLUMN blur_score       REAL    DEFAULT NULL;
ALTER TABLE media_files ADD COLUMN is_blurry        INTEGER DEFAULT NULL;
ALTER TABLE media_files ADD COLUMN long_exposure    INTEGER DEFAULT NULL;
ALTER TABLE media_files ADD COLUMN exposure_time_s  REAL    DEFAULT NULL;
ALTER TABLE media_files ADD COLUMN has_closed_eyes  INTEGER DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_media_blurry      ON media_files(is_blurry);
CREATE INDEX IF NOT EXISTS idx_media_closed_eyes ON media_files(has_closed_eyes);
