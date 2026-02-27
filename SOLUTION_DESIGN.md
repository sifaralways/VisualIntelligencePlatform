# VIP — Visual Intelligence Platform
## Solution Design Document
**Version:** 0.1  
**Status:** Living Document — update after every significant decision or implementation milestone  
**Last Updated:** 2026-02-27  
**Purpose:** Single source of truth for architecture, decisions, and implementation state. Sufficient context to resume work without any prior chat history.

---

## 1. Project Identity

| Field | Value |
|---|---|
| Full Name | Visual Intelligence Platform |
| Short Name | VIP |
| Repo | https://github.com/sifaralways/VisualIntelligencePlatform |
| App Support Dir | `~/Library/Application Support/VIP/` |
| DB Name | `vip.db` |
| Spotlight Prefix | `VIP:` (for custom comment fields only) |
| Target Platform | **Apple Silicon macOS only** (M-series, not Intel) |
| Distribution | GitHub + `./setup.sh` — free, shared with friends |
| License Constraint | Non-commercial. InsightFace Buffalo_L weights are permitted. |

---

## 2. Confirmed Decisions Log

This section is the most important one. Every decision recorded here was explicitly confirmed. Nothing here is assumed.

### 2.1 Media & Storage
- **Library size:** 50K–100K photos, 10K–20K videos. Phase 1 photos only; videos introduced only if hardware headroom confirmed.
- **File formats:** Primarily Canon CR3. Must also support Sony ARW, Nikon NEF, DNG for future users. HEIC/JPEG not a priority.
- **RAW decode strategy:** Never decode full RAW sensor data for ML. **Extract the embedded JPEG preview** from the RAW container using ExifTool. This is camera-agnostic, fast, and avoids LibRaw/rawpy dependency for the ML pipeline. Full RAW decode (via rawpy) only on demand for UI display.
- **File sizes:** 30–60MB per CR3. Total library potentially 3–6TB.
- **Storage medium:** External NVMe SSD or internal SSD. Not a spinning NAS.
- **iCloud workflow:** Files live in iCloud. User downloads batches locally → runs VIP pipeline → offloads files back to iCloud. VIP assumes files are **fully materialised** on disk when processing runs. VIP does not manage downloads or offloads.
- **iCloud stub detection:** When a file is offloaded, macOS leaves a ~1KB stub. Scanner **must detect and skip stubs** before attempting any read. Detection method: check `com.apple.ubiquity.itemhascontents` xattr or file size < threshold for known RAW type.
- **No sidecar files.** Metadata is written directly into the original CR3/RAW file using ExifTool. See §2.5.

### 2.2 Machine Learning
- **Runtime:** MLX (Apple Silicon, unified memory). No PyTorch fallback — dropped entirely.
- **Face detection model:** InsightFace Buffalo_L (RetinaFace detector + ArcFace-style 512-D embeddings).
- **Embedding dimension:** 512-D float32 vectors.
- **Clustering algorithm:** HDBSCAN (via `hdbscan` or `scikit-learn`). Runs silently; user never sees "clusters" — only the UX abstraction over them (§2.4).
- **Clustering tuning:** `min_cluster_size` to be calibrated in Phase 3 against real library data. Starting point: 5 for family-scale libraries.
- **High-confidence threshold:** To be empirically determined, likely cosine similarity ≥ 0.95 within cluster. Clusters above this → show single representative tile + count. Clusters below → show multiple tiles for review.
- **No cloud ML, no network calls, ever.**

### 2.3 Idempotency & Reprocessing
- **Files are processed at most once by default.** Identity check = SHA-256 hash of file content stored in DB.
- **If hash exists in DB → skip entirely.** No re-read, no re-embed, no re-cluster.
- **Re-evaluate** is an explicit user action per folder. It marks all files in that folder as `needs_reprocess = true` and queues them. Reprocessing never silently discards existing identity labels — it merges cautiously (see §5.3).
- **File moves** (same content, different path) are detected via hash → existing record updated with new path, not duplicated.

