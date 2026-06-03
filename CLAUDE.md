# Project Overview
Visual Intelligence Platform (VIP) is a local-first photo intelligence system for large personal media libraries. It ingests image/RAW folders, detects and clusters faces, generates tags/captions/OCR signals, supports assistant-driven retrieval, and writes curated metadata back to files. Primary users are individual photo-library owners (and operators managing profile-scoped libraries) who want offline organization, search, and review workflows.

File repomix-output.xml contains all the files in the repository combined into one.

# Tech Stack
- Backend language/runtime: Python 3.11 (setup default via `PYTHON=python3.11` in `setup.sh`).
- Backend framework: FastAPI (un-pinned), uvicorn[standard] (un-pinned).
- DB: SQLite + `aiosqlite` (WAL mode, migrations in `backend/database/migrations`).
- Backend validation/config: Pydantic, pydantic-settings.
- Frontend language/runtime: TypeScript `~5.9.3`, Node.js (version not pinned in repo; `setup.sh` requires `node` command).
- Frontend framework/build: React `^19.2.0`, React DOM `^19.2.0`, Vite `^7.3.1`, Tailwind CSS `^4.2.1`, ESLint 9 stack.
- Core ML libraries (backend):
  - InsightFace (face detection/embeddings)
  - onnxruntime `>=1.20`
  - FAISS (`faiss-cpu`)
  - scikit-learn `>=1.3,<1.5`
  - Ultralytics (YOLO)
  - torch `>=2.2`, torchvision `>=0.17`
  - open-clip-torch
  - nudenet `>=3.4`
  - timm `>=0.9`
  - huggingface_hub `>=0.20`
  - transformers `==4.46.3` (explicitly pinned for Florence compatibility)
  - sentencepiece `>=0.2`, einops `>=0.8`
  - rawpy, Pillow, numpy
- Geo: geopy (Nominatim reverse geocoding).
- Writeback tooling: ExifTool (system dependency installed via Homebrew).

Non-standard/unusual setup details:
- Runtime dependency bootstrap in `backend/main.py`: on startup, missing/unsatisfied Python packages from `requirements.txt` may be auto-installed unless `VIP_AUTO_INSTALL_DEPS=0`.
- Profile-scoped runtime storage under `~/Library/Application Support/VIP/profiles/<profile_id>/` (not repo-local state).
- Startup scripts are macOS/Apple Silicon oriented (`setup.sh` checks `arm64`, installs Homebrew packages).
- Dev launcher (`start.sh`) runs backend with `uvicorn --reload` and exports `TOKENIZERS_PARALLELISM=false`.

# Architecture
## Top-level directories
- `backend/`: FastAPI app, pipeline orchestration, ML modules, DB layer, assistant logic, writeback engine.
- `frontend/`: React/Vite single-page app and typed API client.
- `scripts/`: operational helper scripts (reset, benchmark, metadata cleanup, contacts matching, etc.).
- `Sample Files for analysis output/`: sample JSON outputs.
- `DB migration/`: project artifact folder; actual applied SQL migrations are in `backend/database/migrations/`.

## Key backend subdirectories
- `backend/api/routes/`: HTTP route modules (`media`, `persons`, `faces`, `pipeline`, `chat`, `writeback`, `settings`, `folders`, `remote`, etc.).
- `backend/api/websocket.py`: `/ws/progress` WebSocket connection registry + event broadcast.
- `backend/pipeline/`: ingest orchestration and runtime state/control.
- `backend/ml/`: model wrappers and feature extractors (detectors, analyzers, classifiers, index helpers).
- `backend/database/`: DB connection/migrations/settings store and model classes.
- `backend/assistant/` and `backend/assistant_v2/`: assistant planner/executor/orchestrator stacks.
- `backend/scanner/`: file walk/hash/exif/preview extraction.
- `backend/writeback/`: ExifTool-based metadata writeback pipeline.
- `backend/runtime/`: runtime activity/state helpers.

## Key entry points and bootstrapping
- Backend app entry: `backend/main.py`
  - Sets up logging and middleware.
  - Initializes profile/bootstrap + DB migrations in lifespan startup.
  - Starts warm model loading and suggestion worker tasks.
  - Registers all API routers and `/ws/progress`.
- Frontend entry: `frontend/src/main.tsx` -> renders `frontend/src/App.tsx`.
- Frontend API boundary: `frontend/src/api/client.ts` (typed wrappers, profile header injection).
- Local startup script: `start.sh` (backend + frontend dev mode).
- Initial setup script: `setup.sh` (system deps, venv, pip/npm installs, DB init).

