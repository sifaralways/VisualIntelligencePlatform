ALTER TABLE faces ADD COLUMN face_sharpness REAL;
ALTER TABLE faces ADD COLUMN pose_yaw REAL;
ALTER TABLE faces ADD COLUMN pose_pitch REAL;
ALTER TABLE faces ADD COLUMN pose_roll REAL;

CREATE INDEX IF NOT EXISTS idx_faces_face_sharpness ON faces(face_sharpness);
CREATE INDEX IF NOT EXISTS idx_faces_pose_yaw ON faces(pose_yaw);