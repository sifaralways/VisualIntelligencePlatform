"""
VIP — FastAPI application entry point.
"""

import asyncio
import importlib.metadata
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import time
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.version import Version
except ModuleNotFoundError:  # pragma: no cover
    Requirement = None  # type: ignore[assignment]
    Version = None  # type: ignore[assignment]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REQUIREMENTS_PATH = _REPO_ROOT / "requirements.txt"
_BOOTSTRAP_LOCK_PATH = _REPO_ROOT / ".vip-deps-bootstrap.lock"


def _read_unsatisfied_requirements() -> list[str]:
    if not _REQUIREMENTS_PATH.exists():
        return []

    # Fallback path for environments that don't yet have the "packaging" module.
    # We still detect missing top-level packages so bootstrap can recover.
    if Requirement is None or Version is None:
        unsatisfied: list[str] = []
        for raw_line in _REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
            requirement = raw_line.split("#", 1)[0].strip()
            if not requirement:
                continue
            name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip()
            if not name:
                continue
            try:
                importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                unsatisfied.append(name)
        return unsatisfied

    unsatisfied: list[str] = []
    for raw_line in _REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines():
        requirement = raw_line.split("#", 1)[0].strip()
        if not requirement:
            continue

        try:
            req = Requirement(requirement)
        except Exception:
            continue

        if req.marker is not None and not req.marker.evaluate():
            continue

        try:
            installed_version = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            unsatisfied.append(requirement)
            continue

        if req.specifier and Version(installed_version) not in req.specifier:
            unsatisfied.append(requirement)

    return unsatisfied


def _wait_for_bootstrap_lock(timeout_sec: float = 300.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while _BOOTSTRAP_LOCK_PATH.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)
    return True


def _bootstrap_missing_python_dependencies() -> None:
    if os.environ.get("VIP_AUTO_INSTALL_DEPS", "1").strip().lower() in {"0", "false", "no"}:
        return

    if not _wait_for_bootstrap_lock():
        print("VIP dependency bootstrap lock timed out; continuing without auto-install.", flush=True)
        return

    unsatisfied = _read_unsatisfied_requirements()
    if not unsatisfied:
        return

    lock_fd: int | None = None
    try:
        lock_fd = os.open(_BOOTSTRAP_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _wait_for_bootstrap_lock():
            return
        print("VIP dependency bootstrap lock timed out; continuing without auto-install.", flush=True)
        return

    try:
        print(
            "VIP startup: installing/updating Python dependencies: " + ", ".join(unsatisfied),
            flush=True,
        )
        env = dict(os.environ)
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *unsatisfied],
            cwd=str(_REPO_ROOT),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", flush=True)
            raise RuntimeError("Automatic dependency install failed during startup")
        if result.stdout.strip():
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
        if result.stderr.strip():
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n", flush=True)
        print("VIP startup: dependency bootstrap complete.", flush=True)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        try:
            _BOOTSTRAP_LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass


_bootstrap_missing_python_dependencies()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend.config import settings, ensure_dirs
from backend.database.db import init_db
from backend.api.routes import media, persons, faces, search, pipeline, writeback, admin, tags, analysis, settings as settings_route, folders, remote, chat, profiles
from backend.api.websocket import router as ws_router
from backend.pipeline.suggestion_worker import run_quality_suggestion_worker
from backend.profiles import bootstrap_profiles, get_active_profile, get_profile, reset_current_profile, run_in_profile, set_current_profile
from backend.runtime.activity import mark_user_activity


def _patch_insightface_skimage() -> None:
    """
    Monkey-patch InsightFace's face_align.estimate_norm() to use the
    skimage >= 0.26 constructor API instead of the deprecated instance method.

    skimage 0.26 deprecated SimilarityTransform.estimate() and will remove it
    in 2.2.  InsightFace upstream hasn't been updated yet.  This patch is
    applied at startup so it survives venv rebuilds without touching installed
    package files.
    """
    try:
        import numpy as np
        from skimage import transform as _trans
        import insightface.utils.face_align as _fa

        _arcface_dst = _fa.arcface_dst  # already defined in the module

        def _estimate_norm_patched(lmk, image_size=112, mode="arcface"):
            assert lmk.shape == (5, 2)
            assert image_size % 112 == 0 or image_size % 128 == 0
            if image_size % 112 == 0:
                ratio = float(image_size) / 112.0
                diff_x = 0.0
            else:
                ratio = float(image_size) / 128.0
                diff_x = 8.0 * ratio
            dst = _arcface_dst * ratio
            dst[:, 0] += diff_x
            # from_estimate() is the replacement for the deprecated instance .estimate()
            tform = _trans.SimilarityTransform.from_estimate(lmk, dst)
            return tform.params[0:2, :]

        _fa.estimate_norm = _estimate_norm_patched
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "InsightFace skimage patch failed (non-fatal): %s", exc
        )

    # Suppress FutureWarning from insightface/utils/transform.py about
    # np.linalg.lstsq rcond parameter — upstream hasn't updated the call yet.
    warnings.filterwarnings(
        "ignore",
        message=r".*rcond.*parameter will change.*",
        category=FutureWarning,
        module=r"insightface\.utils\.transform",
    )


# Module-level handler refs so apply_log_level() can adjust them at runtime
_file_handler: logging.handlers.RotatingFileHandler | None = None
_console_handler: logging.StreamHandler | None = None

# Mapping: 0=Error, 1=Info, 2=Debug
_LEVEL_MAP = {0: logging.ERROR, 1: logging.INFO, 2: logging.DEBUG}


