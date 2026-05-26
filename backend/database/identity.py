from __future__ import annotations

import json
import uuid
from typing import Any


def _new_guid() -> str:
    return str(uuid.uuid4())


async def ensure_person_guid(db, person_id: int) -> str:
    row = await (await db.execute("SELECT person_guid FROM persons WHERE id=?", (person_id,))).fetchone()
    if not row:
        raise ValueError(f"Person not found: {person_id}")
    if row["person_guid"]:
        return str(row["person_guid"])
    person_guid = _new_guid()
    await db.execute("UPDATE persons SET person_guid=? WHERE id=?", (person_guid, person_id))
    return person_guid


async def ensure_cluster_guid(db, cluster_id: int) -> str:
    row = await (await db.execute("SELECT cluster_guid FROM clusters WHERE id=?", (cluster_id,))).fetchone()
    if not row:
        raise ValueError(f"Cluster not found: {cluster_id}")
    if row["cluster_guid"]:
        return str(row["cluster_guid"])
    cluster_guid = _new_guid()
    await db.execute("UPDATE clusters SET cluster_guid=? WHERE id=?", (cluster_guid, cluster_id))
    return cluster_guid


async def ensure_face_guid(db, face_id: int) -> str:
    row = await (await db.execute("SELECT face_guid FROM faces WHERE id=?", (face_id,))).fetchone()
    if not row:
        raise ValueError(f"Face not found: {face_id}")
    if row["face_guid"]:
        return str(row["face_guid"])
    face_guid = _new_guid()
    await db.execute("UPDATE faces SET face_guid=? WHERE id=?", (face_guid, face_id))
    return face_guid


async def append_identity_event(
    db,
    event_type: str,
    *,
    actor: str = "system",
    payload: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        "INSERT INTO identity_events(event_guid, event_type, actor, payload_json) VALUES (?, ?, ?, ?)",
        (_new_guid(), event_type, actor, json.dumps(payload or {})),
    )


async def link_cluster_to_person(
    db,
    *,
    cluster_id: int,
    person_id: int | None,
    source: str,
    actor: str = "system",
) -> None:
    cluster_guid = await ensure_cluster_guid(db, cluster_id)
    person_guid = await ensure_person_guid(db, person_id) if person_id is not None else None

    await db.execute(
        "UPDATE cluster_person_membership SET valid_to=datetime('now') WHERE cluster_guid=? AND valid_to IS NULL",
        (cluster_guid,),
    )
    await db.execute(
        "INSERT INTO cluster_person_membership(cluster_guid, person_guid, source, actor) VALUES (?, ?, ?, ?)",
        (cluster_guid, person_guid, source, actor),
    )
    await append_identity_event(
        db,
        "cluster_person_linked" if person_id is not None else "cluster_person_unlinked",
        actor=actor,
        payload={"cluster_id": cluster_id, "person_id": person_id, "source": source},
    )


async def relink_face_to_cluster(
    db,
    *,
    face_id: int,
    cluster_id: int | None,
    reason: str,
    actor: str = "system",
) -> None:
    face_guid = await ensure_face_guid(db, face_id)
    await db.execute(
        "UPDATE face_cluster_membership SET valid_to=datetime('now') WHERE face_guid=? AND valid_to IS NULL",
        (face_guid,),
    )
    if cluster_id is not None:
        cluster_guid = await ensure_cluster_guid(db, cluster_id)
        await db.execute(
            "INSERT INTO face_cluster_membership(face_guid, cluster_guid, reason, actor) VALUES (?, ?, ?, ?)",
            (face_guid, cluster_guid, reason, actor),
        )
    await append_identity_event(
        db,
        "face_cluster_relinked",
        actor=actor,
        payload={"face_id": face_id, "cluster_id": cluster_id, "reason": reason},
    )


async def get_current_cluster_guid_for_face(db, face_id: int) -> str | None:
    row = await (
        await db.execute(
            """
            SELECT m.cluster_guid
            FROM faces f
            LEFT JOIN face_cluster_membership m ON m.face_guid=f.face_guid AND m.valid_to IS NULL
            WHERE f.id=?
            """,
            (face_id,),
        )
    ).fetchone()
    return str(row["cluster_guid"]) if row and row["cluster_guid"] else None


async def get_current_cluster_id_for_face(db, face_id: int) -> int | None:
    row = await (
        await db.execute(
            """
            SELECT c.id AS cluster_id
            FROM faces f
            LEFT JOIN face_cluster_membership m ON m.face_guid=f.face_guid AND m.valid_to IS NULL
            LEFT JOIN clusters c ON c.cluster_guid=m.cluster_guid
            WHERE f.id=?
            """,
            (face_id,),
        )
    ).fetchone()
    return int(row["cluster_id"]) if row and row["cluster_id"] is not None else None


async def get_current_person_guid_for_cluster(db, cluster_id: int) -> str | None:
    row = await (
        await db.execute(
            """
            SELECT m.person_guid
            FROM clusters c
            LEFT JOIN cluster_person_membership m ON m.cluster_guid=c.cluster_guid AND m.valid_to IS NULL
            WHERE c.id=?
            """,
            (cluster_id,),
        )
    ).fetchone()
    return str(row["person_guid"]) if row and row["person_guid"] else None


async def get_current_person_id_for_cluster(db, cluster_id: int) -> int | None:
    row = await (
        await db.execute(
            """
            SELECT p.id AS person_id
            FROM clusters c
            LEFT JOIN cluster_person_membership m ON m.cluster_guid=c.cluster_guid AND m.valid_to IS NULL
            LEFT JOIN persons p ON p.person_guid=m.person_guid AND p.is_merged=0
            WHERE c.id=?
            """,
            (cluster_id,),
        )
    ).fetchone()
    return int(row["person_id"]) if row and row["person_id"] is not None else None