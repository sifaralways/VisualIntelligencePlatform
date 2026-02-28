# VIP — Visual Intelligence Platform
## Solution Design Document
**Version:** 0.3  
**Status:** Living Document — update after every significant decision or implementation milestone  
**Last Updated:** 2026-02-28  
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
- **Face detection runtime:** InsightFace Buffalo_L via ONNX Runtime — **CPUExecutionProvider only**. CoreML EP was dropped due to a shape-rank mismatch in `det_10g.onnx` when `det_size=(1280,1280)` is used. CPU EP is stable and correct.
- **Object / scene / landmark / species runtime:** PyTorch (MPS backend on Apple Silicon). MLX was evaluated and dropped in favour of the standard PyTorch ecosystem for broader model support.
- **Face detection model:** InsightFace Buffalo_L (RetinaFace detector + ArcFace-style 512-D embeddings). Detection size `(1280,1280)`. Min face size `20px`. Confidence threshold `0.5`.
- **Embedding dimension:** 512-D float32 vectors.
- **Clustering algorithm:** HDBSCAN (`hdbscan` package). `min_cluster_size=2` (calibrated for small family libraries). Runs silently; user never sees "clusters".
- **Object detection:** YOLOv11s (ultralytics) — COCO 80 classes, MPS backend. Confidence threshold `0.40`. Auto-downloads `yolo11s.pt` (~21MB) on first run.
- **Scene / geography classification:** Places365 ResNet-50 — 365 scene categories mapped to geography and place ontologies. Auto-downloads (~100MB) on first run.
- **Landmark recognition:** OpenCLIP ViT-B/32 — zero-shot recognition of 56 world landmarks. Threshold `0.26`.
- **Species classification:** BioCLIP — 150+ animal species via zero-shot CLIP. Only invoked when YOLO detects an animal. Threshold `0.30`.
- **Geo resolution:** Nominatim (OpenStreetMap, via geopy) — GPS lat/lon → human-readable place name. User-agent: `VIP-VisualIntelligencePlatform/1.0`.
- **No cloud ML, no network calls at inference time.** Nominatim is the only external call; it is OSM-based and privacy-safe.

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
| Language | Python | 3.11+ | async support, ML ecosystem |
| ML — Face Detection | InsightFace (RetinaFace) | latest | Best accuracy, non-commercial free |
| ML — Face Embedding | InsightFace (ArcFace Buffalo_L) | latest | 512-D, SOTA, non-commercial free |
| Face ML Runtime | ONNX Runtime (CPUExecutionProvider) | 1.24.2 | CoreML EP dropped (shape mismatch with det_size=1280) |
| Object/Animal Detection | YOLOv11s (ultralytics) | latest | COCO 80 classes, MPS backend |
| Scene Classification | Places365 ResNet-50 | latest | 365 scene categories, PyTorch MPS |
| Landmark Recognition | OpenCLIP ViT-B/32 | latest | Zero-shot, 56 landmarks |
| Species Classification | BioCLIP | latest | 150+ animal species, zero-shot CLIP |
| Object/Scene Runtime | PyTorch | 2.2+ | MPS backend for Apple Silicon acceleration |
| Geo Resolution | Nominatim / geopy | latest | GPS → place name via OpenStreetMap |
| Clustering | HDBSCAN | `hdbscan` package | Density-based, handles noise, min_cluster_size=2 |
| Vector Index | FAISS | latest | Fast ANN search, flat index |
| RAW preview extract | ExifTool | 12+ | Tries LargePreviewImage → PreviewImage → JpgFromRaw |
| RAW display decode | rawpy + LibRaw | latest | On-demand only for UI display |
| EXIF read | ExifTool (JSON mode) | 12+ | Most complete EXIF/XMP reader |
| Metadata write | ExifTool | 12+ | Atomic write, CR3-safe, MWG regions, `-TAG=` clear-before-write |
| Database | SQLite | via `aiosqlite` | No server, WAL mode, FK constraints |
| API | FastAPI | latest | Async, WebSocket, auto-docs |
| Frontend | React + Vite | React 18 / Vite 5 | Lightweight, no SSR needed |
| Styling | Tailwind CSS | v4 | Utility-first, fast iteration |
| Image serving | FastAPI static / streaming | — | Serve face crops + thumbnails locally |
| Job orchestration | Python `asyncio` + queue | — | Single machine, no Celery needed |
| Setup | `setup.sh` (Homebrew + pip + npm) | — | One-command install for friends |
| Logging | Python `logging` + RotatingFileHandler | — | `~/Library/Logs/VIP/vip.log`, 10MB×5 |

