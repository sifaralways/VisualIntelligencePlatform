"""
VIP — FastAPI application entry point.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.config import settings, ensure_dirs
from backend.database.db import init_db
from backend.api.routes import media, persons, faces, search, pipeline, writeback
from backend.api.websocket import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown logic."""
    # Ensure all directories exist
    ensure_dirs()
    # Initialise database (creates tables if not exist)
    await init_db()
    yield
    # Cleanup on shutdown (none required currently)


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

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(ws_router)
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(media.router,    prefix="/api/media",    tags=["media"])
app.include_router(persons.router,  prefix="/api/persons",  tags=["persons"])
app.include_router(faces.router,    prefix="/api/faces",    tags=["faces"])
app.include_router(search.router,   prefix="/api/search",   tags=["search"])
app.include_router(writeback.router, prefix="/api/writeback", tags=["writeback"])

# ---------------------------------------------------------------------------
# Static — serve face thumbnails and previews
# ---------------------------------------------------------------------------
app.mount(
    "/thumbnails",
    StaticFiles(directory=str(settings.thumbnail_dir)),
    name="thumbnails",
)


@app.get("/api/health", tags=["system"])
async def health() -> dict:
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
    }
