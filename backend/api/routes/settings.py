"""VIP API — ML settings endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import settings_store

router = APIRouter()


class SettingsUpdate(BaseModel):
    updates: dict[str, float | int | str]


@router.get("")
async def get_settings():
    """Return all tunable settings with current values and UI metadata."""
    return await settings_store.get_all()


@router.patch("")
async def patch_settings(body: SettingsUpdate):
    """Update one or more settings. Unknown keys are ignored."""
    # Validate all keys exist before writing anything
    unknown = [k for k in body.updates if k not in settings_store.DEFAULTS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting keys: {unknown}")
    await settings_store.update(body.updates)
    return {"status": "ok", "updated": list(body.updates.keys())}


@router.post("/reset")
async def reset_settings():
    """Reset all settings to their default values."""
    await settings_store.reset_all()
    return {"status": "ok", "detail": "All settings restored to defaults."}
