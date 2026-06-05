-- Migration 022: Canonical identity graph (GUID entities + temporal memberships)
--
-- Goals:
-- 1) Immutable GUID identity for faces, clusters, persons.
-- 2) Canonical parent-child relationships via temporal membership tables.
-- 3) Backward-compatible with existing id/person_id/cluster_id writes.
--
-- Notes:
-- - Existing columns remain for compatibility.
-- - Triggers mirror legacy writes into membership history.
-- - New code can query current state via *_current views.

-- ---------------------------------------------------------------------------
-- 1) Add immutable GUID identity columns to existing entity tables
-- ---------------------------------------------------------------------------
ALTER TABLE faces ADD COLUMN face_guid TEXT;
ALTER TABLE clusters ADD COLUMN cluster_guid TEXT;
ALTER TABLE persons ADD COLUMN person_guid TEXT;

-- Optional lifecycle/state columns for future use
ALTER TABLE clusters ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE clusters ADD COLUMN retired_at TEXT;
ALTER TABLE persons ADD COLUMN status TEXT NOT NULL DEFAULT 'active';

-- Backfill GUIDs for all existing rows
UPDATE faces
SET face_guid = (
    lower(hex(randomblob(4))) || '-' ||
    lower(hex(randomblob(2))) || '-' ||
    '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
    substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
    lower(hex(randomblob(6)))
)
WHERE face_guid IS NULL;

UPDATE clusters
SET cluster_guid = (
    lower(hex(randomblob(4))) || '-' ||
    lower(hex(randomblob(2))) || '-' ||
    '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
    substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
    lower(hex(randomblob(6)))
)
WHERE cluster_guid IS NULL;

UPDATE persons
SET person_guid = (
    lower(hex(randomblob(4))) || '-' ||
    lower(hex(randomblob(2))) || '-' ||
    '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
    substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
    lower(hex(randomblob(6)))
)
WHERE person_guid IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_faces_face_guid_unique ON faces(face_guid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clusters_cluster_guid_unique ON clusters(cluster_guid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_person_guid_unique ON persons(person_guid);

-- ---------------------------------------------------------------------------
-- 2) Canonical membership tables (temporal)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS face_cluster_membership (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    face_guid       TEXT NOT NULL,
    cluster_guid    TEXT NOT NULL,
    valid_from      TEXT NOT NULL DEFAULT (datetime('now')),
    valid_to        TEXT,
    reason          TEXT NOT NULL DEFAULT 'unknown',
    actor           TEXT,
    FOREIGN KEY(face_guid) REFERENCES faces(face_guid) ON DELETE CASCADE,
    FOREIGN KEY(cluster_guid) REFERENCES clusters(cluster_guid) ON DELETE CASCADE,
    CHECK(valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS cluster_person_membership (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_guid    TEXT NOT NULL,
    person_guid     TEXT,
    valid_from      TEXT NOT NULL DEFAULT (datetime('now')),
    valid_to        TEXT,
    source          TEXT NOT NULL DEFAULT 'unknown',
    actor           TEXT,
    FOREIGN KEY(cluster_guid) REFERENCES clusters(cluster_guid) ON DELETE CASCADE,
    FOREIGN KEY(person_guid) REFERENCES persons(person_guid) ON DELETE SET NULL,
    CHECK(valid_to IS NULL OR valid_to >= valid_from)
);

CREATE INDEX IF NOT EXISTS idx_fcm_face_active ON face_cluster_membership(face_guid, valid_to);
CREATE INDEX IF NOT EXISTS idx_fcm_cluster_active ON face_cluster_membership(cluster_guid, valid_to);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fcm_one_active_per_face
ON face_cluster_membership(face_guid)
WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_cpm_cluster_active ON cluster_person_membership(cluster_guid, valid_to);
CREATE INDEX IF NOT EXISTS idx_cpm_person_active ON cluster_person_membership(person_guid, valid_to);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cpm_one_active_per_cluster
ON cluster_person_membership(cluster_guid)
WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 3) Optional identity metadata tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS person_aliases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_guid     TEXT NOT NULL,
    alias_name      TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(person_guid) REFERENCES persons(person_guid) ON DELETE CASCADE,
    UNIQUE(person_guid, alias_name)
);

CREATE TABLE IF NOT EXISTS identity_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_guid      TEXT NOT NULL UNIQUE,
    event_type      TEXT NOT NULL,
    actor           TEXT,
    payload_json    TEXT,
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_identity_events_type_time ON identity_events(event_type, occurred_at);

-- ---------------------------------------------------------------------------
-- 4) Bootstrap memberships from legacy current state
-- ---------------------------------------------------------------------------
INSERT INTO face_cluster_membership (face_guid, cluster_guid, reason, actor)
SELECT f.face_guid, c.cluster_guid, 'bootstrap', 'migration_022'
FROM faces f
JOIN clusters c ON c.id = f.cluster_id
LEFT JOIN face_cluster_membership m
       ON m.face_guid = f.face_guid AND m.valid_to IS NULL