### 2.4 UX / Naming Flow
- **User never sees raw cluster IDs or ML jargon.**
- The UI surface is: **a grid of representative face tiles**, sorted by frequency (most-photographed person first).
- Each tile shows: best representative face crop + photo count.
- **High-confidence cluster** (≥ threshold): one tile, one count. Tap → type name → done.
- **Low-confidence cluster**: multiple tiles shown for that group, each checkable. User reviews, unchecks wrong faces, then names.
- **Same name entered for two different tiles:** system prompts — *"Is this the same person or a different person with the same name?"*  
  - Same person → merge clusters, assign single UUID, combined count shown.  
  - Different person → both named independently, UUID kept separate (e.g., "John Smith" and "John Smith (2)").
- **Writeback is never automatic.** Naming in the UI updates the DB only. A separate explicit **"Write to Files"** action triggers ExifTool. This action requires files to be locally present.
- **Dry-run preview** is shown before any write: list of files to be touched, fields to be written. User confirms.
- **Undo / rename** supported at any time — renames update DB instantly; next writeback session overwrites the old metadata.

### 2.5 Metadata Writeback
- **Tool:** ExifTool (CLI, wrapped in Python subprocess with timeout and error capture).
- **Write target:** Directly into the original CR3/RAW file. No sidecar XMP.
- **ExifTool backup:** ExifTool's default `_original` backup is enabled for the **first ever write** to a file. User can purge backups via UI after verifying results. Subsequent rewrites to same file do not re-backup (controlled via `-overwrite_original` flag after first write).
- **Fields written (standard, Spotlight-visible):**
  - `XMP:PersonInImage` — array of person names
  - `XMP:Subject` / `IPTC:Keywords` — searchable tags (person names, scene tags, object labels)
  - `XMP:RegionInfo` — MWG face region metadata (bounding box + name, Lightroom/Capture One compatible)
  - `XMP:Description` — auto-generated summary (optional, configurable)
- **Fields NOT written to files** (DB-only, too proprietary for file embedding):
  - Face embedding vectors
  - Cluster UUIDs
  - Confidence scores
  - Processing state flags
- **Corruption risk mitigated by:** ExifTool atomic write (temp file → rename), backup of original, dry-run preview, and explicit user confirmation. Image pixel data is never affected. Risk is rated: **low**.

### 2.6 Search
- **Primary search layer:** Local SQLite DB. Always available regardless of iCloud file state.
- **Secondary search layer:** macOS Spotlight via standard XMP fields written into files.
- **When files are offline (iCloud stubs):** VIP search still works fully — it queries the DB. Spotlight also works for named fields. Full-resolution image view requires download (outside VIP scope).
- **Search in UI does not require files to be present locally.**

---

## 3. Architecture Overview

```
External NVMe SSD / Internal SSD
        │  (CR3, ARW, NEF, DNG files — present during processing sessions only)
        │
┌────────────────────────────────────────────────────────┐
│                    macOS Host (M2 Max, 64GB)           │
│                                                        │
│  ┌──────────────────┐    ┌───────────────────────────┐ │
│  │  Media Scanner   │    │   ML Inference Engine     │ │
│  │  (Python)        │    │   (InsightFace + MLX)     │ │
│  │                  │    │                           │ │
│  │  • Recursive walk│    │  • JPEG preview extract   │ │
│  │  • Stub detect   │───▶│  • Face detect (Retina)   │ │
│  │  • SHA-256 hash  │    │  • 512-D embed (ArcFace)  │ │
│  │  • EXIF extract  │    │  • Batched, thermal-aware │ │
│  │  • Idempotency   │    └────────────┬──────────────┘ │
│  └──────────────────┘                 │                 │
│                                       ▼                 │
│  ┌────────────────────────────────────────────────────┐ │
│  │              SQLite DB  (vip.db)                   │ │
│  │  media_files | faces | embeddings | persons        │ │
│  │  clusters | scan_state | writeback_queue           │ │
│  └────────────────────────┬───────────────────────────┘ │
│                            │                             │
│  ┌─────────────────────┐   │   ┌──────────────────────┐ │
│  │   FAISS Index       │◀──┤   │  HDBSCAN Clusterer   │ │
│  │   (vip.faiss)       │   │   │  (runs post-ingest)  │ │
│  │   512-D, flat/IVF   │   │   └──────────────────────┘ │
│  └─────────────────────┘   │                             │
│                             │                            │
│  ┌──────────────────────────▼───────────────────────┐   │
│  │            FastAPI Server  (localhost:7474)       │   │
│  │            REST + WebSocket                       │   │
│  └──────────────────────────┬───────────────────────┘   │
│                              │                           │
│  ┌───────────────────────────▼──────────────────────┐   │
│  │            React + Vite  (localhost:5173)         │   │
│  │            Browser UI — face tiles, search,       │   │
│  │            naming, writeback controls             │   │
│  └──────────────────────────────────────────────────┘   │
│                                                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │   ExifTool Writeback Engine  (Phase 5+)           │  │
│  │   Reads writeback_queue → writes XMP into files   │  │
│  │   Only runs when files are locally present        │  │
│  └───────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
        │
        ▼
  iCloud Drive (files offloaded here after processing)
  Spotlight searches XMP fields even when files are stubs
```