def apply_log_level(level: int) -> None:
    """
    Apply a log-level setting (0=Error, 1=Info, 2=Debug) to both handlers
    and the root logger.  Safe to call at any time — takes effect immediately.
    """
    py_level = _LEVEL_MAP.get(int(level), logging.INFO)
    root = logging.getLogger()
    root.setLevel(py_level)
    if _file_handler:
        _file_handler.setLevel(py_level)
    if _console_handler:
        _console_handler.setLevel(py_level)
    logging.getLogger(__name__).info(
        "Log level set to %s", logging.getLevelName(py_level)
    )


def _setup_logging() -> None:
    """Configure rotating file + console logging for the whole application."""
    global _file_handler, _console_handler

    log_dir = Path.home() / "Library" / "Logs" / "VIP"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "vip.log"

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file: 10 MB × 5 backups → max 50 MB on disk
    _file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(fmt)
    _file_handler.setLevel(logging.INFO)  # default Info until DB setting is read

    # Console handler
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(fmt)
    _console_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_file_handler)
    root.addHandler(_console_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # quieten access log spam

    logging.getLogger(__name__).info(
        "Logging initialised → %s", log_file
    )


def _patch_pillow_limits() -> None:
    """
    Raise Pillow's decompression-bomb pixel limit to accommodate large panoramas
    and ultra-high-res camera output (some cameras exceed 100 MP).

    Pillow's default hard limit is ~179 MP (2 × 89.5 MP).  Reaching it raises
    a DecompressionBombError that silently drops the photo from the pipeline.
    The protection was designed for servers processing untrusted uploads;
    VIP exclusively opens files from the user’s own trusted local library, so
    the limit is not needed.
    """
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None  # disable bomb check entirely
        logging.getLogger(__name__).debug("Pillow MAX_IMAGE_PIXELS limit removed")
    except Exception as exc:  # pragma: no cover
        logging.getLogger(__name__).warning(
            "Could not patch Pillow pixel limit (non-fatal): %s", exc
        )


_setup_logging()
_patch_insightface_skimage()
_patch_pillow_limits()


async def _warm_start_models(profile_id: str) -> None:
    """Prefetch downloadable model assets in the background on app startup."""
    from backend.profiles import run_in_profile

    async def _run() -> None:
        from backend.database.settings_store import get as get_setting, load_cache
        from backend.pipeline.ingest import _detector, _florence, _tagger

        await load_cache()
        loop = asyncio.get_running_loop()

        logger = logging.getLogger(__name__)
        logger.info("Startup warm-up: checking local model availability")

        # Face detection/embedding is a core path, so warm it regardless of tag toggles.
        await loop.run_in_executor(None, _detector.load)

        # Secondary tagging models honor the per-profile module toggles.
        await loop.run_in_executor(None, _tagger.load)

        if bool(int(get_setting("florence_enabled") or 0)):
            await loop.run_in_executor(None, _florence.load)

        logger.info("Startup warm-up complete")

    try:
        await run_in_profile(profile_id, _run)
    except Exception:
        logging.getLogger(__name__).exception("Startup model warm-up failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    active_profile = bootstrap_profiles()
    token = set_current_profile(active_profile.id)
    warmup_task: asyncio.Task[None] | None = None
    suggestion_worker_task: asyncio.Task[None] | None = None
    try:
        ensure_dirs()
        await init_db()
        from backend.database.settings_store import get as _get_setting, load_cache

        await load_cache()
        apply_log_level(int(_get_setting('log_level')))
        warmup_task = asyncio.create_task(_warm_start_models(active_profile.id))
        suggestion_worker_task = asyncio.create_task(
            run_in_profile(active_profile.id, run_quality_suggestion_worker)
        )
    finally:
        reset_current_profile(token)
    yield
    # Cleanup on shutdown (none required currently)
    if warmup_task is not None and not warmup_task.done():
        warmup_task.cancel()
    if suggestion_worker_task is not None and not suggestion_worker_task.done():
        suggestion_worker_task.cancel()


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "Local-first media intelligence platform. "
        "Runs entirely offline on Apple Silicon macOS."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS — allow Vite dev server on localhost:5173
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def profile_context_middleware(request: Request, call_next):
    mark_user_activity()
    requested_profile_id = request.headers.get("X-VIP-Profile")
    profile = get_profile(requested_profile_id) if requested_profile_id else get_active_profile()
    if requested_profile_id and profile is None:
        return JSONResponse({"detail": f"Unknown profile: {requested_profile_id}"}, status_code=404)

    token = set_current_profile(profile.id)
    request.state.profile_id = profile.id
    try:
        return await call_next(request)
    finally:
        reset_current_profile(token)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(ws_router)
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(media.router,    prefix="/api/media",    tags=["media"])
app.include_router(persons.router,  prefix="/api/persons",  tags=["persons"])
app.include_router(faces.router,    prefix="/api/faces",    tags=["faces"])
app.include_router(search.router,   prefix="/api/search",   tags=["search"])
app.include_router(chat.router,     prefix="/api/chat",     tags=["chat"])
app.include_router(profiles.router, prefix="/api/profiles", tags=["profiles"])
app.include_router(writeback.router, prefix="/api/writeback", tags=["writeback"])
app.include_router(admin.router,    prefix="/api/admin",    tags=["admin"])
app.include_router(tags.router,      prefix="/api/tags",      tags=["tags"])
app.include_router(analysis.router,  prefix="/api/analysis",  tags=["analysis"])
app.include_router(settings_route.router, prefix="/api/settings", tags=["settings"])
app.include_router(remote.router,         prefix="/api/remote",   tags=["remote"])
app.include_router(folders.router,        prefix="/api/folders",  tags=["folders"])

@app.get("/api/health", tags=["system"])
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
    }


@app.get("/thumbnails/{filename:path}", tags=["system"])
async def legacy_thumbnail(filename: str):
    safe_name = Path(filename).name
    path = settings.thumbnail_dir / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/jpeg")