## End-to-end data flow
1. UI events in `App.tsx`/pages call typed API methods in `frontend/src/api/client.ts`.
2. API client injects `X-VIP-Profile` header from local storage profile id.
3. `backend/main.py` profile middleware resolves profile context and sets per-request/current-profile state.
4. Route handlers in `backend/api/routes/*` call domain services (pipeline, DB settings, writeback, assistant).
5. Long-running pipeline operations in `backend/pipeline/ingest.py` update SQLite, enqueue writeback, and broadcast progress events over `/ws/progress` via `backend/api/websocket.py`.
6. Frontend listens on WebSocket and updates UI state for pipeline progress, quality issues, and suggestion notifications.

## Non-obvious architectural decisions
- Profile isolation is contextvar-based and request-scoped; most runtime paths derive from active profile.
- Pipeline uses module-level singleton model/index objects (`_detector`, `_faiss`, `_florence`, etc.) loaded once per process.
- Settings are DB-backed (`app_settings`) with in-process cache refresh; thresholds/toggles are intended to be runtime-tunable.
- Soft-delete model for library removals (`removed_from_app`) is used in many queries to hide media without deleting source files.

# Domain Model
Core entities (from API/database models and routes):
- MediaFile: indexed source photo/RAW record (`media_files`), file metadata, ingest state, and writeback status.
- Face: detected face region linked to media, optional cluster/person assignment.
- Cluster: unnamed (or person-owned) group of similar face embeddings.
- Person: named identity composed of one or more clusters/faces; may be merged/ignored.
- WritebackQueueItem: pending/written/failed metadata write jobs per media file.
- Tags/Analysis: object/scene/species/explicit/caption/OCR/region outputs persisted and surfaced via analysis/search routes.
- Profile: isolated workspace with own DB/assets/settings.
- RemoteServer: optional remote writeback target mapping and SSH config.

Key relationships:
- One MediaFile -> many Faces.
- Faces -> belong to at most one Cluster and optionally one Person (directly/through current cluster-person mapping logic).
- Person <- many Clusters/Faces (merge/reassignment operations supported).
- MediaFile -> many tags/analysis/writeback events.
- Profile -> owns all of the above state physically via profile data directory.

Domain-specific terminology used in code/UI:
- Unnamed cluster: face cluster not yet assigned to a named person.
- High-confidence cluster: cluster above configured intra-similarity threshold.
- Suggestion worker: background quality-first generation of merge suggestions.
- Removed from app: soft-hidden media state (`removed_from_app=1`).
- Writeback: explicit metadata persistence to source files via ExifTool.
- VIP history import mode: scan mode that reuses prior VIP identifier/history when present.

# Development Commands
## Install dependencies
- Full guided setup (recommended):
  - `./setup.sh`
