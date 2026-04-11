-- Migration 015: Person co-occurrence graph
-- Each row is an undirected edge between two named persons that appeared
-- together in at least one photo.  person_a_id < person_b_id (canonical
-- ordering) so there is exactly one row per pair.
--
-- `count`        — number of distinct photos they share
-- `last_seen_at` — date of the most recent shared photo (date_taken fallback now())
-- `last_updated` — when this row was last recomputed

CREATE TABLE IF NOT EXISTS person_cooccurrence (
    person_a_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    person_b_id  INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    count        INTEGER NOT NULL DEFAULT 1,
    last_seen_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_updated TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (person_a_id, person_b_id),
    CHECK (person_a_id < person_b_id)
);

CREATE INDEX IF NOT EXISTS idx_cooccurrence_a ON person_cooccurrence (person_a_id);
CREATE INDEX IF NOT EXISTS idx_cooccurrence_b ON person_cooccurrence (person_b_id);
