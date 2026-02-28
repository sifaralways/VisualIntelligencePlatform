-- 005_merge_signals.sql
-- Tracks user decisions about proactive merge suggestions.
--
-- When the proactive-merge UI suggests "is cluster X the same person as Y?"
-- and the user clicks "No / Different person", that (person_id, cluster_id)
-- pair is recorded here so it is never re-proposed.
--
-- Accepted merges are captured implicitly by the cluster being assigned to
-- a person in the clusters / faces tables — no extra row is needed.

CREATE TABLE IF NOT EXISTS rejected_suggestions (
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    cluster_id  INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    rejected_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (person_id, cluster_id)
);