---

## 4. Technology Stack

| Layer | Tool | Version | Rationale |
|---|---|---|---|
| Language | Python | 3.11+ | MLX compatibility, async support |
| ML — Face Detection | InsightFace (RetinaFace) | latest | Best accuracy, non-commercial free |
| ML — Face Embedding | InsightFace (ArcFace Buffalo_L) | latest | 512-D, SOTA, non-commercial free |
| ML Runtime | MLX | latest | Apple Silicon only, unified memory |
| Clustering | HDBSCAN | via `scikit-learn` or `hdbscan` | Density-based, handles noise well |
| Vector Index | FAISS | latest | Fast ANN search, flat/IVF as needed |
| RAW preview extract | ExifTool | 12+ | Camera-agnostic JPEG preview extraction |
| RAW display decode | rawpy + LibRaw | latest | On-demand only for UI display |
| EXIF read | ExifTool (JSON mode) | 12+ | Most complete EXIF/XMP reader |
| Metadata write | ExifTool | 12+ | Atomic write, CR3-safe, MWG regions |
| Database | SQLite | via `aiosqlite` | No server, <200K rows, sufficient |
| API | FastAPI | latest | Async, WebSocket, auto-docs |
| Frontend | React + Vite | React 18 / Vite 5 | Lightweight, no SSR needed |
| Styling | Tailwind CSS | v3 | Utility-first, fast iteration |
| Image serving | FastAPI static / streaming | — | Serve face crops + thumbnails locally |
| Job orchestration | Python `asyncio` + queue | — | Single machine, no Celery needed |
| Setup | `setup.sh` (Homebrew + pip + npm) | — | One-command install for friends |

### Dropped / Deferred
- **PyTorch:** Dropped. MLX only.
- **DuckDB:** Deferred. SQLite sufficient at current scale. Revisit if >500K files.
- **Docker:** Not used. Native macOS install via `setup.sh`.
- **Celery / RQ:** Not needed. `asyncio` + background task queue sufficient.
- **Tesseract / TrOCR:** Phase 6 only. Apple Vision OCR to be evaluated first.

---

## 5. Data Schema

### 5.1 `media_files`
```sql
CREATE TABLE media_files (
    id              INTEGER PRIMARY KEY,
    file_path       TEXT NOT NULL UNIQUE,       -- absolute path when last seen
    file_hash       TEXT NOT NULL UNIQUE,       -- SHA-256 of file content
    file_size       INTEGER,                    -- bytes
    file_format     TEXT,                       -- 'CR3', 'ARW', 'NEF', 'DNG', etc.
    camera_make     TEXT,
    camera_model    TEXT,
    date_taken      TEXT,                       -- ISO8601, from EXIF
    gps_lat         REAL,
    gps_lon         REAL,
    width           INTEGER,
    height          INTEGER,
    is_stub         INTEGER DEFAULT 0,          -- 1 if iCloud stub at scan time
    needs_reprocess INTEGER DEFAULT 0,          -- set by re-evaluate action
    ingest_state    TEXT DEFAULT 'pending',     -- pending | scanned | embedded | clustered
    first_seen_at   TEXT,                       -- ISO8601
    last_seen_at    TEXT,
    writeback_done  INTEGER DEFAULT 0,          -- 1 if ExifTool write completed
    writeback_at    TEXT                        -- ISO8601 of last write
);
```

