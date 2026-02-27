"""
VIP Pydantic models — mirrors the DB schema.
Used for API responses and inter-module data passing.
"""

from __future__ import annotations
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
import uuid as _uuid


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class MediaFileBase(BaseModel):
    file_path: str
    file_hash: str
    file_size: Optional[int] = None
    file_format: Optional[str] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    date_taken: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None


class MediaFile(MediaFileBase):
    id: int
    is_stub: bool = False
    needs_reprocess: bool = False
    ingest_state: str = "pending"
    first_seen_at: str
    last_seen_at: str
    writeback_done: bool = False
    writeback_at: Optional[str] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Person
# ---------------------------------------------------------------------------
class PersonCreate(BaseModel):
    name: Optional[str] = None

class Person(BaseModel):
    id: int
    uuid: str
    name: Optional[str] = None
    created_at: str
    named_at: Optional[str] = None
    photo_count: int = 0
    is_merged: bool = False
    merged_into_id: Optional[int] = None

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------
class Cluster(BaseModel):
    id: int
    person_id: Optional[int] = None
    member_count: int = 0
    intra_similarity: Optional[float] = None
    is_high_conf: bool = False
    created_at: str
    last_updated_at: str

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Face
# ---------------------------------------------------------------------------
class Face(BaseModel):
    id: int
    media_file_id: int
    bbox_x: Optional[float] = None
    bbox_y: Optional[float] = None
    bbox_w: Optional[float] = None
    bbox_h: Optional[float] = None
    detection_conf: Optional[float] = None
    thumbnail_path: Optional[str] = None
    person_id: Optional[int] = None
    cluster_id: Optional[int] = None
    created_at: str

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Pipeline progress events (sent over WebSocket)
# ---------------------------------------------------------------------------
class ProgressEvent(BaseModel):
    event: str                      # scan_start | scan_progress | embed_progress | etc.
    total: Optional[int] = None
    done: Optional[int] = None
    current_file: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Writeback
# ---------------------------------------------------------------------------
class WritebackQueueItem(BaseModel):
    id: int
    media_file_id: int
    status: str
    queued_at: str
    written_at: Optional[str] = None
    error_msg: Optional[str] = None

    class Config:
        from_attributes = True


class WritebackPreview(BaseModel):
    """Result of a dry-run: shows what ExifTool would write."""
    media_file_id: int
    file_path: str
    fields_to_write: dict[str, list[str]]   # field_name -> [value, ...]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    person_ids: Optional[list[int]] = None
    date_from: Optional[str] = None     # ISO8601
    date_to: Optional[str] = None
    limit: int = Field(default=50, le=500)
    offset: int = 0


class SearchResult(BaseModel):
    media_file: MediaFile
    persons: list[Person] = []
    score: Optional[float] = None
