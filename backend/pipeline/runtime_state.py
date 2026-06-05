from __future__ import annotations

from typing import Any

from backend.database.db import get_db


async def begin_run(run_kind: str, folder: str | None = None, use_existing_vip_data: bool | None = None) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO pipeline_runtime_state
                (id, status, run_kind, folder, use_existing_vip_data, last_phase, resumable, error, updated_at)
            VALUES (1, 'running', ?, ?, ?, 'init', 0, NULL, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                run_kind=excluded.run_kind,
                folder=excluded.folder,
                use_existing_vip_data=excluded.use_existing_vip_data,
                last_phase=excluded.last_phase,
                resumable=0,
                error=NULL,
                updated_at=excluded.updated_at
            """,
            (run_kind, folder, None if use_existing_vip_data is None else int(bool(use_existing_vip_data))),
        )


async def set_phase(phase: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE pipeline_runtime_state SET last_phase=?, updated_at=datetime('now') WHERE id=1",
            (phase,),
        )


async def set_status(status: str, *, error: str | None = None, resumable: bool | None = None) -> None:
    async with get_db() as db:
        if resumable is None:
            await db.execute(
                "UPDATE pipeline_runtime_state SET status=?, error=?, updated_at=datetime('now') WHERE id=1",
                (status, error),
            )
        else:
            await db.execute(
                """
                UPDATE pipeline_runtime_state
                SET status=?, error=?, resumable=?, updated_at=datetime('now')
                WHERE id=1
                """,
                (status, error, int(bool(resumable))),
            )


async def mark_paused() -> None:
    await set_status("paused")


async def mark_running() -> None:
    await set_status("running")


async def mark_stopping() -> None:
    await set_status("stopping")


async def mark_interrupted_resumable() -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE pipeline_runtime_state
            SET status='stopped', resumable=1, error=NULL, updated_at=datetime('now')
            WHERE id=1
            """
        )


async def mark_stopped_resumable() -> None:
    await set_status("stopped", resumable=True)


async def mark_succeeded_idle() -> None:
    async with get_db() as db:
        await db.execute(
            """
            UPDATE pipeline_runtime_state
            SET status='idle', resumable=0, error=NULL, run_kind=NULL,
                folder=NULL, use_existing_vip_data=NULL, last_phase=NULL,
                updated_at=datetime('now')
            WHERE id=1
            """
        )


async def mark_error(error: str) -> None:
    await set_status("error", error=error, resumable=False)


async def get_runtime_state() -> dict[str, Any]:
    async with get_db() as db:
        row = await (await db.execute(
            """
            SELECT status, run_kind, folder, use_existing_vip_data, last_phase, resumable, error
            FROM pipeline_runtime_state WHERE id=1
            """
        )).fetchone()

    if not row:
        return {
            "status": "idle",
            "run_kind": None,
            "folder": None,
            "use_existing_vip_data": None,
            "last_phase": None,
            "resumable": False,
            "error": None,
        }

    return {
        "status": row["status"],
        "run_kind": row["run_kind"],
        "folder": row["folder"],
        "use_existing_vip_data": (
            None if row["use_existing_vip_data"] is None else bool(row["use_existing_vip_data"])
        ),
        "last_phase": row["last_phase"],
        "resumable": bool(row["resumable"]),
        "error": row["error"],
    }