### Dropped / Deferred
- **MLX:** Evaluated; dropped in favour of PyTorch (MPS) for broader model support (YOLO, CLIP, Places365).
- **CoreML EP:** Dropped. ONNX CoreML EP fails on `det_10g.onnx` with `det_size=(1280,1280)` — shape rank mismatch. CPU EP used instead.
- **DuckDB:** Deferred. SQLite sufficient at current scale. Revisit if >500K files.
- **Docker:** Not used. Native macOS install via `setup.sh`.
- **Celery / RQ:** Not needed. `asyncio` + background task queue sufficient.
- **Tesseract / TrOCR:** Phase 6 only. Apple Vision OCR to be evaluated first.
- **ExifTool stay_open mode:** Replaced with per-file `subprocess.run` (30s timeout). More reliable for RAW containers.

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

### 5.8 `media_tags` ← Added Phase 4
```sql
CREATE TABLE IF NOT EXISTS media_tags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id   INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
    category        TEXT    NOT NULL,   -- 'object' | 'animal' | 'geography' | 'place'
    label           TEXT    NOT NULL,
    confidence      REAL,
    model           TEXT    NOT NULL,   -- 'yolov11' | 'places365' | 'clip' | 'bioclip' | 'nominatim'
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (media_file_id, category, label)
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

### Phase 0 — Foundations ← ✅ COMPLETE
**Goal:** Repo structure, environment, schema, model validation.

- [x] Git repo linked to remote
- [x] `.gitignore` complete
- [x] `setup.sh` written and tested on M2 Mac
- [x] Backend skeleton: FastAPI + aiosqlite + DB migrations
- [x] Frontend skeleton: React + Vite + Tailwind CSS v4
- [x] InsightFace Buffalo_L loaded via ONNX CPU EP — confirmed initialises
- [ ] Benchmark: extract embedded JPEG from 100 CR3s, measure throughput _(requires real library)_
- [ ] Benchmark: run face embed on 1,000 faces, measure time + memory _(requires real library)_
- [ ] Confirm iCloud stub detection with real stub file

### Phase 1 — Media Ingest & Catalog ← ✅ COMPLETE
**Goal:** Safely scan, hash, catalog, and extract previews from 100K files.

- [x] `walker.py` — recursive walk, iCloud stub detection, format filtering
- [x] `hasher.py` — SHA-256, idempotency check against DB
- [x] `exif_reader.py` — ExifTool JSON wrapper, extract date/GPS/camera/dims
- [x] `preview_extractor.py` — tries LargePreviewImage → PreviewImage → JpgFromRaw (no -fast flags)
- [x] DB migration 001: `media_files`, `scan_state` + 5 other tables
- [x] `scan_state` tracking — resume after interruption
- [x] API: `POST /pipeline/scan`, `GET /pipeline/status` (WebSocket)
- [ ] E2E test on real library _(pending)_

### Phase 2 — Face Detection & Embeddings ← ✅ COMPLETE
**Goal:** Detect faces in all preview JPEGs, produce 512-D embeddings.

- [x] `face_detector.py` — RetinaFace via InsightFace, CPUExecutionProvider only, det_size=1280, min_face=20px
- [x] `embedder.py` — ArcFace Buffalo_L; 200×200 thumbnails, 35% context padding, LANCZOS
- [x] Face crop thumbnails saved to `data/thumbnails/`
- [x] DB: `faces`, `embeddings` tables
- [x] Progress via WebSocket
- [x] Previews kept during pipeline, deleted after Phase 4 tagging

### Phase 3 — Vector Indexing & Clustering ← ✅ COMPLETE
**Goal:** Faces grouped into person clusters automatically.

- [x] FAISS flat index for <100K embeddings
- [x] `index.py` — save/load `.faiss` file, add/query
- [x] `clusterer.py` — HDBSCAN min_cluster_size=2; wipes unnamed clusters before re-run
- [x] DB: `clusters`, `persons` (unnamed)
- [x] Cluster merge/split logic

### Phase 4 — ML Tagging (Objects, Scenes, Landmarks, Species, Geo) ← ✅ COMPLETE
**Goal:** Enrich every photo with automatic tags across 4 categories.

- [x] `object_detector.py` — YOLOv11s (COCO), MPS, conf=0.40; skips "person"; flags animals
- [x] `scene_classifier.py` — Places365 ResNet-50, MPS, top-k=5; maps to geography/place
- [x] `landmark_recogniser.py` — OpenCLIP ViT-B/32, 56 world landmarks, threshold=0.26
- [x] `species_classifier.py` — BioCLIP, 150+ species, threshold=0.30; only runs for animal detections
- [x] `geo_resolver.py` — Nominatim/geopy; GPS → "Darling Harbour, Sydney, Australia"
- [x] `tagger.py` — orchestrator for all 5 models
- [x] DB migration 002: `media_tags` table
- [x] `ingest.py` Phase 4 (`_phase_tag`) — runs after cluster, updates state to `tagged`
- [x] API: `GET /api/tags/{id}`, `GET /api/tags/summary/top`
- [ ] E2E test on real library _(models auto-download ~800MB on first run)_

### Phase 4b — Web UI: People & Naming ← ✅ COMPLETE
**Goal:** User can browse face tiles, name people, manage merges, review faces.

- [x] `PeoplePage` — face tile grid, sorted by photo count; shows real face thumbnail for named persons
- [x] Name input → same name detection → merge dialog
- [x] Person UUID assigned on naming
- [x] Face review panel — click named person tile → see all face crops → ✕ eject misassigned faces
- [x] `DELETE /api/faces/{id}/from-person` — eject endpoint
- [x] `AdminPage` — stats + scoped reset (faces/clusters/tags/all, FK-safe)
- [x] DB updates on name/merge/rename

### Phase 5 — Metadata Writeback ← ✅ COMPLETE
**Goal:** Names + tags written into original CR3 files safely.

- [x] `exiftool.py` — per-file subprocess, 30s timeout, `-TAG=` clear-before-write (no duplicate accumulation)
- [x] `fields.py` — XMP field map: `PersonInImage`, `Subject`, `Keywords` (obj:/animal:/geo:/place: prefixed), MWG `RegionInfo`, `Location`
- [x] `engine.py` — dry-run, confirm, rollback; pulls from `persons` + `media_tags`
- [x] `writeback_queue` populated from named persons
- [x] Backup: ExifTool `_original` on first write per file
- [x] UI: `WritebackPage` — file list preview, confirm button, progress
- [ ] E2E test on real CR3 file _(pending)_
- [ ] Post-write Spotlight verification: `mdfind PersonInImage == "Name"` _(pending)_

### Phase 6 — OCR & Text Search ← 🔲 Deferred
**Goal:** Search beyond people — text content in photos.

- [ ] OCR: evaluate Apple Vision OCR before Tesseract
- [ ] DB: `ocr_results` table
- [ ] UI: filter by text content

### Phase 7 — Advanced Search ← 🔲 Deferred
**Goal:** Google Photos-level querying, fully offline.

- [ ] Tag-based search: filter by obj:/geo:/place: categories
- [ ] Hybrid keyword + vector search
- [ ] Natural language query parser
- [ ] Query examples: "X and Y at the beach", "beach before 2018 with a dog"

---

## 8. System Invariants (Never Violate These)

1. **Originals are never modified without explicit user confirmation.** The UI names people in the DB only. ExifTool runs only when user presses "Write to Files" and confirms the dry-run preview.
2. **Embeddings are never deleted.** Even if a face is re-detected or a cluster is split, old embedding rows stay. Add `model_version` column to differentiate.
3. **SHA-256 hash is the identity of a file**, not its path. File moves are handled by updating the path, not creating a new record.
4. **iCloud stubs are detected before any read attempt.** A stub opened as a full read triggers an iCloud download silently — unexpected, slow, and wrong.
5. **No telemetry, no cloud ML.** All inference runs on-device. The only network call is Nominatim (OpenStreetMap) for GPS → place name resolution — this is privacy-safe and OSM-based. No image data ever leaves the machine.
6. **Writeback is idempotent.** Writing the same names again should produce the same file state. ExifTool `-overwrite_original` after first backup ensures this.
7. **Re-evaluation never silently discards names.** If a file is re-embedded and gets a new cluster assignment, its existing person name is preserved and the operator must explicitly resolve conflicts.

---

## 9. Performance Targets (M2 Max, 64GB)

| Operation | Target Throughput | Notes |
|---|---|---|
| Stub detection + hash | 500+ files/sec | I/O bound, parallelise with `asyncio` |
| EXIF extraction | 200+ files/sec | ExifTool per-file in executor |
| JPEG preview extraction | 100+ files/sec | ExifTool per-file, tries LargePreviewImage first |
| Face detection (RetinaFace) | 30–50 images/sec | ONNX CPU EP, det_size=1280 |
| Face embedding (ArcFace) | 50–100 faces/sec | ONNX CPU EP, batch size 32 |
| Object detection (YOLOv11) | 20–40 images/sec | PyTorch MPS |
| Scene classification (Places365) | 30–60 images/sec | PyTorch MPS |
| CLIP landmark recognition | 20–40 images/sec | PyTorch MPS |
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
PREVIEW_DIR = APP_SUPPORT_DIR / "previews"   # temp per-file; deleted after Phase 4 tagging

API_HOST = "127.0.0.1"
API_PORT = 7474

SUPPORTED_FORMATS = {".cr3", ".arw", ".nef", ".dng", ".rw2", ".orf"}

# InsightFace
INSIGHTFACE_MODEL = "buffalo_l"
EMBEDDING_DIM = 512
FACE_DETECTION_THRESHOLD = 0.5     # RetinaFace min confidence
MIN_FACE_SIZE_PX = 20              # minimum face height in pixels (was 40, reduced for recall)
FACE_DET_SIZE = (1280, 1280)       # detection input resolution; CPUExecutionProvider only
THUMBNAIL_PADDING = 0.35          # context padding around face bbox (35%)
THUMBNAIL_SIZE = (200, 200)       # saved face crop size

# Clustering
HDBSCAN_MIN_CLUSTER_SIZE = 2       # calibrated for small family libraries
HIGH_CONFIDENCE_THRESHOLD = 0.92
CLUSTER_INERTIA_THRESHOLD = 0.85

# Phase 4 — ML Tagging
yolo_conf_threshold: float = 0.40  # YOLOv11 detection confidence
landmark_threshold: float = 0.26   # OpenCLIP cosine similarity
species_threshold: float = 0.30    # BioCLIP cosine similarity
places365_top_k: int = 5           # top-k scene categories from Places365

# iCloud stub detection
STUB_MAX_SIZE_BYTES = 4096

# Batch sizes
EMBED_BATCH_SIZE = 32

# ExifTool
EXIFTOOL_TIMEOUT_SEC = 30
EXIFTOOL_WRITE_BACKUP = True       # enable _original backup on first write per file
```

