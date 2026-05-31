CREATE TABLE IF NOT EXISTS person_suggestion_queue (
    id                      INTEGER PRIMARY KEY,
    person_id               INTEGER NOT NULL,
    cluster_id              INTEGER NOT NULL,
    similarity              REAL NOT NULL,
    competing_person_id     INTEGER,
    competing_similarity    REAL,
    margin                  REAL,
    cluster_member_count    INTEGER,
    cluster_thumbnail_path  TEXT,
    source                  TEXT NOT NULL DEFAULT 'quality_background',
    status                  TEXT NOT NULL DEFAULT 'pending',
    generated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at             TEXT,
    metadata_json           TEXT,
    FOREIGN KEY(person_id) REFERENCES persons(id) ON DELETE CASCADE,
    FOREIGN KEY(cluster_id) REFERENCES clusters(id) ON DELETE CASCADE,
    FOREIGN KEY(competing_person_id) REFERENCES persons(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_psq_person_status
ON person_suggestion_queue(person_id, status, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_psq_status_generated
ON person_suggestion_queue(status, generated_at DESC);

CREATE INDEX IF NOT EXISTS idx_psq_cluster_status
ON person_suggestion_queue(cluster_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS uq_psq_pending_person_cluster
ON person_suggestion_queue(person_id, cluster_id, source)
WHERE status = 'pending';