### 5.2 `faces`
```sql
CREATE TABLE faces (
    id              INTEGER PRIMARY KEY,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id),
    bbox_x          REAL,                       -- normalised 0–1
    bbox_y          REAL,
    bbox_w          REAL,
    bbox_h          REAL,
    detection_conf  REAL,                       -- RetinaFace confidence
    thumbnail_path  TEXT,                       -- path to saved face crop JPEG
    person_id       INTEGER REFERENCES persons(id),   -- NULL until named
    cluster_id      INTEGER REFERENCES clusters(id),  -- NULL until clustered
    created_at      TEXT
);
```

### 5.3 `embeddings`
```sql
CREATE TABLE embeddings (
    id              INTEGER PRIMARY KEY,
    face_id         INTEGER NOT NULL UNIQUE REFERENCES faces(id),
    vector          BLOB NOT NULL,              -- 512 x float32, raw bytes
    model_version   TEXT                        -- e.g. 'buffalo_l_v1'
);
-- Note: embeddings are NEVER deleted. Reprocessing adds new rows with new model_version.
```

### 5.4 `persons`
```sql
CREATE TABLE persons (
    id              INTEGER PRIMARY KEY,
    uuid            TEXT NOT NULL UNIQUE,       -- stable UUID, written to files
    name            TEXT,                       -- human-assigned, nullable
    created_at      TEXT,
    named_at        TEXT,
    photo_count     INTEGER DEFAULT 0,          -- denormalised, updated on change
    is_merged       INTEGER DEFAULT 0,          -- 1 if this person was merged into another
    merged_into_id  INTEGER REFERENCES persons(id)
);
```

### 5.5 `clusters`
```sql
CREATE TABLE clusters (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER REFERENCES persons(id),  -- set when named
    centroid        BLOB,                            -- mean 512-D vector
    member_count    INTEGER,
    intra_similarity REAL,                           -- mean cosine sim within cluster
    is_high_conf    INTEGER,                         -- 1 if above threshold
    created_at      TEXT,
    last_updated_at TEXT
);
```

### 5.6 `scan_state`
```sql
CREATE TABLE scan_state (
    id              INTEGER PRIMARY KEY,
    folder_path     TEXT NOT NULL UNIQUE,
    last_scan_at    TEXT,
    file_count      INTEGER,
    status          TEXT                        -- 'idle' | 'scanning' | 'error'
);
```

### 5.7 `writeback_queue`
```sql
CREATE TABLE writeback_queue (
    id              INTEGER PRIMARY KEY,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id),
    status          TEXT DEFAULT 'pending',     -- pending | dry_run | confirmed | written | failed
    queued_at       TEXT,
    written_at      TEXT,
    error_msg       TEXT
);
```

---

## 6. Repository Structure

