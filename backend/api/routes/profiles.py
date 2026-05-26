from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database.db import init_db
from backend.database.settings_store import load_cache
from backend.profiles import (
    copy_admin_settings,
    create_profile,
    delete_profile,
    get_active_profile,
    get_profile,
    list_profiles,
    rename_profile,
    run_in_profile,
    set_profile_password,
    select_profile,
)

router = APIRouter()


class CreateProfileRequest(BaseModel):
    name: str
    copy_settings_from_profile_id: str | None = None
    password: str | None = None


class SelectProfileRequest(BaseModel):
    password: str | None = None


class RenameProfileRequest(BaseModel):
    name: str


class SetProfilePasswordRequest(BaseModel):
    password: str | None = None
    current_password: str | None = None


def _serialize(profile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "is_password_protected": profile.is_password_protected,
        "is_default": profile.is_default,
        "is_active": profile.is_active,
        "created_at": profile.created_at,
        "last_opened_at": profile.last_opened_at,
    }


@router.get("")
async def list_all_profiles():
    return [_serialize(profile) for profile in list_profiles()]


@router.get("/active")
async def get_active():
    return _serialize(get_active_profile())


@router.post("")
async def create_new_profile(req: CreateProfileRequest):
    if req.copy_settings_from_profile_id and get_profile(req.copy_settings_from_profile_id) is None:
        raise HTTPException(status_code=404, detail="Source profile not found")

    try:
        profile = create_profile(req.name, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await run_in_profile(profile.id, init_db)
    if req.copy_settings_from_profile_id:
        copy_admin_settings(req.copy_settings_from_profile_id, profile.id)
    await run_in_profile(profile.id, load_cache)
    return _serialize(profile)


@router.post("/{profile_id}/select")
async def select(profile_id: str, req: SelectProfileRequest | None = None):
    if get_profile(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    try:
        profile = select_profile(profile_id, req.password if req else None)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    await run_in_profile(profile.id, init_db)
    await run_in_profile(profile.id, load_cache)
    return _serialize(profile)


@router.patch("/{profile_id}")
async def rename(profile_id: str, req: RenameProfileRequest):
    try:
        profile = rename_profile(profile_id, req.name)
    except KeyError:
        raise HTTPException(status_code=404, detail="Profile not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(profile)


@router.delete("/{profile_id}")
async def delete(profile_id: str):
    try:
        deleted = delete_profile(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Profile not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "deleted",
        "id": deleted.id,
        "name": deleted.name,
    }


@router.post("/{profile_id}/password")
async def set_password(profile_id: str, req: SetProfilePasswordRequest):
    try:
        profile = set_profile_password(profile_id, req.password, req.current_password)
    except KeyError:
        raise HTTPException(status_code=404, detail="Profile not found")
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize(profile)