CREATE TABLE IF NOT EXISTS person_centroid_faces (
    person_id        INTEGER NOT NULL,
    face_id          INTEGER NOT NULL,
    quality_score    REAL NOT NULL,
    selected_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (person_id, face_id),
    FOREIGN KEY(person_id) REFERENCES persons(id) ON DELETE CASCADE,
    FOREIGN KEY(face_id) REFERENCES faces(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_person_centroid_faces_person_quality
ON person_centroid_faces(person_id, quality_score DESC, face_id DESC);