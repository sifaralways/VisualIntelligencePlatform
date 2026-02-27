-- =============================================================================
-- Migration 001 — Initial schema
-- VIP — Visual Intelligence Platform
-- =============================================================================

-- ---------------------------------------------------------------------------
-- media_files: one row per unique file (identified by SHA-256 hash)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS media_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT    NOT NULL,               -- absolute path at last scan
    file_hash       TEXT    NOT NULL UNIQUE,        -- SHA-256, true identity of file
    file_size       INTEGER,                        -- bytes
    file_format     TEXT,                           -- 'CR3', 'ARW', 'NEF', 'DNG', etc.
    camera_make     TEXT,
    camera_model    TEXT,
    date_taken      TEXT,                           -- ISO8601 from EXIF DateTimeOriginal
    gps_lat         REAL,
    gps_lon         REAL,
    width           INTEGER,                        -- image width in pixels
    height          INTEGER,
    is_stub         INTEGER NOT NULL DEFAULT 0,     -- 1 = iCloud stub at scan time
    needs_reprocess INTEGER NOT NULL DEFAULT 0,     -- 1 = queued for re-evaluation
    ingest_state    TEXT    NOT NULL DEFAULT 'pending',
                                                    -- pending|scanned|embedded|clustered
    first_seen_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    writeback_done  INTEGER NOT NULL DEFAULT 0,     -- 1 = ExifTool write completed
    writeback_at    TEXT                            -- ISO8601 of last write
);

CREATE INDEX IF NOT EXISTS idx_media_hash       ON media_files(file_hash);
CREATE INDEX IF NOT EXISTS idx_media_state      ON media_files(ingest_state);
CREATE INDEX IF NOT EXISTS idx_media_date       ON media_files(date_taken);
CREATE INDEX IF NOT EXISTS idx_media_reprocess  ON media_files(needs_reprocess);

-- ---------------------------------------------------------------------------
-- scan_state: tracks which folders have been scanned and when
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_path     TEXT    NOT NULL UNIQUE,
    last_scan_at    TEXT,
    file_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT    NOT NULL DEFAULT 'idle'  -- idle|scanning|error
);

-- ---------------------------------------------------------------------------
-- persons: a named individual. Created when a cluster is named by the user.
-- UUID is stable and written into files via ExifTool.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT    NOT NULL UNIQUE,        -- stable UUID, written to XMP
    name            TEXT,                           -- human-assigned, nullable until named
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    named_at        TEXT,
    photo_count     INTEGER NOT NULL DEFAULT 0,     -- denormalised, updated on change
    is_merged       INTEGER NOT NULL DEFAULT 0,     -- 1 = merged into another person
    merged_into_id  INTEGER REFERENCES persons(id)
);

CREATE INDEX IF NOT EXISTS idx_persons_uuid ON persons(uuid);
CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(name);

-- ---------------------------------------------------------------------------
-- clusters: a HDBSCAN cluster of face embeddings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clusters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id           INTEGER REFERENCES persons(id),  -- set when named
    centroid            BLOB,                            -- mean 512-D float32 vector
    member_count        INTEGER NOT NULL DEFAULT 0,
    intra_similarity    REAL,                            -- mean cosine similarity within cluster
    is_high_conf        INTEGER NOT NULL DEFAULT 0,      -- 1 = above high_confidence_threshold
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    last_updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- faces: a detected face within a media file
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    bbox_x          REAL,           -- normalised 0–1 (left)
    bbox_y          REAL,           -- normalised 0–1 (top)
    bbox_w          REAL,           -- normalised 0–1 (width)
    bbox_h          REAL,           -- normalised 0–1 (height)
    detection_conf  REAL,           -- RetinaFace confidence score 0–1
    thumbnail_path  TEXT,           -- path to saved face crop JPEG under thumbnails/
    person_id       INTEGER REFERENCES persons(id),   -- NULL until named
    cluster_id      INTEGER REFERENCES clusters(id),  -- NULL until clustered
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_faces_media    ON faces(media_file_id);
CREATE INDEX IF NOT EXISTS idx_faces_person   ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster  ON faces(cluster_id);

-- ---------------------------------------------------------------------------
-- embeddings: 512-D ArcFace vector per face
-- Embeddings are NEVER deleted — reprocessing adds new rows with new model_version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    face_id         INTEGER NOT NULL UNIQUE REFERENCES faces(id) ON DELETE CASCADE,
    vector          BLOB    NOT NULL,       -- 512 x float32, little-endian bytes
    model_version   TEXT    NOT NULL        -- e.g. 'buffalo_l_v1'
);

CREATE INDEX IF NOT EXISTS idx_embeddings_face ON embeddings(face_id);

-- ---------------------------------------------------------------------------
-- writeback_queue: tracks which files are pending metadata write
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS writeback_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    status          TEXT    NOT NULL DEFAULT 'pending',
                                            -- pending|dry_run|confirmed|written|failed
    queued_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    written_at      TEXT,
    error_msg       TEXT
);

CREATE INDEX IF NOT EXISTS idx_writeback_status ON writeback_queue(status);
CREATE INDEX IF NOT EXISTS idx_writeback_media  ON writeback_queue(media_file_id);
