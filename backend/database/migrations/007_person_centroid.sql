-- 007_person_centroid.sql
-- Persist a normalised face-embedding centroid directly on each named person.
--
-- centroid:   Raw float32 bytes (512-dim, normalised unit vector).
--             NULL = person has no faces with embeddings yet (or not yet
--             computed — will be filled on next pipeline run / name assignment).
-- centroid_n: Number of embedding vectors averaged to produce the centroid.
--             Used to know whether a centroid is populated.
--
-- Why: Without a stored centroid, auto-name matching requires loading ALL
-- embeddings for every named person on every pipeline run. More importantly,
-- when all photos of a person are removed from the library, their embeddings
-- are cascade-deleted with the faces — making re-identification of that person
-- in future scans impossible. A stored centroid survives photo removal.

ALTER TABLE persons ADD COLUMN centroid   BLOB    DEFAULT NULL;
ALTER TABLE persons ADD COLUMN centroid_n INTEGER DEFAULT 0;