WHERE f.cluster_id IS NOT NULL
  AND m.id IS NULL;

-- One active cluster->person row per cluster; person_guid is nullable for unnamed clusters.
INSERT INTO cluster_person_membership (cluster_guid, person_guid, source, actor)
SELECT c.cluster_guid, p.person_guid, 'bootstrap', 'migration_022'
FROM clusters c
LEFT JOIN persons p ON p.id = c.person_id
LEFT JOIN cluster_person_membership m
       ON m.cluster_guid = c.cluster_guid AND m.valid_to IS NULL
WHERE m.id IS NULL;

-- ---------------------------------------------------------------------------
-- 5) Compatibility views for "current" relationship state
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_face_cluster_current AS
SELECT face_guid, cluster_guid, valid_from
FROM face_cluster_membership
WHERE valid_to IS NULL;

CREATE VIEW IF NOT EXISTS v_cluster_person_current AS
SELECT cluster_guid, person_guid, source, valid_from
FROM cluster_person_membership
WHERE valid_to IS NULL;

-- Refresh older derived views so fresh databases read current ownership from
-- the canonical graph instead of legacy faces.person_id / clusters.person_id.
DROP VIEW IF EXISTS v_person_photos;
CREATE VIEW v_person_photos AS
SELECT
        p.id         AS person_id,
        p.name       AS person_name,
        mf.id        AS media_id,
        mf.file_path,
        mf.date_taken,
        mf.camera_make,
        mf.camera_model,
        mf.gps_lat,
        mf.gps_lon
FROM persons p
JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
JOIN faces f ON f.cluster_id = c.id
JOIN media_files mf ON mf.id = f.media_file_id
WHERE p.is_ignored  = 0
    AND p.is_merged   = 0
    AND mf.removed_from_app = 0
    AND mf.is_stub    = 0;

DROP VIEW IF EXISTS v_photo_persons_agg;
CREATE VIEW v_photo_persons_agg AS
SELECT
        mf.id        AS media_id,
        mf.file_path,
        mf.date_taken,
        mf.camera_make,
        mf.camera_model,
        mf.gps_lat,
        mf.gps_lon,
        COUNT(DISTINCT p.id)           AS person_count,
        GROUP_CONCAT(DISTINCT p.name)  AS person_names
FROM media_files mf
LEFT JOIN faces f ON f.media_file_id = mf.id
LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
LEFT JOIN persons p ON p.person_guid = cpc.person_guid
        AND p.is_ignored = 0
        AND p.is_merged  = 0
WHERE mf.removed_from_app = 0
    AND mf.is_stub = 0
GROUP BY mf.id;

DROP VIEW IF EXISTS v_person_photo_count;
CREATE VIEW v_person_photo_count AS
SELECT
        p.id         AS person_id,
        p.name,
        COUNT(DISTINCT f.media_file_id) AS photo_count
FROM persons p
JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
JOIN clusters c ON c.cluster_guid = cpc.cluster_guid
JOIN faces f ON f.cluster_id = c.id
JOIN media_files mf ON mf.id = f.media_file_id
                                     AND mf.removed_from_app = 0
                                     AND mf.is_stub = 0
WHERE p.is_ignored = 0
    AND p.is_merged  = 0
GROUP BY p.id;

DROP VIEW IF EXISTS v_unidentified_faces;
CREATE VIEW v_unidentified_faces AS
SELECT
        mf.id        AS media_id,
        mf.file_path,
        mf.date_taken,
        COUNT(f.id)  AS unidentified_face_count
FROM media_files mf
JOIN faces f ON f.media_file_id = mf.id
LEFT JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
LEFT JOIN persons p ON p.person_guid = cpc.person_guid AND p.is_merged = 0 AND p.is_ignored = 0
WHERE mf.removed_from_app = 0
    AND mf.is_stub = 0
    AND p.id IS NULL
