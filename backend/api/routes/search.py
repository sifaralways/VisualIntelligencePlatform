"""VIP API — Search routes."""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from backend.database.db import get_db

router = APIRouter()


class SearchRequest(BaseModel):
    query: str = ""
    person_ids: Optional[list[int]] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    camera_make: Optional[str] = None
    limit: int = 50
    offset: int = 0


@router.post("")
async def search(req: SearchRequest):
    """
    Search media files. All filters are optional and combinable.
    Operates entirely on the local SQLite DB — works even when files
    are offloaded to iCloud.
    """
    conditions = []
    params: list = []

    if req.query:
        # Simple keyword search across path, camera model, date
        like = f"%{req.query}%"
        conditions.append("""
            (mf.file_path LIKE ?
             OR mf.camera_model LIKE ?
             OR mf.date_taken LIKE ?
             OR p.name LIKE ?)
        """)
        params.extend([like, like, like, like])

    if req.person_ids:
        placeholders = ",".join("?" * len(req.person_ids))
        conditions.append(f"p.id IN ({placeholders})")
        params.extend(req.person_ids)

    if req.date_from:
        conditions.append("mf.date_taken >= ?")
        params.append(req.date_from)

    if req.date_to:
        conditions.append("mf.date_taken <= ?")
        params.append(req.date_to)

    if req.camera_make:
        conditions.append("mf.camera_make LIKE ?")
        params.append(f"%{req.camera_make}%")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([req.limit, req.offset])

    query_sql = f"""
        SELECT DISTINCT mf.id, mf.file_path, mf.date_taken, mf.camera_model,
               mf.width, mf.height, mf.ingest_state,
               GROUP_CONCAT(DISTINCT p.name) as persons
        FROM media_files mf
        LEFT JOIN faces f ON f.media_file_id = mf.id
        LEFT JOIN persons p ON p.id = f.person_id AND p.is_merged = 0
        {where}
        GROUP BY mf.id
        ORDER BY mf.date_taken DESC
        LIMIT ? OFFSET ?
    """

    async with get_db() as db:
        rows = await db.execute_fetchall(query_sql, params)

    return {
        "results": [dict(r) for r in rows],
        "count": len(rows),
        "limit": req.limit,
        "offset": req.offset,
    }