- Manual backend deps:
  - `python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Frontend deps:
  - `cd frontend && npm install`

## Run locally
- One-command dev run (backend + frontend):
  - `./start.sh`
- Backend only:
  - `source .venv/bin/activate && uvicorn backend.main:app --host 127.0.0.1 --port 7474 --reload`
- Frontend only:
  - `cd frontend && npm run dev`

## Tests
- Python test runner available:
  - `pytest`
- Current repo state: no discovered test files/directories under conventional patterns (`**/*test*.py`, `**/tests/**`).

## Build for production
- Frontend production bundle:
  - `cd frontend && npm run build`
- Backend has no separate compile/build artifact in repo; run via uvicorn.

## Useful quality commands
- Frontend lint:
  - `cd frontend && npm run lint`
- Frontend preview built app:
  - `cd frontend && npm run preview`

## Environment variables (verified)
- `VIP_*` (global settings namespace from `backend/config.py`): overrides any `Settings` field, e.g. `VIP_API_PORT`, `VIP_OLLAMA_BASE_URL`, `VIP_YOLO_MODEL`, `VIP_FLORENCE_ENABLED`, etc.
- `VIP_AUTO_INSTALL_DEPS`: `0/false/no` disables startup auto-install bootstrap of missing Python deps.
- `PYTHON`: setup-time selector for Python executable in `setup.sh` (default `python3.11`).
- `TOKENIZERS_PARALLELISM`: set in `start.sh` to suppress tokenizer fork warning under reload.

Required variables:
- None are strictly required for local startup because defaults exist in `backend/config.py`; optional overrides are environment-driven.

# Code Conventions
## Naming conventions
- Python: snake_case for modules/functions/variables, PascalCase for Pydantic classes.
- TypeScript/React: PascalCase components/pages, camelCase variables/functions.
- API path prefixes are grouped by domain (`/api/media`, `/api/persons`, `/api/pipeline`, etc.).

## Folder placement rules
- New backend HTTP behavior: place in `backend/api/routes/<domain>.py` and register in `backend/main.py`.
- New pipeline behavior: place in `backend/pipeline/` (mostly `ingest.py` and helpers).
- New ML model wrappers: place in `backend/ml/`.
- New DB schema changes: add SQL migration file in `backend/database/migrations/` (append-only).
- New frontend pages: `frontend/src/pages/`; reusable UI: `frontend/src/components/`; API contract changes mirrored in `frontend/src/api/client.ts` types/wrappers.

## Consistent patterns
- Async DB access through `get_db()` context manager with commit/rollback semantics.
- WebSocket progress broadcasts for long-running phases.
- Explicit writeback confirmation flow before file metadata writes.
- Profile-aware request handling via `X-VIP-Profile` + middleware context.
- Runtime-tunable knobs centralized in `backend/database/settings_store.py` defaults catalog.

## Patterns explicitly avoided (as documented in code/comments)
- Hard-coding thresholds outside settings store/config.
- Trusting shell permission checks alone for remote write probes (remote route uses real I/O probes due to macOS TCC behavior).
- In scanner EXIF path, long-lived ExifTool stay_open was previously avoided for stability; writeback engine still uses persistent writer where flow differs.

## Code Quality & Reusability
Full audit in HANDOVER_AUDIT.md. Read it before any frontend session.

### What IS shared (use these, don't rebuild)
- PhotoGrid.tsx — media grid + selection. Use this for any photo browsing UI.
- PhotoDetail.tsx — photo detail modal. Reused across 4+ pages.
- ConnectionsGraph.tsx — people/cluster graph. Already shared.
- PipelinePanel.tsx — pipeline sidebar. Use this, not PipelinePage.tsx 
  (PipelinePage is unused/stale — do not add new work there).

### What IS NOT shared but should be (known debt)
- Modal/dialog: hand-rolled overlay markup in App.tsx, PeoplePage, 
  QualityPage, AdminPage. No shared component exists yet.
- Button: no shared primitive. Tailwind class strings repeated everywhere.
- Loading/empty/error states: each page does its own.
- WebSocket hook: duplicated connection lifecycle in App.tsx, 
  PipelinePanel.tsx, PipelinePage.tsx.

### Known duplication hotspots (don't add more)
- Face/cluster mutation logic duplicated across persons.py + faces.py
- Result tile cards duplicated: SearchPage.tsx:159 ≈ AssistantPage.tsx:483
- Tag discovery duplicated: DiscoverPage.tsx ≈ TagsPage.tsx

### Anti-patterns to never introduce
- Direct fetch/axios calls outside frontend/src/api/client.ts
- Browser confirm() or alert() — use in-app modals
- Hardcoded thresholds outside settings store
- New SQL inline in route handlers without a helper for multi-step 
  person/cluster mutations

### Open technical debt (known, not blocking)
- pipeline.py:714 backend endpoint has no client.ts wrapper
- PipelinePage.tsx is unrouted/stale — do not extend it
- No backend unit tests exist


# Testing Approach
- Frameworks present: `pytest`, `pytest-asyncio`, `httpx` in requirements.
- Current test layout: no in-repo test suites found under common patterns.
- Practical implication: validation today is primarily runtime/manual (API behavior, UI flows, pipeline runs) plus lint/diagnostics.
- Running a single test file/name: supported by pytest syntax if tests are added (`pytest path/to/test_file.py -k test_name`).

# External Integrations
Critical path:
- SQLite database (local, profile-scoped) for all persistent app state.
- ExifTool CLI for metadata extraction/writeback workflows.

Optional or feature-scoped:
- Ollama local LLM endpoint (`ollama_base_url`, default `http://127.0.0.1:11434`) for assistant planning/search experiences.
- Hugging Face model hub downloads for some models (e.g., GLDv2 assets, Florence model).
- Ultralytics model auto-download behavior for YOLO weight files if missing.
- Geopy/Nominatim reverse geocoding for geo enrichment.
- Remote writeback over SSH + remote ExifTool (`/api/remote/*` wizard/CRUD).

Credentials/config management:
- Primarily environment variables (`VIP_*`) + DB-backed `app_settings` and profile registry.
- Remote SSH one-time password is accepted for key deployment endpoint and not persisted.
- Persistent remote server configs are stored in DB (`remote_servers` table).

# Known Complexity & Gotchas
- Profile scoping and context propagation: many operations depend on current profile contextvar + header middleware; bugs can manifest as cross-profile confusion if header/state is wrong.
- Pipeline orchestration (`backend/pipeline/ingest.py`) is large and phase-rich; side effects span embeddings, clustering, co-occurrence, suggestions, tagging, and writeback queue state.
- Person/cluster/face assignment logic in `backend/api/routes/persons.py` is dense and stateful; merge/suggestion/same-photo safeguards are easy to regress.
- Runtime auto-install bootstrap in `backend/main.py` is unusual for production services; can mask dependency issues and alters startup behavior.
- Transformers/tokenizers + `uvicorn --reload` fork model can produce tokenizer parallelism warnings/noise; environment is now set to reduce this.

Fragile or cautionary areas:
- SQL-heavy route files with many edge-case branches (especially persons/folders/media removal flows).
- Migration interactions with historical data (asset_id/vip_id backfills, dedupe semantics, removed_from_app filters).

# What NOT to Touch
- Do not manually edit `.venv/` or site-packages artifacts.
- Do not hardcode environment-specific runtime paths; use `backend/config.py` + profile helpers.
- Do not edit historical migration files that were already applied; add new files in `backend/database/migrations/`.
- Do not commit profile/runtime data from `~/Library/Application Support/VIP/...` (outside repo) into source.
- Be careful with model weight assets (`*.pt`) in repo root; they are large and environment-dependent artifacts.
- `backend/vip.db` exists in repo tree but authoritative runtime DB is profile-scoped under Application Support; treat repo-local DB file as non-authoritative/dev artifact unless explicitly intended.

# Uncertainty Notes
- No formal backend unit/integration/e2e test suite was found in-repo at the time of this handover.
- Production deployment topology beyond local/dev scripts is not explicitly defined in repo scripts/docs.

## VIP-Specific Context

### Pipeline Stage Contracts
- Phase 1 scan (`run_ingest` -> `_phase_scan`) walks the folder, hashes files, reads EXIF in batches, and inserts or updates `media_files`. New rows start at `ingest_state='scanned'`. Existing rows are re-evaluated in place, `removed_from_app` is cleared, and VIP/XMP history is captured in `external_exif` when present.
- Phase 2 embed (`_phase_embed`) only processes `media_files` where `ingest_state='scanned'` and `is_stub=0`. It extracts previews, runs blur detection, detects faces, inserts face rows and embeddings, and advances files to `ingest_state='embedded'` when no face work is needed or after completion.
- Phase 3 cluster (`_phase_cluster`) groups unowned embeddings into clusters. On full-library runs it first clears unnamed clusters so faces return to the pool. Newly assigned files are promoted to `ingest_state='clustered'` unless they are already tagged.
- Phase 3a singleton recovery (`_phase_recover_singletons`) uses FAISS to absorb singleton noise into existing clusters or surface merge suggestions. It respects `auto_name_threshold`, `merge_suggest_threshold`, and `unnamed_auto_merge_threshold`.
- Phase 3b auto-merge (`_phase_auto_merge`) compares named-person centroids against unnamed clusters, auto-assigns near-identical matches, persists person centroids, and queues affected media for writeback. Ignored persons are also used as silent suppressors.
- Phase 3b-ii co-occurrence (`_phase_build_cooccurrence`) rebuilds `person_cooccurrence` from scratch from current face assignments.
- Phase 3c VIP name restore (`_phase_restore_vip_names`) replays historical VIP face names from `external_exif` for files ingested in the current run whose snapshot contains an identifier and named regions.
- Phase 4 tag (`_phase_tag`) runs object, animal, geography, place, and explicit detection only for files with `tags_done=0` and `ingest_state IN ('embedded','clustered','tagged')`. It marks files `ingest_state='tagged'`, sets `tags_done=1`, clears `florence_done`, and queues GPS-resolved place labels for writeback.
- Phase 5 Florence (`_phase_florence`) only runs when `florence_enabled` is true and the request has selected media IDs. It writes caption, OCR, and region tags, sets `florence_done=1`, and queues changed files for writeback.
- Phase 4b CLIP index (`_phase_clip_index`) builds or refreshes per-photo CLIP embeddings. Full rebuilds clear `clip_embeddings`; incremental runs only add missing rows for active photos with `tags_done=1`.
- Phase 6 analysis (`_phase_analyse`) rebuilds per-photo analysis documents for tagged files whose stored model version is stale or missing.
- Resume and reprocess helpers intentionally reset state: single-photo and batch reprocess delete face rows/embeddings, reset `ingest_state` back to `scanned`, and force the affected items back through embed -> cluster -> auto-merge -> restore names -> tag -> Florence -> analyse.

### Settings Store Catalog
- Face detection: `face_detection_mode`, `face_detection_threshold`, `min_face_size_px`, `gender_min_sharpness`, `face_min_sharpness`.
- Clustering and merge policy: `hdbscan_min_cluster_size`, `hdbscan_min_samples`, `hdbscan_cluster_epsilon`, `cluster_inertia_threshold`, `high_confidence_threshold`, `auto_name_threshold`, `merge_suggest_threshold`, `unnamed_auto_merge_threshold`.
- Background suggestions: `merge_multi_anchor_enabled`, `merge_multi_anchor_max`, `suggestion_worker_enabled`, `suggestion_worker_idle_sec`, `suggestion_worker_sleep_sec`, `suggestion_worker_person_batch`, `suggestion_worker_max_per_person`, `suggestion_worker_min_sim`, `suggestion_worker_min_margin`.
- Object and scene tagging: `yolo_conf_threshold`, `places365_top_k`, `landmark_threshold`, `species_threshold`.
- System and throughput: `log_level`, `embed_concurrency`, `tag_concurrency`, `florence_concurrency`, `florence_inference_batch_size`, `florence_num_beams`, `exif_batch_size`, `exif_batch_timeout`.
- Content safety and module switches: `nudenet_confidence_threshold`, `object_detector_enabled`, `scene_classifier_enabled`, `landmark_recogniser_enabled`, `species_classifier_enabled`, `geo_resolver_enabled`, `explicit_detector_enabled`, `florence_enabled`.
- The settings store is profile-scoped, cached in-process, and refreshed with `load_cache()`. `get_all()` is the admin UI source of truth for labels, ranges, and groups.

### Assistant Stack Status
- Legacy chat remains wired at `POST /api/chat/message` through `AssistantPlanner` and `execute_plan`.
- Assistant V2 is wired at `POST /api/chat/v2/message` through `AssistantV2Orchestrator` and `AssistantV2ToolPlanner`.
- V2 prefers deterministic tools first, then retrieval/SQL fallbacks, then legacy assistant only when no better tool fits.
- The active V2 message-aware tools include retrieval and people/location utilities such as `show_photos_of_people`, `count_photos_of_people`, `natural_search`, `retrieval_broker`, `sql_agent`, `legacy_assistant`, and `list_people_with_person_in_location`.

### Writeback Flow
- `backend/writeback/engine.py` is the writeback authority. `preview_pending()` performs a dry run, `execute_writes()` confirms queued work, and `write_single_file()` bypasses the queue for a single media item.
- The API surface is `GET /api/writeback/preview`, `POST /api/writeback/confirm`, `POST /api/writeback/single/{media_id}`, `GET /api/writeback/status`, and `POST /api/writeback/retry-failed`.
- Queue items live in `writeback_queue` and are grouped by status. Failed rows can be reset to `pending` and retried.
- Phase 4 and Florence both enqueue photos that need metadata persistence. The queue is also updated when auto-merge or name assignment changes person-linked metadata.
- Writeback expects files to be local on disk, not iCloud stubs, and the first real write creates ExifTool backup files.

### Active Development State
- The folder view now has a scoped people-style faces pane that only reflects the current folder, not all photos, and it stays hidden in All Photos.
- The pane supports named and unnamed faces, ignore-all-unnamed, collapsibility, selection mode, bulk name, and bulk ignore flows that mirror the existing People UX.
- The frontend uses in-app themed modals rather than browser prompts for name/ignore actions.
- The dev launcher sets `TOKENIZERS_PARALLELISM=false` to suppress HuggingFace tokenizer fork warnings under `uvicorn --reload`.
- The root `CLAUDE.md` is intended as a repo handover document, so future work should preserve the verified architecture and runtime notes above rather than rewriting them from scratch.