GROUP BY mf.id;

DROP VIEW IF EXISTS v_photo_full_context;
CREATE VIEW v_photo_full_context AS
SELECT
        mf.id            AS media_id,
        mf.file_path,
        mf.date_taken,
        mf.camera_make,
        mf.camera_model,
        mf.gps_lat,
        mf.gps_lon,
        mf.width,
        mf.height,
        (
                SELECT GROUP_CONCAT(DISTINCT p.name)
                FROM faces f
                JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
                JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
                JOIN persons p ON p.person_guid = cpc.person_guid
                WHERE f.media_file_id = mf.id
                    AND p.is_ignored = 0
                    AND p.is_merged  = 0
        ) AS persons,
        (
                SELECT GROUP_CONCAT(DISTINCT mt.label)
                FROM media_tags mt
                WHERE mt.media_file_id = mf.id
                    AND mt.category = 'object'
        ) AS objects,
        (
                SELECT GROUP_CONCAT(DISTINCT mt.label)
                FROM media_tags mt
                WHERE mt.media_file_id = mf.id
                    AND mt.category IN ('place', 'geography')
        ) AS places,
        (
                SELECT GROUP_CONCAT(DISTINCT mt.label)
                FROM media_tags mt
                WHERE mt.media_file_id = mf.id
                    AND mt.category = 'animal'
        ) AS animals,
        (
                SELECT GROUP_CONCAT(DISTINCT mt.label)
                FROM media_tags mt
                WHERE mt.media_file_id = mf.id
                    AND mt.category = 'scene'
        ) AS scenes
FROM media_files mf
WHERE mf.removed_from_app = 0
    AND mf.is_stub = 0;

-- ---------------------------------------------------------------------------
-- 6) Triggers to keep canonical memberships synced with legacy writes
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_faces_set_face_guid_ai;
CREATE TRIGGER trg_faces_set_face_guid_ai
AFTER INSERT ON faces
FOR EACH ROW
WHEN NEW.face_guid IS NULL
BEGIN
    UPDATE faces
    SET face_guid = (
        lower(hex(randomblob(4))) || '-' ||
        lower(hex(randomblob(2))) || '-' ||
        '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
        substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
        lower(hex(randomblob(6)))
    )
    WHERE id = NEW.id
      AND face_guid IS NULL;
END;

DROP TRIGGER IF EXISTS trg_clusters_set_cluster_guid_ai;
CREATE TRIGGER trg_clusters_set_cluster_guid_ai
AFTER INSERT ON clusters
FOR EACH ROW
WHEN NEW.cluster_guid IS NULL
BEGIN
    UPDATE clusters
    SET cluster_guid = (
        lower(hex(randomblob(4))) || '-' ||
        lower(hex(randomblob(2))) || '-' ||
        '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
        substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
        lower(hex(randomblob(6)))
    )
    WHERE id = NEW.id
      AND cluster_guid IS NULL;
END;

DROP TRIGGER IF EXISTS trg_persons_set_person_guid_ai;
CREATE TRIGGER trg_persons_set_person_guid_ai
AFTER INSERT ON persons
FOR EACH ROW
WHEN NEW.person_guid IS NULL
BEGIN
    UPDATE persons
    SET person_guid = (
        lower(hex(randomblob(4))) || '-' ||
        lower(hex(randomblob(2))) || '-' ||
        '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
        substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
        lower(hex(randomblob(6)))
    )
    WHERE id = NEW.id
      AND person_guid IS NULL;
END;

DROP TRIGGER IF EXISTS trg_faces_cluster_membership_ai;
CREATE TRIGGER trg_faces_cluster_membership_ai
AFTER INSERT ON faces
FOR EACH ROW
WHEN NEW.cluster_id IS NOT NULL
BEGIN
    INSERT OR IGNORE INTO face_cluster_membership(face_guid, cluster_guid, reason, actor)
    SELECT f.face_guid, c.cluster_guid, 'face_insert', 'legacy_sync'
    FROM faces f
    JOIN clusters c ON c.id = NEW.cluster_id
    WHERE f.id = NEW.id;

    INSERT INTO identity_events(event_guid, event_type, actor, payload_json)
    VALUES (
        (
            lower(hex(randomblob(4))) || '-' ||
            lower(hex(randomblob(2))) || '-' ||
            '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
            substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
            lower(hex(randomblob(6)))
        ),
        'face_cluster_linked',
        'legacy_sync',
        json_object('face_id', NEW.id, 'cluster_id', NEW.cluster_id, 'reason', 'insert')
    );
