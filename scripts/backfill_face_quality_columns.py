#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings
from backend.face_quality import extract_quality_fields


def backfill(db_path: Path, batch_size: int) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")

    total = cur.execute(
        """
        SELECT COUNT(*) AS n
        FROM faces
        WHERE face_attributes IS NOT NULL
          AND (face_sharpness IS NULL OR pose_yaw IS NULL OR pose_pitch IS NULL OR pose_roll IS NULL)
        """
    ).fetchone()["n"]
    print(f"Backfilling {total} face rows in {db_path}")

    offset = 0
    updated = 0
    while True:
        rows = cur.execute(
            """
            SELECT id, face_attributes, face_sharpness, pose_yaw, pose_pitch, pose_roll
            FROM faces
            WHERE face_attributes IS NOT NULL
              AND (face_sharpness IS NULL OR pose_yaw IS NULL OR pose_pitch IS NULL OR pose_roll IS NULL)
            ORDER BY id
            LIMIT ? OFFSET ?
            """,
            (batch_size, offset),
        ).fetchall()
        if not rows:
            break

        updates: list[tuple[float | None, float | None, float | None, float | None, int]] = []
        for row in rows:
            parsed = extract_quality_fields(row["face_attributes"])
            face_sharpness = row["face_sharpness"] if row["face_sharpness"] is not None else parsed["face_sharpness"]
            pose_yaw = row["pose_yaw"] if row["pose_yaw"] is not None else parsed["pose_yaw"]
            pose_pitch = row["pose_pitch"] if row["pose_pitch"] is not None else parsed["pose_pitch"]
            pose_roll = row["pose_roll"] if row["pose_roll"] is not None else parsed["pose_roll"]
            updates.append((face_sharpness, pose_yaw, pose_pitch, pose_roll, int(row["id"])))

        cur.executemany(
            """
            UPDATE faces
            SET face_sharpness=?, pose_yaw=?, pose_pitch=?, pose_roll=?
            WHERE id=?
            """,
            updates,
        )
        con.commit()
        updated += len(updates)
        offset += len(rows)
        print(f"Updated {updated}/{total}")

    con.close()
    print("Backfill complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill structured face-quality columns from face_attributes JSON")
    parser.add_argument("--db", type=Path, default=settings.db_path, help="Path to vip.db")
    parser.add_argument("--batch-size", type=int, default=1000, help="Rows per commit")
    args = parser.parse_args()
    backfill(args.db, max(1, args.batch_size))


if __name__ == "__main__":
    main()