---

## 11. API Surface (Summary)

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/pipeline/scan` | Start pipeline (all 4 phases) |
| `GET` | `/api/pipeline/status` | Current pipeline status |
| `WS` | `/ws/progress` | Real-time progress events |
| `GET` | `/api/persons` | All persons with photo counts + representative thumbnail |
| `GET` | `/api/persons/{id}/faces` | All face crops for a person (for review panel) |
| `PATCH` | `/api/persons/{id}` | Set name, merge, split |
| `DELETE` | `/api/faces/{id}/from-person` | Eject a misassigned face from a person |
| `GET` | `/api/media/{id}` | Media file metadata |
| `GET` | `/api/media/{id}/preview` | Serve JPEG preview |
| `GET` | `/api/faces/{id}/thumbnail` | Serve face crop JPEG (200×200) |
| `POST` | `/api/search` | Search query |
| `GET` | `/api/writeback/preview` | Dry-run: list of pending writes + field diffs |
| `POST` | `/api/writeback/confirm` | Execute ExifTool writes |
| `GET` | `/api/writeback/status` | Writeback job status |
| `GET` | `/api/tags/{media_file_id}` | ML tags for a photo, grouped by category |
| `GET` | `/api/tags/summary/top` | Most frequent tags across library (filterable by category) |
| `GET` | `/api/admin/stats` | DB row counts + pipeline state breakdown |
| `POST` | `/api/admin/reset/{scope}` | Scoped reset: faces / clusters / tags / all (FK-safe) |

---

## 12. Open Questions / Deferred Decisions

| # | Question | Deferred To | Notes |
|---|---|---|---|
| 1 | Videos: keyframe-only vs. fixed-rate vs. scene-change sampling? | Phase 1 end | Depends on HW headroom after photos done |
| 2 | OCR: Apple Vision vs. Tesseract vs. TrOCR? | Phase 6 | Evaluate Apple Vision first on M-series |
| 3 | ~~Object detection: YOLO variant vs. CLIP embedding similarity?~~ | ~~Phase 6~~ | **Resolved:** Both used. YOLO for object/animal bbox; CLIP for zero-shot landmarks |
| 4 | FAISS: Flat vs. IVF? | Phase 3 | Flat sufficient up to ~300K vectors; benchmark first |
| 5 | Thermal throttle: temperature threshold for backing off ML jobs? | Phase 2 | Need to profile M2 Max sustained inference temps |
| 6 | App name in UI displayed to user | Before Phase 4 UI | Owner to confirm — "VIP", "Visual Intelligence Platform", or other |
| 7 | Should re-evaluated files that produce new clusters prompt the user to re-confirm existing names? | Phase 3 | Design needed — conflict resolution flow |
| 8 | Species/landmark threshold tuning against real library? | After Phase 4 E2E | BioCLIP at 0.30, landmark at 0.26 — may need empirical adjustment |
| 9 | Frontend: UI to display ML tags (object chips, scene chips)? | Next sprint | `/api/tags/{id}` exists; no frontend display page yet |
| 10 | Search integration: filter by tag category + label? | Next sprint | Enables queries like "beach with a dog" |

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
| README.md | ✅ Done | Full project overview, setup, API, structure |
| SOLUTION_DESIGN.md | ✅ Done | v0.3 — this document |
| setup.sh / start.sh | ✅ Done | One-command bootstrap + launcher |
| requirements.txt | ✅ Done | All Python deps incl. ultralytics, torch, open-clip-torch, geopy |
| backend/config.py | ✅ Done | All constants via Pydantic Settings; Phase 4 thresholds added |
| backend/main.py | ✅ Done | FastAPI app, CORS, lifespan, all routers registered |
| DB migration 001 | ✅ Done | 7 core tables + indices |
| DB migration 002 | ✅ Done | media_tags table (Phase 4) |
| backend/database/models.py | ✅ Done | Pydantic response models |
| backend/scanner/ | ✅ Done | walker, hasher, exif_reader; preview_extractor tries LargePreviewImage→PreviewImage→JpgFromRaw |
| backend/ml/face_detector.py | ✅ Done | InsightFace Buffalo_L, CPU EP only, det_size=1280, min_face=20px |
| backend/ml/embedder.py | ✅ Done | 200×200 thumbnails, 35% padding, LANCZOS |
| backend/ml/clusterer.py | ✅ Done | HDBSCAN min_cluster_size=2; wipes unnamed clusters before re-run |
| backend/ml/object_detector.py | ✅ Done | YOLOv11s, MPS, COCO 80 classes, skips "person", flags is_animal |
| backend/ml/scene_classifier.py | ✅ Done | Places365 ResNet-50, MPS, geography + place ontology mapping |
| backend/ml/landmark_recogniser.py | ✅ Done | OpenCLIP ViT-B/32, 56 landmarks, threshold=0.26 |
| backend/ml/species_classifier.py | ✅ Done | BioCLIP, 150+ species, threshold=0.30 |
| backend/ml/geo_resolver.py | ✅ Done | Nominatim/geopy, GPS→place name |
| backend/ml/tagger.py | ✅ Done | Orchestrator for all 5 tagging models |
| backend/pipeline/ingest.py | ✅ Done | 4-phase orchestrator: scan→embed→cluster→tag |
| backend/api/websocket.py | ✅ Done | WebSocket progress broadcaster |
| backend/api/routes/pipeline.py | ✅ Done | Trigger scan, WebSocket progress |
| backend/api/routes/persons.py | ✅ Done | List (explicit columns, no photo_count collision), name, merge, GET/{id}/faces |
| backend/api/routes/faces.py | ✅ Done | Serve thumbnails; DELETE /{id}/from-person eject |
| backend/api/routes/media.py | ✅ Done | Media file metadata + preview serve |
| backend/api/routes/search.py | ✅ Done | Keyword search |
| backend/api/routes/writeback.py | ✅ Done | Dry-run + confirm + status |
| backend/api/routes/tags.py | ✅ Done | GET /api/tags/{id}, GET /api/tags/summary/top |
| backend/api/routes/admin.py | ✅ Done | Stats + scoped reset (FK-safe) |
| backend/writeback/exiftool.py | ✅ Done | Per-file subprocess, 30s timeout, -TAG= clear-before-write (no duplicates) |
| backend/writeback/fields.py | ✅ Done | XMP map; obj:/animal:/geo:/place: prefixed keywords |
| backend/writeback/engine.py | ✅ Done | Dry-run, confirm, rollback; queries media_tags + persons |
| scripts/ | ✅ Done | benchmark.py, reset_db.py |
| Frontend scaffold | ✅ Done | Vite + React 18 + Tailwind CSS v4, proxy config |
| frontend/src/api/client.ts | ✅ Done | Typed client incl. tags, faces.byPerson, faces.removeFromPerson |
| frontend/PeoplePage.tsx | ✅ Done | Face tiles, naming, face review panel, per-face ✕ eject button |
| frontend/PipelinePage.tsx | ✅ Done | Scan controls + live WebSocket progress |
| frontend/SearchPage.tsx | ✅ Done | Keyword search UI |
| frontend/WritebackPage.tsx | ✅ Done | Dry-run preview + confirm |
| frontend/AdminPage.tsx | ✅ Done | Stats + scoped reset controls |
| File logging | ✅ Done | ~/Library/Logs/VIP/vip.log, rotating 10MB×5 |
| Phase 0 benchmarks | 🔲 Not started | Requires real CR3 files — run locally |
| Phase 1–4: E2E test on real library | 🔲 Not started | Run full pipeline against photo library |
| Phase 4: Model first-run downloads | 🔲 Pending first run | ~800MB total auto-downloaded from YOLO/HuggingFace/OSM |
| Frontend: Tags display page | 🔲 Not started | /api/tags/{id} exists; no UI yet |
| Search: tag-based filtering | 🔲 Not started | Filter by obj:/geo:/place: prefix |
| Phase 6: OCR | 🔲 Deferred | Evaluate Apple Vision OCR |
| Videos | 🔲 Deferred | Post Phase 5 |

**Last updated:** 2026-02-28 — Phase 4 ML tagging pipeline complete (YOLOv11 + Places365 + OpenCLIP + BioCLIP + Nominatim). Writeback engine updated with tag categories. Face review UI with per-face eject. Admin page. All code committed and pushed (commit `b425e9d`).
