📸 Offline Visual Intelligence Platform

Delivery Roadmap (v1 → v1.5)

⸻

🎯 Product Outcome (North Star)

A local-first media intelligence system that:
	•	Indexes terabytes of photos & videos offline
	•	Identifies people, objects, scenes, text
	•	Allows users to name people via web UI
	•	Writes standards-compliant metadata back into original files
	•	Provides powerful search via browser
	•	Is incremental, resumable, and future-proof

⸻

🧱 High-Level Architecture

macOS Host
│
├── Media Scanner (Python)
├── ML Inference Engine (MLX + PyTorch)
├── Vector Index (FAISS)
├── Metadata Store (SQLite / DuckDB)
├── Metadata Writer (ExifTool)
├── API Server (FastAPI)
└── Web UI (React / Next.js)

All components run locally.

⸻

🗺️ PHASED DELIVERY PLAN

⸻

🔹 Phase 0 — Foundations & Decisions (Week 0–1)

Outcomes
	•	Shared understanding
	•	Repo structure
	•	Clear ownership boundaries

Deliverables
	•	Architecture diagram
	•	Model selection doc
	•	Data schemas
	•	Dev environment setup

Preferred Tools

Area	Tool
Language	Python 3.10+
ML	MLX + PyTorch
DB	SQLite (start), DuckDB (later)
Vectors	FAISS
API	FastAPI
UI	React + Vite or Next.js
Media	FFmpeg
Metadata	ExifTool


⸻

🔹 Phase 1 — Media Ingest & Catalog (Week 1–3)

Outcome

System can safely scan, hash, and catalog millions of files.

Features
	•	Recursive folder scanning
	•	File hashing (SHA-256)
	•	EXIF extraction
	•	Video frame sampling
	•	Change detection
	•	Resume after interruption

Key Engineering Decisions
	•	Originals are read-only
	•	No ML yet
	•	Sidecar JSON for early metadata

Core Tables

media_files
media_versions
video_frames
scan_state

Ownership
	•	Backend / Infra engineers

⸻

🔹 Phase 2 — Face Detection & Embeddings (Week 3–6)

Outcome

System detects faces and produces stable identity embeddings.

Features
	•	Face detection
	•	Face cropping
	•	Embedding generation
	•	Batched inference
	•	Confidence scoring

Preferred Models

Task	Model
Detection	RetinaFace
Embeddings	ArcFace-style encoder
Runtime	MLX (primary), PyTorch fallback

Storage
	•	Face bounding boxes
	•	512-D embeddings
	•	Media ↔ face relationships

Important Rule

🚫 No labeling yet

Everything is “unknown person”.

⸻

🔹 Phase 3 — Vector Indexing & Clustering (Week 6–8)

Outcome

Faces automatically cluster into likely individuals.

Features
	•	FAISS vector index
	•	Incremental indexing
	•	HDBSCAN clustering
	•	Cluster confidence
	•	Merge/split support

Outputs

person_clusters
cluster_memberships

Engineering Notes
	•	Cluster stability is more important than speed
	•	Never delete embeddings
	•	Recluster incrementally

⸻

🔹 Phase 4 — Web UI v1 (Week 7–10)

Outcome

User can see, browse, and name people.

Web UI Capabilities
	•	Local web app (localhost)
	•	Auth-less (single user)
	•	Face cluster browser
	•	“Name this person”
	•	Preview affected photos
	•	Undo / rename

Tech Stack

Layer	Tool
API	FastAPI
Frontend	React + Tailwind
Image serving	Local HTTP
State	REST + WebSockets

UX Principles
	•	Fast, minimal clicks
	•	No ML jargon
	•	Confidence-based suggestions

⸻

🔹 Phase 5 — Metadata Writeback Engine (Week 10–12)

Outcome

Names and tags are written into original files safely.

Features
	•	Dry-run mode
	•	Backup originals
	•	Standards-compliant XMP/EXIF
	•	Face region metadata
	•	Video sidecar handling

Metadata Written
	•	PersonInImage
	•	Subject / Keywords
	•	MWG Regions
	•	Scene tags

Tooling
	•	ExifTool (wrapped safely)
	•	Rollback support

⚠️ This phase requires extreme care

⸻

🔹 Phase 6 — Object, Scene & OCR Intelligence (Week 12–15)

Outcome

Search expands beyond people.

Added Capabilities
	•	Object detection
	•	Scene classification
	•	OCR text indexing
	•	Semantic tags

Models

Task	Approach
Objects	YOLO / embedding similarity
Scenes	Places-style model
OCR	Tesseract + TrOCR

UI Enhancements
	•	Filter by object
	•	Filter by place
	•	Text search inside images

⸻

🔹 Phase 7 — Advanced Search & UX (Week 15–18)

Outcome

Google-Photos-level querying offline.

Search Examples
	•	“Photos of X and Y together”
	•	“Beach photos before 2018”
	•	“Videos where X appears >30s”
	•	“Photos with text containing ‘Invoice’”

Tech
	•	Hybrid keyword + vector search
	•	Query planner
	•	Cached results

⸻

🔐 Non-Functional Requirements (Built-In)

Privacy
	•	No network calls
	•	No telemetry
	•	No cloud dependencies

Performance
	•	Batch processing
	•	Backpressure control
	•	Thermal-aware scheduling

Reliability
	•	Idempotent jobs
	•	Crash recovery
	•	File integrity verification

⸻

👥 Suggested Team Composition

Role	Count
ML Engineer	1–2
Backend Engineer	1–2
Frontend Engineer	1
Systems / Infra	1
QA / Tooling	Shared


⸻

🧠 v1 Success Criteria

The project is successful when:
	•	User can open a browser
	•	See all people clusters
	•	Name a person once
	•	See metadata written into files
	•	Search works without the app running

⸻