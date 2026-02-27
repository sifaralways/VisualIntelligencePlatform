"""VIP API — Tags routes."""

from fastapi import APIRouter, HTTPException
from backend.database.db import get_db

router = APIRouter()


@router.get("/{media_file_id}")
async def get_tags(media_file_id: int):
    """
    All ML-generated tags for a media file, grouped by category.

    Returns:
        {
          "person":    ["Alice", "Bob"],
          "object":    ["Car", "Laptop"],
          "animal":    ["Golden Retriever"],
          "geography": ["Beach", "Ocean"],
          "place":     ["Sydney Opera House, Sydney, Australia"]
        }
    """
    async with get_db() as db:
        # Check file exists
        exists = await (
            await db.execute("SELECT id FROM media_files WHERE id=?", (media_file_id,))
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Media file not found")

        rows = await db.execute_fetchall("""
            SELECT category, label, confidence, model
            FROM media_tags
            WHERE media_file_id = ?
            ORDER BY category, confidence DESC
        """, (media_file_id,))

    grouped: dict[str, list[str]] = {}
    for row in rows:
        cat = row["category"]
        grouped.setdefault(cat, []).append(row["label"])

    return grouped


@router.get("/summary/top")
async def get_top_tags(category: str | None = None, limit: int = 20):
    """Most frequent tags across all media files, optionally filtered by category."""
    async with get_db() as db:
        if category:
            rows = await db.execute_fetchall("""
                SELECT label, COUNT(*) as count
                FROM media_tags
                WHERE category = ?
                GROUP BY label
                ORDER BY count DESC
                LIMIT ?
            """, (category, limit))
        else:
            rows = await db.execute_fetchall("""
                SELECT category, label, COUNT(*) as count
                FROM media_tags
                GROUP BY category, label
                ORDER BY count DESC
                LIMIT ?
            """, (limit,))

    return [dict(r) for r in rows]
