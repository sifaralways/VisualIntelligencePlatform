from __future__ import annotations

import contextvars
import inspect
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "VIP"
PROFILES_ROOT = APP_SUPPORT_ROOT / "profiles"
REGISTRY_PATH = APP_SUPPORT_ROOT / "profiles.sqlite3"
DEFAULT_PROFILE_NAME = "SifarAlways-1"

_LEGACY_FILES = [
    "vip.db",
    "vip.faiss",
    "vip.faiss.ids.pkl",
    "vip_clip.faiss",
    "vip_clip.faiss.ids.pkl",
    "vip_clip.faiss.meta.pkl",
]
_LEGACY_DIRS = ["thumbnails", "photo_thumbs", "previews"]

_current_profile_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vip_profile_id", default=None
)
_profiles_cache: dict[str, "ProfileRecord"] = {}
_active_profile_id: str | None = None

T = TypeVar("T")


@dataclass(frozen=True)
class ProfileRecord:
    id: str
    name: str
    data_dir: Path
    created_at: str
    last_opened_at: str | None
    is_default: bool = False
    is_active: bool = False


def _connect_registry() -> sqlite3.Connection:
    APP_SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(REGISTRY_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL UNIQUE,
            data_dir        TEXT NOT NULL UNIQUE,
            is_default      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            last_opened_at  TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-._")
    slug = slug.lower()
    return slug or "profile"


def _load_cache(conn: sqlite3.Connection) -> None:
    global _profiles_cache, _active_profile_id

    active_row = conn.execute(
        "SELECT value FROM app_state WHERE key='active_profile_id'"
    ).fetchone()
    active_id = str(active_row["value"]) if active_row and active_row["value"] else None

    rows = conn.execute(
        "SELECT id, name, data_dir, created_at, last_opened_at, is_default FROM profiles ORDER BY created_at, name"
    ).fetchall()
    cache: dict[str, ProfileRecord] = {}
    for row in rows:
        profile_id = str(row["id"])
        cache[profile_id] = ProfileRecord(
            id=profile_id,
            name=str(row["name"]),
            data_dir=Path(str(row["data_dir"])),
            created_at=str(row["created_at"]),
            last_opened_at=str(row["last_opened_at"]) if row["last_opened_at"] else None,
            is_default=bool(row["is_default"]),
            is_active=(profile_id == active_id),
        )

    _profiles_cache = cache
    _active_profile_id = active_id


def _profile_is_empty(data_dir: Path) -> bool:
    if not data_dir.exists():
        return True
    return not any(data_dir.iterdir())


def _legacy_state_exists() -> bool:
    for name in _LEGACY_FILES:
        if (APP_SUPPORT_ROOT / name).exists():
            return True
    for name in _LEGACY_DIRS:
        if (APP_SUPPORT_ROOT / name).exists():
            return True
    return False


def _move_path(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _migrate_legacy_state(default_profile: ProfileRecord) -> None:
    if not _legacy_state_exists():
        return
    default_profile.data_dir.mkdir(parents=True, exist_ok=True)
    if not _profile_is_empty(default_profile.data_dir):
        logger.info(
            "Legacy VIP state exists but profile %s already has data; leaving legacy files in place",
            default_profile.id,
        )
        return

    for name in _LEGACY_FILES:
        _move_path(APP_SUPPORT_ROOT / name, default_profile.data_dir / name)
    for name in _LEGACY_DIRS:
        _move_path(APP_SUPPORT_ROOT / name, default_profile.data_dir / name)

    logger.info(
        "Migrated legacy single-profile VIP data into default profile '%s'",
        default_profile.name,
    )


def _set_active_profile(conn: sqlite3.Connection, profile_id: str) -> None:
    conn.execute(
        """
        INSERT INTO app_state (key, value, updated_at) VALUES ('active_profile_id', ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (profile_id,),
    )
    conn.execute(
        "UPDATE profiles SET last_opened_at=datetime('now') WHERE id=?",
        (profile_id,),
    )
    conn.commit()


def _next_profile_id(conn: sqlite3.Connection, name: str) -> str:
    base = _slugify(name)
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM profiles WHERE id=?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def bootstrap_profiles() -> ProfileRecord:
    with _connect_registry() as conn:
        _ensure_schema(conn)
        count_row = conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()
        count = int(count_row["n"]) if count_row else 0

        if count == 0:
            profile_id = _slugify(DEFAULT_PROFILE_NAME)
            data_dir = PROFILES_ROOT / profile_id
            data_dir.mkdir(parents=True, exist_ok=True)
            conn.execute(
                "INSERT INTO profiles (id, name, data_dir, is_default, last_opened_at) VALUES (?, ?, ?, 1, datetime('now'))",
                (profile_id, DEFAULT_PROFILE_NAME, str(data_dir)),
            )
            _set_active_profile(conn, profile_id)

        _load_cache(conn)
        default_profile = next(
            (profile for profile in _profiles_cache.values() if profile.is_default),
            None,
        )
        if default_profile is None:
            first = next(iter(_profiles_cache.values()))
            conn.execute("UPDATE profiles SET is_default=1 WHERE id=?", (first.id,))
            conn.commit()
            _load_cache(conn)
            default_profile = _profiles_cache[first.id]

        if _legacy_state_exists():
            _migrate_legacy_state(default_profile)

        if _active_profile_id is None or _active_profile_id not in _profiles_cache:
            _set_active_profile(conn, default_profile.id)
            _load_cache(conn)

        active_profile = _profiles_cache[_active_profile_id]
        active_profile.data_dir.mkdir(parents=True, exist_ok=True)
        return active_profile


def list_profiles() -> list[ProfileRecord]:
    if not _profiles_cache:
        bootstrap_profiles()
    return list(_profiles_cache.values())


def get_profile(profile_id: str) -> ProfileRecord | None:
    if not _profiles_cache:
        bootstrap_profiles()
    return _profiles_cache.get(profile_id)


def get_active_profile() -> ProfileRecord:
    if not _profiles_cache or _active_profile_id not in _profiles_cache:
        return bootstrap_profiles()
    return _profiles_cache[_active_profile_id]


def select_profile(profile_id: str) -> ProfileRecord:
    with _connect_registry() as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        _set_active_profile(conn, profile_id)
        _load_cache(conn)
    profile = _profiles_cache[profile_id]
    profile.data_dir.mkdir(parents=True, exist_ok=True)
    return profile


def create_profile(name: str) -> ProfileRecord:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Profile name is required")

    with _connect_registry() as conn:
        _ensure_schema(conn)
        existing = conn.execute(
            "SELECT id FROM profiles WHERE lower(name)=lower(?)",
            (clean_name,),
        ).fetchone()
        if existing:
            raise ValueError("A profile with that name already exists")

        profile_id = _next_profile_id(conn, clean_name)
        data_dir = PROFILES_ROOT / profile_id
        data_dir.mkdir(parents=True, exist_ok=True)
        conn.execute(
            "INSERT INTO profiles (id, name, data_dir, is_default) VALUES (?, ?, ?, 0)",
            (profile_id, clean_name, str(data_dir)),
        )
        conn.commit()
        _load_cache(conn)
    return _profiles_cache[profile_id]


def rename_profile(profile_id: str, new_name: str) -> ProfileRecord:
    clean_name = (new_name or "").strip()
    if not clean_name:
        raise ValueError("Profile name is required")

    with _connect_registry() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id FROM profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not row:
            raise KeyError(profile_id)

        existing = conn.execute(
            "SELECT id FROM profiles WHERE lower(name)=lower(?) AND id!=?",
            (clean_name, profile_id),
        ).fetchone()
        if existing:
            raise ValueError("A profile with that name already exists")

        conn.execute(
            "UPDATE profiles SET name=? WHERE id=?",
            (clean_name, profile_id),
        )
        conn.commit()
        _load_cache(conn)

    return _profiles_cache[profile_id]


def delete_profile(profile_id: str) -> ProfileRecord:
    with _connect_registry() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT id, is_default FROM profiles WHERE id=?",
            (profile_id,),
        ).fetchone()
        if not row:
            raise KeyError(profile_id)
        if int(row["is_default"]) == 1:
            raise ValueError("Default profile cannot be deleted")

        count_row = conn.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()
        count = int(count_row["n"]) if count_row else 0
        if count <= 1:
            raise ValueError("Cannot delete the last profile")

        deleted_profile = _profiles_cache.get(profile_id)
        if deleted_profile is None:
            _load_cache(conn)
            deleted_profile = _profiles_cache.get(profile_id)

        active_id = _active_profile_id
        if active_id == profile_id:
            default_row = conn.execute(
                "SELECT id FROM profiles WHERE is_default=1 LIMIT 1"
            ).fetchone()
            if default_row:
                _set_active_profile(conn, str(default_row["id"]))

        conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        conn.commit()
        _load_cache(conn)

    if deleted_profile:
        shutil.rmtree(deleted_profile.data_dir, ignore_errors=True)
        return deleted_profile

    raise KeyError(profile_id)


def copy_admin_settings(source_profile_id: str, target_profile_id: str) -> int:
    source = get_profile(source_profile_id)
    target = get_profile(target_profile_id)
    if source is None:
        raise KeyError(source_profile_id)
    if target is None:
        raise KeyError(target_profile_id)

    source_db = source.data_dir / "vip.db"
    target_db = target.data_dir / "vip.db"
    if not source_db.exists() or not target_db.exists():
        return 0

    with sqlite3.connect(source_db) as src_conn, sqlite3.connect(target_db) as dst_conn:
        src_conn.row_factory = sqlite3.Row
        rows = src_conn.execute("SELECT key, value FROM app_settings").fetchall()
        if not rows:
            return 0
        dst_conn.executemany(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            [(str(row["key"]), str(row["value"])) for row in rows],
        )
        dst_conn.commit()
        return len(rows)


def get_current_profile_id() -> str:
    profile_id = _current_profile_id.get()
    if profile_id:
        return profile_id
    return get_active_profile().id


def get_current_profile_data_dir(_root_override: Path | None = None) -> Path:
    profile = get_profile(get_current_profile_id()) or get_active_profile()
    profile.data_dir.mkdir(parents=True, exist_ok=True)
    return profile.data_dir


def set_current_profile(profile_id: str) -> contextvars.Token[str | None]:
    return _current_profile_id.set(profile_id)


def reset_current_profile(token: contextvars.Token[str | None]) -> None:
    _current_profile_id.reset(token)


async def run_in_profile(
    profile_id: str,
    func: Callable[..., T | Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    token = set_current_profile(profile_id)
    try:
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
    finally:
        reset_current_profile(token)