END;

DROP TRIGGER IF EXISTS trg_faces_cluster_membership_au;
CREATE TRIGGER trg_faces_cluster_membership_au
AFTER UPDATE OF cluster_id ON faces
FOR EACH ROW
WHEN COALESCE(OLD.cluster_id, -1) <> COALESCE(NEW.cluster_id, -1)
BEGIN
    UPDATE face_cluster_membership
    SET valid_to = datetime('now')
    WHERE face_guid = NEW.face_guid
      AND valid_to IS NULL;

    INSERT INTO face_cluster_membership(face_guid, cluster_guid, reason, actor)
    SELECT NEW.face_guid, c.cluster_guid, 'recluster', 'legacy_sync'
    FROM clusters c
    WHERE c.id = NEW.cluster_id;

    INSERT INTO identity_events(event_guid, event_type, actor, payload_json)
    VALUES (
        (
            lower(hex(randomblob(4))) || '-' ||
            lower(hex(randomblob(2))) || '-' ||
            '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
            substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
            lower(hex(randomblob(6)))
        ),
        'face_cluster_relinked',
        'legacy_sync',
        json_object('face_id', NEW.id, 'old_cluster_id', OLD.cluster_id, 'new_cluster_id', NEW.cluster_id)
    );
END;

DROP TRIGGER IF EXISTS trg_clusters_person_membership_ai;
CREATE TRIGGER trg_clusters_person_membership_ai
AFTER INSERT ON clusters
FOR EACH ROW
BEGIN
    INSERT OR IGNORE INTO cluster_person_membership(cluster_guid, person_guid, source, actor)
    SELECT c.cluster_guid, p.person_guid, 'cluster_insert', 'legacy_sync'
    FROM clusters c
    LEFT JOIN persons p ON p.id = NEW.person_id
    WHERE c.id = NEW.id;
END;

DROP TRIGGER IF EXISTS trg_clusters_person_membership_au;
CREATE TRIGGER trg_clusters_person_membership_au
AFTER UPDATE OF person_id ON clusters
FOR EACH ROW
WHEN COALESCE(OLD.person_id, -1) <> COALESCE(NEW.person_id, -1)
BEGIN
    UPDATE cluster_person_membership
    SET valid_to = datetime('now')
    WHERE cluster_guid = NEW.cluster_guid
      AND valid_to IS NULL;

    INSERT INTO cluster_person_membership(cluster_guid, person_guid, source, actor)
    SELECT NEW.cluster_guid, p.person_guid, 'legacy_update', 'legacy_sync'
    FROM persons p
    WHERE p.id = NEW.person_id;

    -- New person_id can be NULL (unnamed cluster) - keep an explicit active row.
    INSERT INTO cluster_person_membership(cluster_guid, person_guid, source, actor)
    SELECT NEW.cluster_guid, NULL, 'legacy_update', 'legacy_sync'
    WHERE NEW.person_id IS NULL;

    INSERT INTO identity_events(event_guid, event_type, actor, payload_json)
    VALUES (
        (
            lower(hex(randomblob(4))) || '-' ||
            lower(hex(randomblob(2))) || '-' ||
            '4' || substr(lower(hex(randomblob(2))), 2) || '-' ||
            substr('89ab', 1 + (abs(random()) % 4), 1) || substr(lower(hex(randomblob(2))), 2) || '-' ||
            lower(hex(randomblob(6)))
        ),
        'cluster_person_relinked',
        'legacy_sync',
        json_object('cluster_id', NEW.id, 'old_person_id', OLD.person_id, 'new_person_id', NEW.person_id)
    );
END;

DROP TRIGGER IF EXISTS trg_persons_alias_on_name_change_au;
CREATE TRIGGER trg_persons_alias_on_name_change_au
AFTER UPDATE OF name ON persons
FOR EACH ROW
WHEN OLD.name IS NOT NULL
 AND NEW.name IS NOT NULL
 AND OLD.name <> NEW.name
BEGIN
    INSERT OR IGNORE INTO person_aliases(person_guid, alias_name, confidence, source)
    VALUES (NEW.person_guid, OLD.name, 1.0, 'rename_history');
END;
