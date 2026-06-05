-- =============================================================================
-- Migration 024 — Person cannot-link memory
-- =============================================================================

CREATE TABLE IF NOT EXISTS person_cannot_link (
    person_a_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    person_b_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    reason      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (person_a_id < person_b_id),
    UNIQUE (person_a_id, person_b_id)
);

CREATE INDEX IF NOT EXISTS idx_person_cannot_link_a ON person_cannot_link(person_a_id);
CREATE INDEX IF NOT EXISTS idx_person_cannot_link_b ON person_cannot_link(person_b_id);