```
VisualIntelligencePlatform/
│
├── SOLUTION_DESIGN.md          ← this document
├── High level BRD.md           ← original business requirements
├── README.md
├── .gitignore
├── setup.sh                    ← one-command bootstrap (Homebrew + pip + npm)
│
├── backend/
│   ├── main.py                 ← FastAPI app entry point
│   ├── config.py               ← paths, thresholds, constants
│   ├── database/
│   │   ├── db.py               ← aiosqlite connection pool
│   │   ├── migrations/         ← numbered SQL migration files
│   │   └── models.py           ← dataclasses / pydantic models matching schema
│   ├── scanner/
│   │   ├── walker.py           ← recursive folder walk, stub detection
│   │   ├── hasher.py           ← SHA-256, idempotency check
│   │   ├── exif_reader.py      ← ExifTool JSON wrapper, EXIF extraction
│   │   └── preview_extractor.py ← ExifTool embedded JPEG preview extraction
│   ├── ml/
│   │   ├── face_detector.py    ← InsightFace RetinaFace wrapper
│   │   ├── embedder.py         ← InsightFace ArcFace embedding wrapper
│   │   ├── clusterer.py        ← HDBSCAN clustering, merge/split logic
│   │   └── index.py            ← FAISS index load/save/query
│   ├── pipeline/
│   │   ├── ingest.py           ← orchestrates scan → extract → embed → cluster
│   │   └── queue.py            ← async task queue, backpressure, thermal check
│   ├── writeback/
│   │   ├── exiftool.py         ← ExifTool subprocess wrapper, atomic write
│   │   ├── fields.py           ← XMP field mapping (PersonInImage, MWG Regions etc)
│   │   └── engine.py           ← dry-run, confirm, write, rollback logic
│   ├── api/
│   │   ├── routes/
│   │   │   ├── media.py        ← media file endpoints
│   │   │   ├── persons.py      ← person/naming endpoints
│   │   │   ├── faces.py        ← face tile endpoints
│   │   │   ├── search.py       ← search endpoints
│   │   │   ├── pipeline.py     ← trigger scan, get progress via WS
│   │   │   └── writeback.py    ← dry-run, confirm, status
│   │   └── websocket.py        ← progress events
│   └── tests/
│       └── ...
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── pages/
│   │   │   ├── PeoplePage.tsx  ← face tile grid, naming UI
│   │   │   ├── SearchPage.tsx
│   │   │   └── WritebackPage.tsx
│   │   ├── components/
│   │   └── api/               ← typed fetch wrappers
│   └── ...
│
├── data/                      ← gitignored. runtime data lives here or in ~/Library/Application Support/VIP/
│   ├── vip.db
│   ├── vip.faiss
│   ├── thumbnails/            ← face crop JPEGs (small, ~10KB each)
│   └── previews/              ← extracted JPEG previews (temp, cleared after embed)
│
└── scripts/
    ├── benchmark.py            ← measure throughput on real library
    └── reset_db.py             ← dev tool: wipe DB + index, keep named persons export
```

---

## 7. Phase Plan (Revised from BRD)

### Phase 0 — Foundations (Week 0–1) ← CURRENT
**Goal:** Repo structure, environment, schema, model validation.

- [x] Git repo linked to remote
- [x] `.gitignore` complete
- [ ] `setup.sh` written and tested on M2 Mac
- [ ] Backend skeleton: FastAPI + aiosqlite + DB migrations
- [ ] Frontend skeleton: React + Vite + Tailwind
- [ ] InsightFace Buffalo_L loaded via MLX — confirm it initialises
- [ ] Benchmark: extract embedded JPEG from 100 CR3s, measure throughput
- [ ] Benchmark: run face embed on 1,000 faces, measure time + memory
- [ ] Confirm iCloud stub detection method with `xattr` on a real stub file
- [ ] Lock confidence threshold empirically (run Buffalo_L on test faces)
- [ ] Document model version in `embeddings.model_version`

### Phase 1 — Media Ingest & Catalog (Week 1–3)
**Goal:** Safely scan, hash, catalog, and extract previews from 100K files.

- [ ] `walker.py` — recursive walk, iCloud stub detection, format filtering
- [ ] `hasher.py` — SHA-256, idempotency check against DB
- [ ] `exif_reader.py` — ExifTool JSON wrapper, extract date/GPS/camera/dims
- [ ] `preview_extractor.py` — ExifTool extract embedded JPEG from CR3/ARW/NEF/DNG
- [ ] DB migration 001: `media_files`, `scan_state`
- [ ] `scan_state` tracking — resume after interruption
- [ ] API: `POST /pipeline/scan` (folder path), `GET /pipeline/status` (WebSocket)
- [ ] Test: 10K real CR3 files, measure scan throughput, verify idempotency
- [ ] Re-evaluate folder action (marks files `needs_reprocess=true`)

### Phase 2 — Face Detection & Embeddings (Week 3–6)
**Goal:** Detect faces in all preview JPEGs, produce 512-D embeddings.

- [ ] `face_detector.py` — RetinaFace via InsightFace, confidence filter
- [ ] `embedder.py` — ArcFace via InsightFace, batch processing
- [ ] Face crop thumbnails saved to `data/thumbnails/`
- [ ] DB migrations: `faces`, `embeddings`
- [ ] Batch pipeline with backpressure (don't OOM on 64GB unified memory)
- [ ] Thermal-aware scheduling: check CPU temp, back off if needed
- [ ] Progress via WebSocket
- [ ] Test: detection accuracy on diverse test faces
- [ ] Previews temp files cleared after embedding complete

### Phase 3 — Vector Indexing & Clustering (Week 6–8)
**Goal:** Faces grouped into person clusters automatically.

- [ ] FAISS flat index for <100K embeddings (upgrade to IVF if needed)
- [ ] `index.py` — save/load `.faiss` file, add/query
- [ ] `clusterer.py` — HDBSCAN, calibrate `min_cluster_size`
- [ ] DB migrations: `clusters`, `persons` (unnamed)
- [ ] High-confidence vs. low-confidence cluster classification
- [ ] Cluster merge/split logic (preserves embeddings always)
- [ ] Incremental re-cluster after new embeddings added

### Phase 4 — Web UI v1: People & Naming (Week 7–10)
**Goal:** User can browse face tiles, name people, manage merges.

- [ ] `PeoplePage` — face tile grid, sorted by photo count
- [ ] High-confidence tile: single face crop + count + name input
- [ ] Low-confidence tile group: multi-face grid with checkboxes
- [ ] Name input → same name detection → merge or split dialog
- [ ] Person UUID assigned on naming
- [ ] DB updates on name/merge/rename (no file writes yet)
- [ ] Undo / rename at any time
- [ ] Thumbnail serving via FastAPI static endpoint

### Phase 5 — Metadata Writeback (Week 10–12)
**Goal:** Names written into original CR3 files safely and correctly.

- [ ] `exiftool.py` — subprocess wrapper, timeout, error capture
- [ ] `fields.py` — XMP field mapping: `PersonInImage`, `Subject`, `Keywords`, MWG `RegionInfo`
- [ ] `engine.py` — dry-run mode (show diff), confirm mode (write)
- [ ] `writeback_queue` populated from named persons
- [ ] Backup: ExifTool `_original` on first write per file only
- [ ] UI: `WritebackPage` — file list preview, confirm button, progress
- [ ] Status tracking: pending → dry_run → confirmed → written / failed
- [ ] Post-write Spotlight verification: `mdfind PersonInImage == "Name"` test

### Phase 6 — Object, Scene & OCR (Week 12–15)
**Goal:** Search beyond people — objects, places, text.

- [ ] Object detection: YOLO or embedding similarity approach (decide in Phase 5)
- [ ] Scene classification: evaluate Apple Vision framework first (on-device, zero deps)
- [ ] OCR: evaluate Apple Vision OCR before Tesseract (faster, more accurate on M-series)
- [ ] DB: `tags` table, linked to `media_files`
- [ ] UI: filter by object, scene, text content

### Phase 7 — Advanced Search (Week 15–18)
**Goal:** Google Photos-level querying, fully offline.

- [ ] Hybrid keyword + vector search
- [ ] Natural language query parser
- [ ] Query examples: "X and Y together", "beach before 2018", "text containing Invoice"
- [ ] Cached query results

---

## 8. System Invariants (Never Violate These)

1. **Originals are never modified without explicit user confirmation.** The UI names people in the DB only. ExifTool runs only when user presses "Write to Files" and confirms the dry-run preview.
2. **Embeddings are never deleted.** Even if a face is re-detected or a cluster is split, old embedding rows stay. Add `model_version` column to differentiate.
3. **SHA-256 hash is the identity of a file**, not its path. File moves are handled by updating the path, not creating a new record.
4. **iCloud stubs are detected before any read attempt.** A stub opened as a full read triggers an iCloud download silently — unexpected, slow, and wrong.
5. **No network calls from any component.** No telemetry, no model download at runtime (all models downloaded once by `setup.sh`).
6. **Writeback is idempotent.** Writing the same names again should produce the same file state. ExifTool `-overwrite_original` after first backup ensures this.
7. **Re-evaluation never silently discards names.** If a file is re-embedded and gets a new cluster assignment, its existing person name is preserved and the operator must explicitly resolve conflicts.

---

## 9. Performance Targets (M2 Max, 64GB)

| Operation | Target Throughput | Notes |
|---|---|---|
| Stub detection + hash | 500+ files/sec | I/O bound, parallelise with `asyncio` |
| EXIF extraction | 200+ files/sec | ExifTool batch mode (`-stay_open`) |
| JPEG preview extraction | 100+ files/sec | ExifTool, batched |
| Face detection (RetinaFace) | 30–50 images/sec | MLX, batch size 8–16 |
| Face embedding (ArcFace) | 50–100 faces/sec | MLX, batch size 32 |
| FAISS index query (1-NN) | <5ms | Flat index at 200K vectors |
| HDBSCAN cluster (200K pts) | <60 sec | One-time, not per query |
| DB write throughput | 1000+ rows/sec | WAL mode, batched inserts |

---

## 10. Key Configuration Constants (`config.py`)

```python
APP_NAME = "VIP"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "VIP"
DB_PATH = APP_SUPPORT_DIR / "vip.db"
FAISS_PATH = APP_SUPPORT_DIR / "vip.faiss"
THUMBNAIL_DIR = APP_SUPPORT_DIR / "thumbnails"
PREVIEW_DIR = APP_SUPPORT_DIR / "previews"   # temp, cleared after embedding

API_HOST = "127.0.0.1"
API_PORT = 7474

SUPPORTED_FORMATS = {".cr3", ".arw", ".nef", ".dng", ".rw2", ".orf"}

# InsightFace
INSIGHTFACE_MODEL = "buffalo_l"
EMBEDDING_DIM = 512
FACE_DETECTION_THRESHOLD = 0.5     # RetinaFace min confidence
MIN_FACE_SIZE_PX = 40               # ignore tiny faces

# Clustering
HDBSCAN_MIN_CLUSTER_SIZE = 5
HIGH_CONFIDENCE_THRESHOLD = 0.92    # cosine similarity — tune in Phase 0
CLUSTER_INERTIA_THRESHOLD = 0.85    # below this = uncertain cluster

# iCloud stub detection
STUB_MAX_SIZE_BYTES = 4096          # files smaller than this for known RAW types

# Batch sizes
EMBED_BATCH_SIZE = 32
EXIF_BATCH_SIZE = 100               # ExifTool stay_open batch

# ExifTool
EXIFTOOL_TIMEOUT_SEC = 30
EXIFTOOL_WRITE_BACKUP = True        # enable _original backup on first write
```

---

## 11. API Surface (Summary)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/pipeline/scan` | Start scan on folder path |
| `GET` | `/api/pipeline/status` | Current pipeline status |
| `WS` | `/ws/progress` | Real-time progress events |
| `GET` | `/api/persons` | All persons (named + unnamed clusters) |
| `GET` | `/api/persons/{id}/faces` | Face tiles for a person/cluster |
| `PATCH` | `/api/persons/{id}` | Set name, merge, split |
| `GET` | `/api/media/{id}` | Media file metadata |
| `GET` | `/api/media/{id}/preview` | Serve JPEG preview |
| `GET` | `/api/faces/{id}/thumbnail` | Serve face crop JPEG |
| `POST` | `/api/search` | Search query |
| `GET` | `/api/writeback/preview` | Dry-run: list of pending writes |
| `POST` | `/api/writeback/confirm` | Execute ExifTool writes |
| `GET` | `/api/writeback/status` | Writeback job status |

---

## 12. Open Questions / Deferred Decisions

| # | Question | Deferred To | Notes |
|---|---|---|---|
| 1 | Videos: keyframe-only vs. fixed-rate vs. scene-change sampling? | Phase 1 end | Depends on HW headroom after photos done |
| 2 | OCR: Apple Vision vs. Tesseract vs. TrOCR? | Phase 6 | Evaluate Apple Vision first on M-series |
| 3 | Object detection: YOLO variant vs. CLIP embedding similarity? | Phase 6 | CLIP may unify object + scene without two models |
| 4 | FAISS: Flat vs. IVF? | Phase 3 | Flat sufficient up to ~300K vectors; benchmark first |
| 5 | Thermal throttle: temperature threshold for backing off ML jobs? | Phase 2 | Need to profile M2 Max sustained inference temps |
| 6 | App name in UI displayed to user | Before Phase 4 UI | Owner to confirm — "VIP", "Visual Intelligence Platform", or other |
| 7 | Should re-evaluated files that produce new clusters prompt the user to re-confirm existing names? | Phase 3 | Design needed — conflict resolution flow |

---

## 13. Setup Plan (`setup.sh` outline)

```bash
#!/bin/bash
# VIP setup — Apple Silicon macOS only
set -e

echo "=== VIP Setup ==="

# 1. Homebrew dependencies
brew install exiftool ffmpeg

# 2. Python 3.11+ via pyenv or system check
python3.11 -m venv .venv
source .venv/bin/activate

# 3. Python packages
pip install --upgrade pip
pip install mlx insightface fastapi uvicorn aiosqlite faiss-cpu hdbscan scikit-learn rawpy pillow

# 4. Download InsightFace Buffalo_L model (one-time)
python -c "import insightface; insightface.app.FaceAnalysis(name='buffalo_l').prepare(ctx_id=0)"

# 5. Frontend
cd frontend && npm install && cd ..

# 6. Init DB
python backend/database/db.py --init

echo ""
echo "=== Setup complete ==="
echo "Run: ./start.sh"
```

---

## 14. Implementation State Tracker

> Update this section at the end of every working session.

| Component | Status | Notes |
|---|---|---|
| Git repo + remote | ✅ Done | main branch, remote linked |
| .gitignore | ✅ Done | macOS, Node, ML, media, project-specific |
| SOLUTION_DESIGN.md | ✅ Done | This document |
| setup.sh / start.sh | ✅ Done | One-command bootstrap + launcher |
| requirements.txt | ✅ Done | All Python deps listed |
| backend/config.py | ✅ Done | All constants via Pydantic Settings |
| backend/main.py | ✅ Done | FastAPI app, CORS, lifespan, routers |
| DB schema + migrations | ✅ Done | 001_initial.sql — 7 tables + indices |
| backend/database/models.py | ✅ Done | Pydantic response models |
| backend/scanner/ | ✅ Done | walker, hasher, exif_reader, preview_extractor |
| backend/ml/ | ✅ Done | face_detector, embedder, clusterer, FAISS index |
| backend/pipeline/ingest.py | ✅ Done | 3-phase orchestrator (scan→embed→cluster) |
| backend/api/websocket.py | ✅ Done | WebSocket progress broadcaster |
| backend/api/routes/ | ✅ Done | pipeline, persons, faces, media, search, writeback |
| backend/writeback/ | ✅ Done | exiftool writer, XMP fields, dry-run engine |
| scripts/ | ✅ Done | benchmark.py, reset_db.py |
| Frontend scaffold | ✅ Done | Vite + React 18 + Tailwind CSS v4, proxy config |
| frontend/src/api/client.ts | ✅ Done | Fully-typed API client |
| frontend pages | ✅ Done | PeoplePage, PipelinePage, SearchPage, WritebackPage |
| Phase 0 benchmarks | 🔲 Not started | Requires real CR3 files — run locally |
| Phase 1: Scanner (E2E test) | 🔲 Not started | Wire up & test on real library |
| Phase 2: ML pipeline (E2E) | 🔲 Not started | Validate InsightFace output |
| Phase 3: Clustering (E2E) | 🔲 Not started | Tune HDBSCAN params |
| Phase 4: Web UI polish | 🔲 Not started | Error states, pagination, thumbnails |
| Phase 5: Writeback (E2E) | 🔲 Not started | Test ExifTool atomic write on CR3 |
| Phase 6: Object/OCR | 🔲 Not started | Deferred — post Phase 5 |
| Phase 7: Search (E2E) | 🔲 Not started | FAISS ANN + SQLite keyword |

**Last updated:** 2025-07-14 — Phase 0 complete; all skeleton code committed and pushed.
