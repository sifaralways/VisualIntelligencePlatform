# Multi-Anchor Face Recognition — Implementation Plan

## Problem Statement

When a person has thousands of face embeddings spanning many years, the single quality-weighted centroid collapses into a meaningless point in embedding space. The centroid sits between distinct appearance modes (child, adult, glasses, no glasses, bearded, etc.) and doesn't closely match ANY real face — causing recognition failures as the library grows.

**Current state:** 500+ named persons, scaling to 100K+ photos, individuals appearing in 10,000+ photos across decades.

**Root cause:** `backend/pipeline/centroid.py:136` computes `weighted_centroid_from_rows()` which averages up to 100 face embeddings into a single 512-D vector. When those 100 faces span 20 years of aging, the average occupies empty space between natural clusters.

---

## Architecture Overview

Replace single-centroid matching with a **multi-anchor** system where each person is represented by 3–15 sub-centroids ("anchors") that each cover a tight region of embedding space.

**Matching rule change:**
```
OLD: match if cosine(face, person_centroid) >= 0.98
NEW: match if max(cosine(face, anchor_i)) >= 0.88 for any anchor_i of person
```

**Key invariant:** The system remains fully backward-compatible. The existing `persons.centroid` column is preserved as a "global centroid" for fast screening and UI display. Anchors add precision on top.

---

## Phase 1: Database Schema & Anchor Storage

**Outcome:** New tables exist. Existing functionality unchanged. Migration is additive.

### 1.1 Create Migration `030_person_anchors.sql`

**File:** `backend/database/migrations/030_person_anchors.sql`

```sql
-- Person anchors: multiple sub-centroids per person for high-fidelity matching.
-- Each anchor represents a tight cluster of faces (e.g., one age range, one look).

CREATE TABLE IF NOT EXISTS person_anchors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    anchor_index    INTEGER NOT NULL,          -- 0-based ordinal within this person
    centroid        BLOB NOT NULL,             -- 512 x float32, L2-normalized
    member_count    INTEGER NOT NULL DEFAULT 0,
    intra_similarity REAL,                     -- mean cosine sim of members to this anchor
    time_range_start TEXT,                     -- earliest date_taken of member faces (ISO)
    time_range_end   TEXT,                     -- latest date_taken of member faces (ISO)
    quality_score   REAL,                      -- average quality of member faces
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(person_id, anchor_index)
);

CREATE INDEX idx_person_anchors_person ON person_anchors(person_id);

-- Which faces belong to which anchor (for recomputation and UI display)
CREATE TABLE IF NOT EXISTS person_anchor_faces (
    person_id    INTEGER NOT NULL,
    anchor_id    INTEGER NOT NULL REFERENCES person_anchors(id) ON DELETE CASCADE,
    face_id      INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    similarity   REAL,                        -- cosine sim of this face to its anchor centroid
    PRIMARY KEY (person_id, face_id),
    FOREIGN KEY(person_id) REFERENCES persons(id) ON DELETE CASCADE
);

CREATE INDEX idx_paf_anchor ON person_anchor_faces(anchor_id);
CREATE INDEX idx_paf_person ON person_anchor_faces(person_id);

-- Track anchor system metadata on person
ALTER TABLE persons ADD COLUMN anchor_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE persons ADD COLUMN anchors_updated_at TEXT;
```

### 1.2 Register Migration

**File:** `backend/database/db.py` — add `030_person_anchors.sql` to the migration list (follow the pattern of existing migration loading in the `run_migrations` function).

### 1.3 Settings Store Additions

**File:** `backend/database/settings_store.py` — add to `DEFAULTS` dict:

```python
"anchor_min_faces_to_split": {
    "value": 50, "type": "int", "min": 20, "max": 200, "step": 10,
    "label": "Min faces before multi-anchor",
    "description": "Persons with fewer faces than this use a single centroid. Above this, the system creates multiple anchors to handle appearance variation.",
    "group": "Clustering",
},
"anchor_max_count": {
    "value": 15, "type": "int", "min": 3, "max": 30, "step": 1,
    "label": "Max anchors per person",
    "description": "Maximum number of sub-centroids per person. More anchors = finer resolution but slower matching.",
    "group": "Clustering",
},
"anchor_min_count": {
    "value": 3, "type": "int", "min": 2, "max": 10, "step": 1,
    "label": "Min anchors per person",
    "description": "When splitting into anchors, always create at least this many to capture temporal variation.",
    "group": "Clustering",
},
"anchor_target_intra_sim": {
    "value": 0.80, "type": "float", "min": 0.60, "max": 0.95, "step": 0.05,
    "label": "Target intra-anchor similarity",
    "description": "Each anchor should have members with at least this mean cosine similarity to its centroid. Anchors below this are split further.",
    "group": "Clustering",
},
"anchor_auto_name_threshold": {
    "value": 0.88, "type": "float", "min": 0.75, "max": 0.98, "step": 0.01,
    "label": "Multi-anchor auto-name threshold",
    "description": "When multi-anchor matching is active, auto-name if best anchor similarity exceeds this. Lower than single-centroid threshold because each anchor is tighter.",
    "group": "Clustering",
},
"anchor_suggest_threshold": {
    "value": 0.60, "type": "float", "min": 0.40, "max": 0.85, "step": 0.05,
    "label": "Multi-anchor suggest threshold",
    "description": "Generate merge suggestion when best anchor similarity exceeds this.",
    "group": "Clustering",
},
```

---

## Phase 2: Anchor Computation Engine

**Outcome:** Given a person_id, the system can compute and store optimal anchors. No matching logic changes yet.

### 2.1 Create `backend/pipeline/anchors.py`

This is the core new module. Implements anchor computation.

```python
"""
VIP Pipeline — Multi-anchor person representation.

Instead of a single centroid per person, computes k sub-centroids (anchors)
that each tightly cover a region of the person's embedding space.

Algorithm:
1. Load all face embeddings for a person (via membership graph)
2. If count < anchor_min_faces_to_split → single anchor = current centroid
3. Otherwise, determine k (number of anchors) adaptively
4. Run agglomerative clustering on the person's embeddings
5. Each resulting cluster becomes one anchor
6. Store anchor centroids and face assignments
"""
```

**Key functions to implement:**

#### `compute_person_anchors(db, person_id) -> list[AnchorResult]`

```
Steps:
1. Query all embeddings for person via v_face_cluster_current + v_cluster_person_current
   (same join as centroid.py:31-49 but without the max_faces limit)
2. If count < settings.anchor_min_faces_to_split:
   - Return single anchor = weighted centroid (current behavior)
3. If count > 2000:
   - Sample 2000 faces using diversity sampling (see 2.2)
   - Use sampled set for clustering, assign remainder after
4. Determine k = adaptive_k(count, settings)
5. Run sklearn AgglomerativeClustering(n_clusters=k, metric='cosine', linkage='average')
6. For each resulting cluster:
   - Compute quality-weighted centroid of its members
   - Compute intra-similarity
   - If intra_sim < anchor_target_intra_sim AND member_count > 15:
     - Recursively split this sub-cluster (up to 1 level deep)
   - Extract time_range from date_taken of member faces
7. For faces not in the sample (if sampling was used):
   - Assign to nearest anchor by cosine similarity
8. Return list of AnchorResult(centroid, face_ids, intra_sim, time_range, quality)
```

#### `adaptive_k(face_count, settings) -> int`

```
Determines number of anchors based on face count and time spread.

Rules:
- 50–100 faces:   k = anchor_min_count (3)
- 100–500 faces:  k = 5
- 500–2000 faces: k = 8
- 2000–5000:      k = 12
- 5000+:          k = anchor_max_count (15)

Additional boost: if date_taken spans > 10 years, add 2 to k (capped at max).
```

#### `diversity_sample(vectors, face_ids, n) -> (sampled_vectors, sampled_ids)`

```
Farthest-point sampling to select n diverse representatives:
1. Start with the face closest to the global centroid
2. Iteratively add the face farthest from all already-selected faces
3. Returns indices of selected faces

This ensures all appearance modes are represented in the clustering input.
```

#### `persist_anchors(db, person_id, anchors: list[AnchorResult]) -> None`

```
1. DELETE FROM person_anchors WHERE person_id = ?
2. DELETE FROM person_anchor_faces WHERE person_id = ?
3. For each anchor (with index i):
   - INSERT INTO person_anchors (person_id, anchor_index, centroid, member_count,
     intra_similarity, time_range_start, time_range_end, quality_score)
   - INSERT INTO person_anchor_faces for each face
4. UPDATE persons SET anchor_count = len(anchors), anchors_updated_at = now()
```

### 2.2 Anchor Computation Integration

**File:** `backend/pipeline/centroid.py`

Add new function `update_person_anchors` that wraps `compute_person_anchors` + `persist_anchors`:

```python
async def update_person_anchors(db, person_id: int) -> int:
    """Recompute anchors for a person. Returns anchor count."""
    from backend.pipeline.anchors import compute_person_anchors, persist_anchors
    anchors = await compute_person_anchors(db, person_id)
    await persist_anchors(db, person_id, anchors)
    return len(anchors)
```

**Call sites:** Every place that currently calls `update_person_centroid` should ALSO call `update_person_anchors` afterward. These are:

| File | Line | Context |
|------|------|---------|
| `backend/pipeline/centroid.py` | 74 | `update_person_centroid()` — add anchor update at end |
| `backend/api/routes/persons.py` | 447 | After auto-naming in rescore |
| `backend/api/routes/persons.py` | 488 | After identity change |
| `backend/api/routes/persons.py` | 1002 | After ignore operation |
| `backend/api/routes/persons.py` | 1446 | After person merge |
| `backend/api/routes/persons.py` | 1968 | Manual recalculate endpoint |

**Strategy:** Modify `update_person_centroid` to call `update_person_anchors` as its final step. This way all existing call sites automatically get anchor updates without modification.

---

## Phase 3: Multi-Anchor Matching

**Outcome:** The auto-merge and suggestion logic uses anchors when available, falling back to single centroid for persons without anchors.

### 3.1 Create Anchor Loading Utility

**File:** `backend/pipeline/anchors.py` — add:

```python
async def load_person_anchors(db, person_id: int) -> list[np.ndarray]:
    """Load all anchor centroids for a person as list of numpy arrays."""
    rows = await db.execute_fetchall(
        "SELECT centroid FROM person_anchors WHERE person_id=? ORDER BY anchor_index",
        (person_id,)
    )
    return [np.frombuffer(row["centroid"], dtype=np.float32).copy() for row in rows]


async def load_all_person_anchors(db) -> dict[int, list[np.ndarray]]:
    """Load anchors for all active named persons. Returns {person_id: [anchor_vectors]}."""
    rows = await db.execute_fetchall("""
        SELECT pa.person_id, pa.centroid
        FROM person_anchors pa
        JOIN persons p ON p.id = pa.person_id
        WHERE p.is_merged = 0 AND p.is_ignored = 0 AND p.name IS NOT NULL
        ORDER BY pa.person_id, pa.anchor_index
    """)
    result = {}
    for row in rows:
        pid = row["person_id"]
        vec = np.frombuffer(row["centroid"], dtype=np.float32).copy()
        result.setdefault(pid, []).append(vec)
    return result
```

### 3.2 Create Multi-Anchor Similarity Function

**File:** `backend/pipeline/anchors.py` — add:

```python
def best_anchor_similarity(face_vec: np.ndarray, anchors: list[np.ndarray]) -> float:
    """Return highest cosine similarity between face_vec and any anchor."""
    if not anchors:
        return 0.0
    # Stack for vectorized dot product
    anchor_matrix = np.stack(anchors)  # shape: (k, 512)
    sims = anchor_matrix @ face_vec    # shape: (k,)
    return float(sims.max())


def best_anchor_similarity_with_index(
    face_vec: np.ndarray, anchors: list[np.ndarray]
) -> tuple[float, int]:
    """Return (best_similarity, anchor_index)."""
    if not anchors:
        return 0.0, -1
    anchor_matrix = np.stack(anchors)
    sims = anchor_matrix @ face_vec
    idx = int(sims.argmax())
    return float(sims[idx]), idx
```

### 3.3 Modify Auto-Merge Phase

**File:** `backend/pipeline/ingest.py` — function `_phase_auto_merge` (line ~1996)

**Current logic (line 2154):**
```python
sim = float(np.dot(pc, c["centroid"]))
```

**New logic:**
```python
# Check if this person has anchors
anchors = person_anchors_map.get(pid)
if anchors and len(anchors) > 1:
    sim = best_anchor_similarity(c["centroid"], anchors)
    effective_threshold = anchor_auto_name_threshold  # 0.88
else:
    sim = float(np.dot(pc, c["centroid"]))
    effective_threshold = auto_name_threshold  # 0.98
```

**Changes required in _phase_auto_merge:**

1. **Before the matching loop (~line 2020):** Load all person anchors:
   ```python
   from backend.pipeline.anchors import load_all_person_anchors, best_anchor_similarity
   person_anchors_map = await load_all_person_anchors(db)
   anchor_auto_name_th = float(settings_store.get("anchor_auto_name_threshold") or 0.88)
   anchor_suggest_th = float(settings_store.get("anchor_suggest_threshold") or 0.60)
   ```

2. **In the matching loop (~line 2144-2201):** Replace the similarity computation:
   ```python
   for pid, pc in person_centroids:
       anchors = person_anchors_map.get(pid)
       for c in unnamed_clusters:
           if anchors and len(anchors) > 1:
               sim = best_anchor_similarity(c["centroid"], anchors)
               auto_th = anchor_auto_name_th
               suggest_th = anchor_suggest_th
           else:
               sim = float(np.dot(pc, c["centroid"]))
               auto_th = auto_name_threshold
               suggest_th = suggest_threshold
           
           if sim >= auto_th:
               # ... existing auto-name logic ...
           elif sim >= suggest_th:
               # ... existing suggestion logic ...
   ```

3. **After an auto-name merge (~line 2186):** The existing `update_person_centroid` call already triggers anchor recomputation (from Phase 2.2).

### 3.4 Modify Suggestion Worker

**File:** `backend/pipeline/suggestion_worker.py` — function `_refresh_person_queue_quality` (line ~88)

**Changes:**

1. **Load anchors at start (~line 99):**
   ```python
   from backend.pipeline.anchors import load_person_anchors, best_anchor_similarity
   person_anchors = await load_person_anchors(db, person_id)
   use_anchors = len(person_anchors) > 1
   ```

2. **Replace similarity calculation (~line 210):**
   ```python
   # OLD:
   sim = float(np.dot(person_centroid, c_vec))
   
   # NEW:
   if use_anchors:
       sim = best_anchor_similarity(c_vec, person_anchors)
   else:
       sim = float(np.dot(person_centroid, c_vec))
   ```

3. **Update competitor comparison (~line 214, function `_best_competing_person`):**
   Add an optional `all_person_anchors_map` parameter:
   ```python
   def _best_competing_person(cluster_centroid, competitor_centroids, anchors_map=None):
       best_sim = None
       best_id = None
       for other_id, other_vec in competitor_centroids:
           other_anchors = anchors_map.get(other_id) if anchors_map else None
           if other_anchors and len(other_anchors) > 1:
               sim = best_anchor_similarity(cluster_centroid, other_anchors)
           else:
               sim = float(np.dot(cluster_centroid, other_vec))
           if best_sim is None or sim > best_sim:
               best_id = other_id
               best_sim = sim
       return best_id, best_sim
   ```

### 3.5 Modify Rescore After Person Update

**File:** `backend/api/routes/persons.py` — function `_rescore_after_person_update` (line ~286)

**Changes:**

1. **Load anchors (~line 316):**
   ```python
   from backend.pipeline.anchors import load_person_anchors, best_anchor_similarity
   person_anchors = await load_person_anchors(db, person_id)
   use_anchors = len(person_anchors) > 1
   ```

2. **Replace rerank similarity (~line 419):**
   ```python
   # OLD:
   sim = float(np.dot(person_centroid, cluster_centroid))
   
   # NEW:
   if use_anchors:
       sim = best_anchor_similarity(cluster_centroid, person_anchors)
   else:
       sim = float(np.dot(person_centroid, cluster_centroid))
   ```

3. **Use anchor-appropriate thresholds in auto-name decision (~line 427).**

---

## Phase 4: Diversity-Aware Exemplar Selection

**Outcome:** The faces selected for centroid/anchor computation maximally cover the person's appearance space rather than being biased toward one "look."

### 4.1 Replace `select_top_face_rows` Strategy

**File:** `backend/face_quality.py` — modify `select_top_face_rows` (line 176)

**Current behavior:** Sort by quality score, take top N. This biases toward one good-lighting/good-pose condition.

**New behavior:** Diversity-then-quality selection:

```python
def select_top_face_rows(
    rows: list,
    max_faces: int,
    *,
    prefer_recent_photos: bool = False,
    recency_boost: float = 0.35,
    diversity_aware: bool = True,  # NEW PARAMETER
) -> list:
    if max_faces <= 0 or len(rows) <= max_faces:
        return list(rows)
    
    if not diversity_aware:
        # Legacy behavior
        ...existing code...
    
    # NEW: Diversity-aware selection
    # 1. Extract embeddings and compute quality scores
    vectors = [np.frombuffer(row["vector"], dtype=np.float32) for row in rows]
    quality_scores = [face_quality_score_from_row(row) for row in rows]
    
    # 2. Select 2x candidates by quality (pre-filter junk)
    candidate_count = min(len(rows), max_faces * 3)
    quality_ranked = sorted(range(len(rows)), key=lambda i: quality_scores[i], reverse=True)
    candidates = quality_ranked[:candidate_count]
    
    # 3. From candidates, select max_faces using farthest-point sampling
    selected = _farthest_point_sample(
        [vectors[i] for i in candidates],
        max_faces,
    )
    
    return [rows[candidates[i]] for i in selected]
```

### 4.2 Implement Farthest-Point Sampling

**File:** `backend/face_quality.py` — add new function:

```python
def _farthest_point_sample(vectors: list[np.ndarray], n: int) -> list[int]:
    """
    Select n diverse vectors using farthest-point sampling.
    Returns indices into the input list.
    
    Algorithm:
    1. Start with vector closest to mean (most "typical")
    2. Iteratively add the vector most distant from all selected vectors
    """
    if n >= len(vectors):
        return list(range(len(vectors)))
    
    matrix = np.stack(vectors)  # (N, 512)
    mean_vec = matrix.mean(axis=0)
    mean_vec /= np.linalg.norm(mean_vec)
    
    # Start with most typical face
    sims_to_mean = matrix @ mean_vec
    first = int(np.argmax(sims_to_mean))
    
    selected = [first]
    # min_distances[i] = min distance from vectors[i] to any selected vector
    # Using 1 - cosine_sim as distance
    min_sims = matrix @ matrix[first]  # similarity to first selected
    
    for _ in range(n - 1):
        # Find the point with lowest similarity to its nearest selected point
        # (i.e., most distant from the selected set)
        mask = np.ones(len(vectors), dtype=bool)
        for s in selected:
            mask[s] = False
        
        # Among unselected, find the one with minimum max-similarity to selected set
        candidates_min_sim = np.where(mask, min_sims, 2.0)  # 2.0 = ignore selected
        next_idx = int(np.argmin(candidates_min_sim))
        selected.append(next_idx)
        
        # Update min_sims
        new_sims = matrix @ matrix[next_idx]
        min_sims = np.maximum(min_sims, new_sims)  # max sim = min distance
    
    return selected
```

### 4.3 Wire Diversity Selection into Anchor Computation

In `backend/pipeline/anchors.py`, the `diversity_sample` function (from 2.1) uses the same `_farthest_point_sample` logic but operates on the full face set before clustering.

---

## Phase 5: Temporal Stratification

**Outcome:** Anchors are aware of time, and matching gives preference to temporally-adjacent anchors.

### 5.1 Time-Aware Anchor Clustering

**File:** `backend/pipeline/anchors.py` — modify `compute_person_anchors`

When the person's photos span > 5 years, use **time-stratified clustering**:

```python
async def compute_person_anchors(db, person_id):
    # ... load all embeddings with date_taken ...
    
    time_span_years = compute_time_span(rows)
    
    if time_span_years > 5:
        # Partition into time buckets, cluster within each
        anchors = _time_stratified_anchoring(rows, settings)
    else:
        # Standard agglomerative clustering on embedding space
        anchors = _embedding_space_anchoring(rows, settings)
    
    return anchors
```

#### `_time_stratified_anchoring`

```
Algorithm:
1. Sort faces by date_taken
2. Partition into time windows:
   - < 5 years span:  2-year windows
   - 5-15 years span: 3-year windows
   - > 15 years span: 5-year windows
3. For each window with >= 10 faces:
   - If intra-similarity of the window is >= target_intra_sim:
     - Single anchor for this window
   - Else:
     - Sub-cluster within window (k=2-3) to separate concurrent looks
4. For windows with < 10 faces:
   - Merge with adjacent window
5. Cap total anchors at anchor_max_count
```

### 5.2 Temporal Proximity Boost in Matching

**File:** `backend/pipeline/anchors.py` — add:

```python
def best_anchor_similarity_temporal(
    face_vec: np.ndarray,
    anchors: list[np.ndarray],
    anchor_time_ranges: list[tuple[str, str]],  # (start, end) ISO dates
    photo_date: str | None,
    temporal_boost: float = 0.03,
) -> float:
    """
    Match with temporal proximity boost.
    Anchors whose time range overlaps or is adjacent to the photo date
    get a small similarity boost.
    """
    base_sims = np.stack(anchors) @ face_vec
    
    if photo_date is None:
        return float(base_sims.max())
    
    photo_ts = parse_iso_date(photo_date)
    if photo_ts is None:
        return float(base_sims.max())
    
    for i, (start, end) in enumerate(anchor_time_ranges):
        start_ts = parse_iso_date(start)
        end_ts = parse_iso_date(end)
        if start_ts and end_ts:
            # Boost if photo is within or near this anchor's time range
            if start_ts <= photo_ts <= end_ts:
                base_sims[i] += temporal_boost
            else:
                # Decay boost by distance (halves every 3 years)
                years_away = min(
                    abs(photo_ts - end_ts),
                    abs(photo_ts - start_ts)
                ) / (365.25 * 24 * 3600)
                decay = temporal_boost * (0.5 ** (years_away / 3.0))
                base_sims[i] += decay
    
    return float(base_sims.max())
```

### 5.3 Integration Points

The temporal-aware matching is used in:
- `_phase_auto_merge` — when the cluster being matched has `date_taken` metadata available
- `suggestion_worker` — same
- `_rescore_after_person_update` — same

Pass `photo_date` as the median `date_taken` of the unnamed cluster's member faces.

---

## Phase 6: Cascade Matching with FAISS

**Outcome:** Matching remains sub-second even with 500+ persons × 15 anchors = 7,500+ anchor vectors.

### 6.1 Build Anchor FAISS Index

**File:** `backend/ml/index.py` — add new class or method:

```python
class AnchorIndex:
    """
    FAISS index over all person anchors for fast candidate retrieval.
    Each vector in the index is one anchor, tagged with (person_id, anchor_index).
    """
    
    def __init__(self):
        self._index: faiss.Index | None = None
        self._person_ids: list[int] = []      # parallel to FAISS vectors
        self._anchor_indices: list[int] = []  # parallel to FAISS vectors
    
    def build(self, anchors_by_person: dict[int, list[np.ndarray]]):
        """Build index from {person_id: [anchor_vectors]}."""
        vectors = []
        person_ids = []
        anchor_indices = []
        
        for pid, anchor_list in anchors_by_person.items():
            for i, vec in enumerate(anchor_list):
                vectors.append(vec)
                person_ids.append(pid)
                anchor_indices.append(i)
        
        if not vectors:
            self._index = None
            return
        
        matrix = np.stack(vectors).astype(np.float32)
        d = matrix.shape[1]
        
        # At 7,500 vectors, flat index is fine (< 300K threshold)
        # But design for growth: if > 10K anchors, use IVF
        if len(vectors) < 10000:
            self._index = faiss.IndexFlatIP(d)
        else:
            nlist = min(256, len(vectors) // 40)
            quantizer = faiss.IndexFlatIP(d)
            self._index = faiss.IndexIVFFlat(quantizer, d, nlist, faiss.METRIC_INNER_PRODUCT)
            self._index.train(matrix)
        
        self._index.add(matrix)
        self._person_ids = person_ids
        self._anchor_indices = anchor_indices
    
    def search(self, query_vec: np.ndarray, k: int = 20) -> list[tuple[int, float]]:
        """
        Returns top-k (person_id, similarity) pairs, deduplicated by person.
        A person appears at most once, with their best anchor match.
        """
        if self._index is None:
            return []
        
        query = query_vec.reshape(1, -1).astype(np.float32)
        sims, indices = self._index.search(query, min(k * 3, self._index.ntotal))
        
        # Deduplicate by person, keep best anchor
        best_per_person: dict[int, float] = {}
        for sim, idx in zip(sims[0], indices[0]):
            if idx < 0:
                continue
            pid = self._person_ids[idx]
            if pid not in best_per_person or sim > best_per_person[pid]:
                best_per_person[pid] = float(sim)
        
        # Sort by similarity descending
        results = sorted(best_per_person.items(), key=lambda x: x[1], reverse=True)
        return results[:k]
```

### 6.2 Integrate Anchor Index into Pipeline

**File:** `backend/pipeline/ingest.py`

In `_phase_auto_merge`, replace the O(persons × clusters) nested loop with:

```python
# Build anchor index once at phase start
anchor_index = AnchorIndex()
anchors_by_person = await load_all_person_anchors(db)
anchor_index.build(anchors_by_person)

# For each unnamed cluster:
for c in unnamed_clusters:
    # Stage 1: FAISS ANN retrieval — get top-20 candidate persons
    candidates = anchor_index.search(c["centroid"], k=20)
    
    # Stage 2: Fine verification against full anchor set
    best_pid = None
    best_sim = 0.0
    for pid, coarse_sim in candidates:
        if coarse_sim < suggest_threshold:
            break  # sorted descending, no need to check further
        person_anchors = anchors_by_person.get(pid, [])
        if person_anchors:
            sim = best_anchor_similarity(c["centroid"], person_anchors)
        else:
            # Fallback to single centroid
            pc = person_centroids_map[pid]
            sim = float(np.dot(pc, c["centroid"]))
        if sim > best_sim:
            best_sim = sim
            best_pid = pid
    
    # Stage 3: Determine action
    if best_pid and best_sim >= auto_name_threshold:
        # Auto-name
        ...
    elif best_pid and best_sim >= suggest_threshold:
        # Check margin against second-best
        second_best = _second_best_from_candidates(candidates, best_pid, ...)
        if best_sim - second_best >= min_margin:
            # Generate suggestion
            ...
```

### 6.3 Rebuild Anchor Index Timing

The anchor FAISS index should be rebuilt:
- At the start of `_phase_auto_merge`
- At the start of `_phase_recover_singletons`
- When `_rescore_after_person_update` runs (or use module-level cached instance with invalidation)

**Cache strategy:** Store as module-level singleton with a generation counter. Increment generation on any person centroid/anchor update. Rebuild lazily on next access if stale.

---

## Phase 7: Incremental Anchor Updates

**Outcome:** Adding a few faces to a person doesn't require full O(N) recomputation of all anchors.

### 7.1 Fast Incremental Update Path

**File:** `backend/pipeline/anchors.py` — add:

```python
async def incremental_anchor_update(
    db, person_id: int, new_face_ids: list[int]
) -> bool:
    """
    Try to absorb new faces into existing anchors without full recomputation.
    Returns True if successful, False if full recompute is needed.
    """
    existing_anchors = await load_person_anchors_with_metadata(db, person_id)
    if not existing_anchors:
        return False  # no anchors yet, need full compute
    
    new_embeddings = await load_face_embeddings(db, new_face_ids)
    
    needs_full_recompute = False
    for face_id, vec in zip(new_face_ids, new_embeddings):
        best_sim, best_anchor_idx = best_anchor_similarity_with_index(
            vec, [a.centroid for a in existing_anchors]
        )
        
        if best_sim >= 0.75:
            # Face fits well in existing anchor — absorb it
            await _absorb_face_into_anchor(
                db, person_id, existing_anchors[best_anchor_idx], face_id, vec
            )
        elif best_sim < 0.60:
            # Face is far from all anchors — new appearance mode
            # Need full recompute to potentially create new anchor
            needs_full_recompute = True
            break
        else:
            # Borderline — absorb into nearest but flag for periodic rebalance
            await _absorb_face_into_anchor(
                db, person_id, existing_anchors[best_anchor_idx], face_id, vec
            )
            # Mark person for background rebalance
            await db.execute(
                "UPDATE persons SET anchors_updated_at=NULL WHERE id=?",
                (person_id,)
            )
    
    return not needs_full_recompute


async def _absorb_face_into_anchor(db, person_id, anchor, face_id, vec):
    """Update anchor centroid incrementally with new face."""
    # Running weighted average: new_centroid = (old * n + new) / (n + 1)
    n = anchor.member_count
    new_centroid = (anchor.centroid * n + vec) / (n + 1)
    new_centroid /= np.linalg.norm(new_centroid)  # re-normalize
    
    await db.execute("""
        UPDATE person_anchors
        SET centroid=?, member_count=member_count+1, updated_at=datetime('now')
        WHERE id=?
    """, (new_centroid.tobytes(), anchor.id))
    
    await db.execute("""
        INSERT OR REPLACE INTO person_anchor_faces (person_id, anchor_id, face_id, similarity)
        VALUES (?, ?, ?, ?)
    """, (person_id, anchor.id, face_id, float(np.dot(new_centroid, vec))))
```

### 7.2 Integration Into Merge Flow

**File:** `backend/pipeline/ingest.py` — in auto-merge (after assigning cluster to person):

```python
# After auto-naming a cluster to a person:
new_face_ids = [fid for fid in cluster_face_ids]

# Try incremental update first
success = await incremental_anchor_update(db, pid, new_face_ids)
if not success:
    # Full recompute needed (new appearance mode detected)
    await update_person_centroid(db, pid)  # this now includes anchor recompute
```

### 7.3 Background Rebalance Worker

**File:** `backend/pipeline/suggestion_worker.py` — add anchor rebalance to idle processing:

```python
async def _rebalance_stale_anchors(db):
    """
    Find persons whose anchors are stale (anchors_updated_at IS NULL)
    and recompute their anchors from scratch.
    Run during idle time, one person per tick.
    """
    row = await db.execute_fetchone("""
        SELECT id FROM persons
        WHERE anchor_count > 0
          AND anchors_updated_at IS NULL
          AND is_merged = 0
        LIMIT 1
    """)
    if row:
        await update_person_anchors(db, row["id"])
```

Invoke this from the suggestion worker's idle loop.

---

## Phase 8: Anchor Health Monitoring

**Outcome:** The system detects and flags degraded anchors and persons that may need attention.

### 8.1 Health Metrics

**File:** `backend/pipeline/anchors.py` — add:

```python
async def compute_person_anchor_health(db, person_id: int) -> dict:
    """
    Compute health metrics for a person's anchor set.
    Returns dict with:
      - spread: max inter-anchor distance (high = diverse identity, or two-person merge)
      - min_intra_sim: worst anchor's internal cohesion
      - stale_anchors: anchors with no new faces in last 3 pipeline runs
      - coverage: fraction of person's faces within 0.8 sim of their assigned anchor
    """
    anchors = await load_person_anchors_with_metadata(db, person_id)
    if len(anchors) < 2:
        return {"spread": 0.0, "min_intra_sim": 1.0, "stale_anchors": 0, "coverage": 1.0}
    
    # Spread: maximum inter-anchor distance
    centroids = np.stack([a.centroid for a in anchors])
    sim_matrix = centroids @ centroids.T
    np.fill_diagonal(sim_matrix, 1.0)
    min_inter_sim = float(sim_matrix.min())
    spread = 1.0 - min_inter_sim  # higher = more spread
    
    # Min intra similarity
    min_intra = min(a.intra_similarity for a in anchors if a.intra_similarity)
    
    # Coverage: check face-to-anchor assignments
    face_rows = await db.execute_fetchall("""
        SELECT paf.similarity FROM person_anchor_faces paf
        WHERE paf.person_id = ?
    """, (person_id,))
    if face_rows:
        sims = [row["similarity"] for row in face_rows if row["similarity"]]
        coverage = sum(1 for s in sims if s >= 0.80) / len(sims) if sims else 1.0
    else:
        coverage = 1.0
    
    return {
        "spread": spread,
        "min_intra_sim": min_intra or 0.0,
        "stale_anchors": 0,  # computed from updated_at vs last pipeline run
        "coverage": coverage,
    }
```

### 8.2 Health Check API Endpoint

**File:** `backend/api/routes/persons.py` — add endpoint:

```python
@router.get("/persons/{person_id}/anchor-health")
async def get_person_anchor_health(person_id: int):
    """Return anchor health metrics for admin/debug UI."""
    async with get_db() as db:
        health = await compute_person_anchor_health(db, person_id)
        anchors = await load_person_anchors_with_metadata(db, person_id)
        return {
            "person_id": person_id,
            "anchor_count": len(anchors),
            "health": health,
            "anchors": [
                {
                    "index": a.anchor_index,
                    "member_count": a.member_count,
                    "intra_similarity": a.intra_similarity,
                    "time_range": [a.time_range_start, a.time_range_end],
                    "quality_score": a.quality_score,
                }
                for a in anchors
            ],
        }
```

### 8.3 Warning Thresholds

Add to settings store:

```python
"anchor_health_spread_warning": {
    "value": 0.60, "type": "float", "min": 0.30, "max": 0.80, "step": 0.05,
    "label": "Anchor spread warning threshold",
    "description": "If max inter-anchor distance exceeds this, flag person for review (may be two people merged).",
    "group": "Clustering",
},
"anchor_health_coverage_warning": {
    "value": 0.70, "type": "float", "min": 0.50, "max": 0.90, "step": 0.05,
    "label": "Anchor coverage warning threshold",
    "description": "If less than this fraction of faces are well-covered by their anchor, trigger recompute.",
    "group": "Clustering",
},
```

---

## Phase 9: Age-Gap Bridging (Link Chains)

**Outcome:** People with large temporal gaps in photos (e.g., childhood photos + adult photos, nothing between) are still connected via transitional evidence.

### 9.1 Anchor Chain Connectivity

**File:** `backend/pipeline/anchors.py` — add:

```python
def anchors_form_connected_chain(
    anchors: list,  # sorted by time
    max_gap_similarity: float = 0.55,
) -> bool:
    """
    Check if anchors form a connected temporal chain.
    Each anchor must have similarity > max_gap_similarity with its
    temporal neighbor (the next anchor in time order).
    
    This allows the system to keep two very different-looking anchors
    (e.g., child and adult) as the same person, IF there exists a
    chain of intermediate anchors connecting them.
    """
    if len(anchors) < 2:
        return True
    
    for i in range(len(anchors) - 1):
        sim = float(np.dot(anchors[i].centroid, anchors[i+1].centroid))
        if sim < max_gap_similarity:
            return False
    return True
```

### 9.2 Split Detection Using Chain Breaks

When health monitoring detects a chain break (two temporally adjacent anchors with very low similarity and no intermediate anchors), flag the person for review:

```python
async def detect_potential_splits(db, person_id: int) -> list[dict]:
    """
    Identify potential split points where a person might actually be two people.
    Returns list of {anchor_a, anchor_b, similarity, suggestion}.
    """
    anchors = await load_person_anchors_with_metadata(db, person_id)
    if len(anchors) < 3:
        return []
    
    # Sort by time
    anchors_sorted = sorted(anchors, key=lambda a: a.time_range_start or "")
    
    splits = []
    for i in range(len(anchors_sorted) - 1):
        sim = float(np.dot(anchors_sorted[i].centroid, anchors_sorted[i+1].centroid))
        if sim < 0.45:  # very low similarity between adjacent time windows
            splits.append({
                "anchor_a_index": anchors_sorted[i].anchor_index,
                "anchor_b_index": anchors_sorted[i+1].anchor_index,
                "similarity": sim,
                "suggestion": "Review: these may be different people merged together",
            })
    
    return splits
```

### 9.3 User-Confirmed Bridges

When the user confirms that two very different anchors ARE the same person (e.g., childhood + adulthood), store this as a permanent link that survives recomputation:

**Migration addition to `030_person_anchors.sql`:**

```sql
CREATE TABLE IF NOT EXISTS person_anchor_bridges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    anchor_a    INTEGER NOT NULL,  -- anchor_index
    anchor_b    INTEGER NOT NULL,  -- anchor_index
    confirmed_by TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
```

These bridges prevent the health monitor from flagging confirmed connections and prevent the system from ever auto-splitting at those points.

---

## Phase 10: Migration & Backward Compatibility

**Outcome:** Existing libraries upgrade seamlessly. No data loss. Gradual anchor population.

### 10.1 First-Run Migration Strategy

When the migration runs on an existing library with 500+ named persons:

1. **Migration SQL** creates empty tables — no anchors exist yet.
2. **On next pipeline run**, the auto-merge phase detects `anchor_count = 0` for all persons and falls back to single-centroid matching (current behavior).
3. **Background anchor builder** (new idle worker) populates anchors for persons sorted by face count (largest first):

```python
async def _build_missing_anchors(db):
    """Background task: compute anchors for persons that don't have them yet."""
    row = await db.execute_fetchone("""
        SELECT p.id, COUNT(DISTINCT f.id) as face_count
        FROM persons p
        JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
        JOIN v_face_cluster_current fcc ON fcc.cluster_guid = cpc.cluster_guid
        JOIN faces f ON f.face_guid = fcc.face_guid
        WHERE p.anchor_count = 0
          AND p.is_merged = 0
          AND p.is_ignored = 0
          AND p.name IS NOT NULL
        GROUP BY p.id
        HAVING face_count >= ?
        ORDER BY face_count DESC
        LIMIT 1
    """, (int(settings_store.get("anchor_min_faces_to_split") or 50),))
    
    if row:
        await update_person_anchors(db, row["id"])
        return True
    return False
```

4. **Wire into suggestion worker idle loop** — when no suggestions to compute, build one person's anchors per tick.

### 10.2 Fallback Behavior

All matching code (Phase 3) already includes fallback:
```python
if anchors and len(anchors) > 1:
    # Multi-anchor matching
else:
    # Single centroid matching (existing behavior)
```

This means the system works correctly during the transition period while anchors are being built in the background.

### 10.3 Progress Visibility

Add a simple status endpoint:

**File:** `backend/api/routes/persons.py`:

```python
@router.get("/persons/anchor-status")
async def get_anchor_build_status():
    """Report anchor population progress."""
    async with get_db() as db:
        total = await db.execute_fetchone(
            "SELECT COUNT(*) as n FROM persons WHERE is_merged=0 AND is_ignored=0 AND name IS NOT NULL"
        )
        with_anchors = await db.execute_fetchone(
            "SELECT COUNT(*) as n FROM persons WHERE anchor_count > 0 AND is_merged=0"
        )
        eligible = await db.execute_fetchone("""
            SELECT COUNT(DISTINCT p.id) as n
            FROM persons p
            JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
            JOIN v_face_cluster_current fcc ON fcc.cluster_guid = cpc.cluster_guid
            JOIN faces f ON f.face_guid = fcc.face_guid
            WHERE p.is_merged=0 AND p.is_ignored=0 AND p.name IS NOT NULL
            GROUP BY p.id
            HAVING COUNT(f.id) >= 50
        """)
        return {
            "total_persons": total["n"],
            "eligible_for_anchors": eligible["n"] if eligible else 0,
            "with_anchors": with_anchors["n"],
        }
```

---

## Summary: File Change Map

| File | Phase | Change Type |
|------|-------|-------------|
| `backend/database/migrations/030_person_anchors.sql` | 1 | NEW FILE |
| `backend/database/settings_store.py` | 1 | Add settings to DEFAULTS |
| `backend/database/db.py` | 1 | Register migration |
| `backend/pipeline/anchors.py` | 2,3,5,7,8,9 | NEW FILE (core module) |
| `backend/pipeline/centroid.py` | 2 | Add `update_person_anchors` call |
| `backend/face_quality.py` | 4 | Add diversity sampling, modify `select_top_face_rows` |
| `backend/pipeline/ingest.py` | 3,6,7 | Modify `_phase_auto_merge`, `_phase_recover_singletons` |
| `backend/pipeline/suggestion_worker.py` | 3,7,10 | Modify matching, add idle tasks |
| `backend/api/routes/persons.py` | 3,8,10 | Modify rescore, add endpoints |
| `backend/ml/index.py` | 6 | Add `AnchorIndex` class |

---

## Threshold Summary (Recommended Starting Values)

| Parameter | Current | With Anchors | Rationale |
|-----------|---------|--------------|-----------|
| Auto-name (single centroid) | 0.98 | 0.98 (unchanged) | Keep conservative for fallback |
| Auto-name (multi-anchor) | N/A | 0.88 | Each anchor is tight; 0.88 against a tight cluster is highly confident |
| Suggest (single centroid) | 0.63 | 0.63 (unchanged) | |
| Suggest (multi-anchor) | N/A | 0.60 | Slightly lower because anchors are more precise |
| Min faces for anchors | N/A | 50 | Below this, single centroid works fine |
| Max anchors per person | N/A | 15 | Diminishing returns above this |
| Target anchor intra-sim | N/A | 0.80 | Each anchor should be a tight cluster |
| Anchor health spread warning | N/A | 0.60 | Flag potential wrong merges |

---

## Phase 11: Existing Data Remediation

**Outcome:** Persons already in the database that may have been incorrectly merged or missed under the single-centroid regime are detected, flagged, and either auto-corrected or surfaced for user review. Unassigned faces are re-evaluated against the new multi-anchor representation.

### The Problem with Existing Data

Under the old single-centroid system, two failure modes have accumulated:

1. **False negatives (missed assignments):** Faces of a known person were NOT auto-named because the drifted centroid had similarity < 0.98 with the face. These faces remain as unnamed singletons or small clusters despite belonging to a named person.

2. **False positives (wrong merges):** Less common due to the 0.98 threshold, but possible when two different people happen to be similar to the same drifted centroid — especially siblings, parent/child, or twins who share facial structure.

3. **Orphaned clusters:** Groups of faces that should have been merged into a named person but never reached the 0.98 threshold because the centroid had already drifted away from that appearance mode.

### 11.1 Database Schema for Remediation Tracking

**Add to migration `030_person_anchors.sql`:**

```sql
-- Track remediation state per person
CREATE TABLE IF NOT EXISTS remediation_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id       INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,          -- 'audit', 'reassign', 'split_suggest', 'reclaim'
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'applied', 'rejected', 'reviewed'
    details         TEXT,                   -- JSON payload with specifics
    face_count      INTEGER,               -- number of faces affected
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT
);

CREATE INDEX idx_remediation_person ON remediation_log(person_id);
CREATE INDEX idx_remediation_status ON remediation_log(status);

-- Track the overall remediation run state
CREATE TABLE IF NOT EXISTS remediation_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    phase           TEXT NOT NULL,          -- 'audit', 'reclaim', 'verify'
    persons_processed INTEGER NOT NULL DEFAULT 0,
    issues_found    INTEGER NOT NULL DEFAULT 0,
    auto_fixed      INTEGER NOT NULL DEFAULT 0,
    needs_review    INTEGER NOT NULL DEFAULT 0
);
```

### 11.2 Step 1: Person Integrity Audit

**File:** `backend/pipeline/remediation.py` (NEW FILE)

Run after anchors are built (Phase 2 complete). For each named person with anchors:

#### `audit_person_integrity(db, person_id) -> AuditResult`

```python
async def audit_person_integrity(db, person_id: int) -> dict:
    """
    Evaluate whether a person's face assignments are internally consistent
    using the new multi-anchor representation.
    
    Detects:
    - Outlier faces: assigned to this person but far from ALL anchors
    - Potential contamination: faces that match another person's anchors better
    - Fragmentation: sub-groups within the person that have no chain connection
    """
    anchors = await load_person_anchors_with_metadata(db, person_id)
    if not anchors or len(anchors) < 2:
        return {"status": "ok", "issues": []}
    
    # Load ALL face embeddings currently assigned to this person
    rows = await db.execute_fetchall("""
        SELECT f.id as face_id, e.vector, f.media_file_id, mf.date_taken
        FROM faces f
        JOIN embeddings e ON e.face_id = f.id
        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
        JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
        JOIN persons p ON p.person_guid = cpc.person_guid
        WHERE p.id = ?
    """, (person_id,))
    
    issues = []
    outlier_faces = []
    
    anchor_centroids = [a.centroid for a in anchors]
    
    for row in rows:
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        best_sim, best_idx = best_anchor_similarity_with_index(vec, anchor_centroids)
        
        if best_sim < 0.55:
            # This face is FAR from all anchors — likely a wrong assignment
            outlier_faces.append({
                "face_id": row["face_id"],
                "media_file_id": row["media_file_id"],
                "best_anchor_sim": best_sim,
                "best_anchor_idx": best_idx,
            })
    
    if outlier_faces:
        issues.append({
            "type": "outlier_faces",
            "severity": "high" if len(outlier_faces) > 5 else "medium",
            "count": len(outlier_faces),
            "faces": outlier_faces[:50],  # cap for storage
            "description": f"{len(outlier_faces)} faces assigned to this person are far from all anchors (best sim < 0.55). These may be wrong assignments.",
        })
    
    # Check for fragmentation (chain breaks)
    from backend.pipeline.anchors import detect_potential_splits
    splits = await detect_potential_splits(db, person_id)
    if splits:
        issues.append({
            "type": "fragmentation",
            "severity": "high",
            "splits": splits,
            "description": f"Person has {len(splits)} potential split point(s) — may be multiple people merged together.",
        })
    
    return {
        "status": "issues_found" if issues else "ok",
        "issues": issues,
        "total_faces": len(rows),
        "outlier_count": len(outlier_faces),
    }
```

#### `run_full_audit(db) -> AuditSummary`

```python
async def run_full_audit(db) -> dict:
    """
    Audit all named persons with anchors. 
    Run as a background task after initial anchor build completes.
    """
    run_id = await _create_remediation_run(db, phase="audit")
    
    persons = await db.execute_fetchall("""
        SELECT id, name, anchor_count FROM persons
        WHERE is_merged = 0 AND is_ignored = 0
          AND name IS NOT NULL AND anchor_count > 0
        ORDER BY anchor_count DESC
    """)
    
    issues_found = 0
    for person in persons:
        result = await audit_person_integrity(db, person["id"])
        if result["status"] == "issues_found":
            issues_found += 1
            for issue in result["issues"]:
                await db.execute("""
                    INSERT INTO remediation_log (person_id, action, status, details, face_count)
                    VALUES (?, ?, 'pending', ?, ?)
                """, (
                    person["id"],
                    "split_suggest" if issue["type"] == "fragmentation" else "reassign",
                    json.dumps(issue),
                    issue.get("count", 0),
                ))
    
    await _complete_remediation_run(db, run_id, len(persons), issues_found)
    return {"persons_audited": len(persons), "issues_found": issues_found}
```

### 11.3 Step 2: Outlier Face Reassignment

For faces flagged as outliers (assigned to person X but far from all of X's anchors):

#### `remediate_outlier_faces(db, person_id) -> RemediationResult`

```python
async def remediate_outlier_faces(db, person_id: int, auto_apply: bool = False) -> dict:
    """
    For each outlier face assigned to this person:
    1. Query the anchor FAISS index to find the TRUE best-matching person
    2. If a better match exists with high confidence → reassign (or suggest)
    3. If no good match → unassign (return to unnamed cluster pool)
    
    Args:
        auto_apply: If True, apply reassignments automatically for high-confidence
                    corrections. If False, generate suggestions for user review.
    """
    # Load pending outlier issues for this person
    issues = await db.execute_fetchall("""
        SELECT id, details FROM remediation_log
        WHERE person_id = ? AND action = 'reassign' AND status = 'pending'
    """, (person_id,))
    
    if not issues:
        return {"status": "nothing_to_do"}
    
    # Load all person anchors for cross-matching
    all_anchors = await load_all_person_anchors(db)
    
    reassigned = 0
    unassigned = 0
    needs_review = 0
    
    for issue in issues:
        details = json.loads(issue["details"])
        for face_info in details.get("faces", []):
            face_id = face_info["face_id"]
            vec = await _load_single_embedding(db, face_id)
            if vec is None:
                continue
            
            # Find best matching person across ALL persons (excluding current)
            best_pid = None
            best_sim = 0.0
            for pid, anchors in all_anchors.items():
                if pid == person_id:
                    continue
                sim = best_anchor_similarity(vec, anchors)
                if sim > best_sim:
                    best_sim = sim
                    best_pid = pid
            
            if best_sim >= 0.85 and auto_apply:
                # High confidence: this face clearly belongs to another person
                await _reassign_face_to_person(db, face_id, best_pid)
                reassigned += 1
            elif best_sim >= 0.70:
                # Medium confidence: suggest for user review
                await _create_reassignment_suggestion(
                    db, face_id, person_id, best_pid, best_sim
                )
                needs_review += 1
            else:
                # No good match anywhere: unassign back to unnamed pool
                if auto_apply:
                    await _unassign_face_from_person(db, face_id)
                    unassigned += 1
                else:
                    needs_review += 1
        
        # Mark issue as processed
        status = "applied" if auto_apply else "reviewed"
        await db.execute(
            "UPDATE remediation_log SET status=?, resolved_at=datetime('now') WHERE id=?",
            (status, issue["id"])
        )
    
    return {
        "reassigned": reassigned,
        "unassigned": unassigned,
        "needs_review": needs_review,
    }
```

#### Helper: `_reassign_face_to_person`

```python
async def _reassign_face_to_person(db, face_id: int, new_person_id: int):
    """
    Move a face from its current person to a different person.
    
    Steps:
    1. Find face's current cluster
    2. If cluster has only this face → reassign entire cluster
    3. If cluster has multiple faces → split face into new singleton cluster, 
       then assign singleton to target person's cluster
    4. Update both old and new person's centroids/anchors
    """
    # Get current cluster
    row = await db.execute_fetchone("""
        SELECT fcc.cluster_guid, c.id as cluster_id, c.member_count,
               cpc.person_guid, p.id as current_person_id
        FROM faces f
        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
        JOIN clusters c ON c.cluster_guid = fcc.cluster_guid
        LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
        LEFT JOIN persons p ON p.person_guid = cpc.person_guid
        WHERE f.id = ?
    """, (face_id,))
    
    if not row:
        return
    
    old_person_id = row["current_person_id"]
    cluster_id = row["cluster_id"]
    member_count = row["member_count"]
    
    if member_count == 1:
        # Singleton cluster — just reassign the cluster
        await _link_cluster_to_person_by_id(db, cluster_id, new_person_id)
    else:
        # Multi-face cluster — need to split this face out
        new_cluster_id = await _split_face_to_new_cluster(db, face_id, cluster_id)
        await _link_cluster_to_person_by_id(db, new_cluster_id, new_person_id)
    
    # Recompute centroids/anchors for both persons
    if old_person_id:
        await update_person_centroid(db, old_person_id)
    await update_person_centroid(db, new_person_id)
```

#### Helper: `_unassign_face_from_person`

```python
async def _unassign_face_from_person(db, face_id: int):
    """
    Remove a face from its current person assignment.
    The face returns to the unnamed cluster pool for future matching.
    
    Steps:
    1. Find face's current cluster
    2. If cluster has only this face → unlink cluster from person
    3. If cluster has multiple faces → split face into new unlinked singleton
    4. Update old person's centroid/anchors
    """
    row = await db.execute_fetchone("""
        SELECT fcc.cluster_guid, c.id as cluster_id, c.member_count,
               cpc.person_guid, p.id as person_id
        FROM faces f
        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
        JOIN clusters c ON c.cluster_guid = fcc.cluster_guid
        LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
        LEFT JOIN persons p ON p.person_guid = cpc.person_guid
        WHERE f.id = ?
    """, (face_id,))
    
    if not row or not row["person_id"]:
        return
    
    person_id = row["person_id"]
    cluster_id = row["cluster_id"]
    member_count = row["member_count"]
    
    if member_count == 1:
        # Unlink this singleton cluster from person
        await _unlink_cluster_from_person(db, cluster_id)
    else:
        # Split face out into its own unlinked cluster
        await _split_face_to_new_cluster(db, face_id, cluster_id)
    
    # Recompute person's centroid/anchors
    await update_person_centroid(db, person_id)
```

### 11.4 Step 3: Reclaim Missed Faces (False Negative Recovery)

After anchors are built, re-scan ALL unnamed singletons and small clusters against the new multi-anchor representation. Many faces that were "missed" by the old 0.98-threshold single-centroid will now match at 0.88 against a tight anchor.

#### `reclaim_missed_faces(db) -> ReclaimResult`

```python
async def reclaim_missed_faces(
    db,
    *,
    auto_apply_threshold: float = 0.90,
    suggest_threshold: float = 0.75,
    batch_size: int = 500,
) -> dict:
    """
    Re-evaluate all unnamed clusters/singletons against person anchors.
    
    This is the primary remediation step that recovers faces the old system
    missed due to centroid drift. Run after all person anchors are built.
    
    Strategy:
    - sim >= 0.90 to best anchor AND margin >= 0.10 over second-best → auto-assign
    - sim >= 0.75 to best anchor AND margin >= 0.05 → generate suggestion
    - sim < 0.75 → leave alone (genuinely unknown person)
    """
    run_id = await _create_remediation_run(db, phase="reclaim")
    
    # Load all person anchors
    all_anchors = await load_all_person_anchors(db)
    if not all_anchors:
        return {"status": "no_anchors_built"}
    
    # Build anchor FAISS index for fast retrieval
    from backend.ml.index import AnchorIndex
    anchor_index = AnchorIndex()
    anchor_index.build(all_anchors)
    
    # Load unnamed clusters (no person assignment)
    unnamed_clusters = await db.execute_fetchall("""
        SELECT c.id as cluster_id, c.cluster_guid, c.centroid, c.member_count
        FROM clusters c
        LEFT JOIN v_cluster_person_current cpc ON cpc.cluster_guid = c.cluster_guid
        WHERE cpc.person_guid IS NULL
          AND c.is_active = 1
          AND c.centroid IS NOT NULL
        ORDER BY c.member_count DESC
    """)
    
    auto_assigned = 0
    suggested = 0
    skipped = 0
    
    for cluster in unnamed_clusters:
        c_vec = np.frombuffer(cluster["centroid"], dtype=np.float32).copy()
        
        # Stage 1: Fast candidate retrieval via FAISS
        candidates = anchor_index.search(c_vec, k=10)
        if not candidates:
            skipped += 1
            continue
        
        # Stage 2: Fine verification
        best_pid, best_sim = candidates[0]
        second_sim = candidates[1][1] if len(candidates) > 1 else 0.0
        margin = best_sim - second_sim
        
        # Verify with full anchor set
        person_anchors = all_anchors.get(best_pid, [])
        if person_anchors:
            best_sim = best_anchor_similarity(c_vec, person_anchors)
        
        # Stage 3: Decision
        if best_sim >= auto_apply_threshold and margin >= 0.10:
            # High confidence reclaim
            await _assign_cluster_to_person(db, cluster["cluster_id"], best_pid)
            auto_assigned += cluster["member_count"]
            
            # Log for audit trail
            await db.execute("""
                INSERT INTO remediation_log (person_id, action, status, details, face_count)
                VALUES (?, 'reclaim', 'applied', ?, ?)
            """, (
                best_pid,
                json.dumps({
                    "cluster_id": cluster["cluster_id"],
                    "similarity": best_sim,
                    "margin": margin,
                    "auto_applied": True,
                }),
                cluster["member_count"],
            ))
        
        elif best_sim >= suggest_threshold and margin >= 0.05:
            # Medium confidence — create suggestion for user
            await _create_merge_suggestion(
                db, best_pid, cluster["cluster_id"], best_sim
            )
            suggested += cluster["member_count"]
        
        else:
            skipped += 1
    
    await _complete_remediation_run(
        db, run_id, len(unnamed_clusters),
        issues_found=auto_assigned + suggested,
        auto_fixed=auto_assigned,
        needs_review=suggested,
    )
    
    return {
        "clusters_evaluated": len(unnamed_clusters),
        "faces_auto_assigned": auto_assigned,
        "faces_suggested": suggested,
        "clusters_skipped": skipped,
    }
```

### 11.5 Step 4: Cross-Person Contamination Check

Detect cases where the same face (or highly similar faces) exist under TWO different named persons — indicating a historical false merge.

#### `detect_cross_person_contamination(db) -> list[ContaminationReport]`

```python
async def detect_cross_person_contamination(db) -> list[dict]:
    """
    Find pairs of persons whose anchors overlap significantly.
    This indicates either:
    - Two entries for the same person (should be merged)
    - Contamination from wrong assignments (should be split)
    
    Algorithm:
    1. For each pair of persons, compute max anchor-to-anchor similarity
    2. If any anchor from person A is very similar to an anchor from person B,
       flag for review
    """
    all_anchors = await load_all_person_anchors(db)
    person_ids = list(all_anchors.keys())
    
    contamination_pairs = []
    
    for i in range(len(person_ids)):
        pid_a = person_ids[i]
        anchors_a = all_anchors[pid_a]
        
        for j in range(i + 1, len(person_ids)):
            pid_b = person_ids[j]
            anchors_b = all_anchors[pid_b]
            
            # Compute cross-similarity matrix
            mat_a = np.stack(anchors_a)  # (ka, 512)
            mat_b = np.stack(anchors_b)  # (kb, 512)
            cross_sims = mat_a @ mat_b.T  # (ka, kb)
            
            max_cross_sim = float(cross_sims.max())
            
            if max_cross_sim >= 0.80:
                # High overlap — either same person or contamination
                idx_a, idx_b = np.unravel_index(cross_sims.argmax(), cross_sims.shape)
                
                contamination_pairs.append({
                    "person_a_id": pid_a,
                    "person_b_id": pid_b,
                    "max_similarity": max_cross_sim,
                    "anchor_a_index": int(idx_a),
                    "anchor_b_index": int(idx_b),
                    "suggestion": "merge" if max_cross_sim >= 0.90 else "review",
                })
    
    # Sort by similarity descending (worst contamination first)
    contamination_pairs.sort(key=lambda x: x["max_similarity"], reverse=True)
    
    # Log findings
    for pair in contamination_pairs:
        await db.execute("""
            INSERT INTO remediation_log (person_id, action, status, details)
            VALUES (?, 'split_suggest', 'pending', ?)
        """, (pair["person_a_id"], json.dumps(pair)))
    
    return contamination_pairs
```

### 11.6 Step 5: Per-Person Embedding Verification

For the highest-value persons (most faces), verify every face assignment against the new anchor system:

#### `verify_person_assignments(db, person_id) -> VerificationResult`

```python
async def verify_person_assignments(db, person_id: int) -> dict:
    """
    For each face assigned to this person, verify it still belongs here
    under multi-anchor matching. Surfaces faces that:
    
    1. Are far from all this person's anchors (sim < 0.60)
    2. Are closer to ANOTHER person's anchors than to this person's
    
    This catches historical wrong merges that accumulated under centroid drift.
    """
    person_anchors = await load_person_anchors(db, person_id)
    all_anchors = await load_all_person_anchors(db)
    
    # Load all face embeddings for this person
    rows = await db.execute_fetchall("""
        SELECT f.id as face_id, e.vector, mf.file_path, mf.date_taken
        FROM faces f
        JOIN embeddings e ON e.face_id = f.id
        JOIN media_files mf ON mf.id = f.media_file_id
        JOIN v_face_cluster_current fcc ON fcc.face_guid = f.face_guid
        JOIN v_cluster_person_current cpc ON cpc.cluster_guid = fcc.cluster_guid
        JOIN persons p ON p.person_guid = cpc.person_guid
        WHERE p.id = ?
    """, (person_id,))
    
    misplaced = []
    weak = []
    correct = 0
    
    for row in rows:
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        
        # Similarity to current person's anchors
        own_sim = best_anchor_similarity(vec, person_anchors) if person_anchors else 0.0
        
        # Best similarity to any OTHER person's anchors
        best_other_pid = None
        best_other_sim = 0.0
        for pid, anchors in all_anchors.items():
            if pid == person_id:
                continue
            sim = best_anchor_similarity(vec, anchors)
            if sim > best_other_sim:
                best_other_sim = sim
                best_other_pid = pid
        
        if best_other_sim > own_sim + 0.05:
            # This face matches another person BETTER
            misplaced.append({
                "face_id": row["face_id"],
                "file_path": row["file_path"],
                "date_taken": row["date_taken"],
                "own_sim": own_sim,
                "better_person_id": best_other_pid,
                "better_sim": best_other_sim,
                "margin": best_other_sim - own_sim,
            })
        elif own_sim < 0.60:
            # Weakly assigned — not clearly wrong, but not confident
            weak.append({
                "face_id": row["face_id"],
                "own_sim": own_sim,
                "best_other_pid": best_other_pid,
                "best_other_sim": best_other_sim,
            })
        else:
            correct += 1
    
    return {
        "total_faces": len(rows),
        "correct": correct,
        "misplaced": misplaced,
        "weak": weak,
        "misplaced_count": len(misplaced),
        "weak_count": len(weak),
        "accuracy": correct / len(rows) if rows else 1.0,
    }
```

### 11.7 Remediation API Endpoints

**File:** `backend/api/routes/persons.py` — add:

```python
@router.post("/remediation/audit")
async def trigger_remediation_audit():
    """
    Start a full audit of all named persons.
    Returns immediately; audit runs in background.
    """
    # Queue as background task
    asyncio.create_task(_run_audit_background())
    return {"status": "started", "message": "Audit started in background"}


@router.get("/remediation/status")
async def get_remediation_status():
    """Current remediation progress and pending issues."""
    async with get_db() as db:
        runs = await db.execute_fetchall(
            "SELECT * FROM remediation_runs ORDER BY started_at DESC LIMIT 5"
        )
        pending = await db.execute_fetchone(
            "SELECT COUNT(*) as n FROM remediation_log WHERE status='pending'"
        )
        by_action = await db.execute_fetchall("""
            SELECT action, COUNT(*) as count, SUM(face_count) as total_faces
            FROM remediation_log WHERE status='pending'
            GROUP BY action
        """)
        return {
            "recent_runs": [dict(r) for r in runs],
            "pending_issues": pending["n"],
            "by_action": [dict(r) for r in by_action],
        }


@router.post("/remediation/reclaim")
async def trigger_face_reclaim():
    """
    Re-evaluate all unnamed clusters against person anchors.
    High-confidence matches are auto-assigned; medium-confidence create suggestions.
    """
    asyncio.create_task(_run_reclaim_background())
    return {"status": "started"}


@router.get("/remediation/person/{person_id}")
async def get_person_remediation_details(person_id: int):
    """Get audit/remediation details for a specific person."""
    async with get_db() as db:
        audit = await audit_person_integrity(db, person_id)
        verification = await verify_person_assignments(db, person_id)
        pending_logs = await db.execute_fetchall("""
            SELECT * FROM remediation_log
            WHERE person_id = ? AND status = 'pending'
            ORDER BY created_at DESC
        """, (person_id,))
        return {
            "audit": audit,
            "verification": verification,
            "pending_actions": [dict(r) for r in pending_logs],
        }


@router.post("/remediation/person/{person_id}/apply")
async def apply_person_remediation(person_id: int, auto_apply: bool = False):
    """
    Apply remediation for a specific person.
    With auto_apply=True, high-confidence corrections are applied automatically.
    With auto_apply=False (default), all changes become suggestions for review.
    """
    async with get_db() as db:
        result = await remediate_outlier_faces(db, person_id, auto_apply=auto_apply)
        return result


@router.post("/remediation/contamination-check")
async def trigger_contamination_check():
    """Find persons whose anchors overlap (potential duplicates or wrong merges)."""
    async with get_db() as db:
        pairs = await detect_cross_person_contamination(db)
        return {
            "pairs_found": len(pairs),
            "high_confidence_merges": sum(1 for p in pairs if p["suggestion"] == "merge"),
            "needs_review": sum(1 for p in pairs if p["suggestion"] == "review"),
            "pairs": pairs[:50],  # cap response size
        }
```

### 11.8 Remediation Orchestration — Full Run Order

The complete remediation should be run as a multi-step background process AFTER all person anchors are initially built (Phase 10 background builder completes):

```python
async def run_full_remediation(db):
    """
    Complete remediation pipeline for existing data.
    Should be triggered once after initial anchor build completes.
    
    Order matters:
    1. Audit: find problems
    2. Cross-contamination: find duplicate persons
    3. Verify: per-person face verification
    4. Reassign: move outlier faces
    5. Reclaim: bring back missed faces
    6. Recompute: update all affected centroids/anchors
    """
    logger.info("=== Starting full data remediation ===")
    
    # Step 1: Audit all persons
    logger.info("Step 1/6: Running integrity audit...")
    audit_result = await run_full_audit(db)
    logger.info(f"Audit complete: {audit_result['issues_found']} persons with issues")
    
    # Step 2: Cross-contamination detection
    logger.info("Step 2/6: Checking cross-person contamination...")
    contamination = await detect_cross_person_contamination(db)
    logger.info(f"Found {len(contamination)} overlapping person pairs")
    
    # Step 3: Verify top persons (by face count, most likely to have drift)
    logger.info("Step 3/6: Verifying top persons by face count...")
    top_persons = await db.execute_fetchall("""
        SELECT p.id, p.name, COUNT(f.id) as face_count
        FROM persons p
        JOIN v_cluster_person_current cpc ON cpc.person_guid = p.person_guid
        JOIN v_face_cluster_current fcc ON fcc.cluster_guid = cpc.cluster_guid
        JOIN faces f ON f.face_guid = fcc.face_guid
        WHERE p.is_merged = 0 AND p.is_ignored = 0 AND p.name IS NOT NULL
        GROUP BY p.id
        HAVING face_count >= 100
        ORDER BY face_count DESC
    """)
    for person in top_persons:
        verification = await verify_person_assignments(db, person["id"])
        if verification["misplaced_count"] > 0:
            await db.execute("""
                INSERT INTO remediation_log (person_id, action, status, details, face_count)
                VALUES (?, 'reassign', 'pending', ?, ?)
            """, (
                person["id"],
                json.dumps({"misplaced": verification["misplaced"][:100]}),
                verification["misplaced_count"],
            ))
    logger.info(f"Verified {len(top_persons)} persons with 100+ faces")
    
    # Step 4: Auto-apply high-confidence reassignments
    logger.info("Step 4/6: Applying high-confidence reassignments...")
    persons_with_issues = await db.execute_fetchall("""
        SELECT DISTINCT person_id FROM remediation_log
        WHERE action = 'reassign' AND status = 'pending'
    """)
    total_reassigned = 0
    for row in persons_with_issues:
        result = await remediate_outlier_faces(db, row["person_id"], auto_apply=True)
        total_reassigned += result.get("reassigned", 0)
    logger.info(f"Auto-reassigned {total_reassigned} faces")
    
    # Step 5: Reclaim missed faces
    logger.info("Step 5/6: Reclaiming missed faces from unnamed pool...")
    reclaim_result = await reclaim_missed_faces(db)
    logger.info(
        f"Reclaimed {reclaim_result['faces_auto_assigned']} faces, "
        f"suggested {reclaim_result['faces_suggested']} more"
    )
    
    # Step 6: Recompute anchors for all affected persons
    logger.info("Step 6/6: Recomputing anchors for affected persons...")
    affected_persons = await db.execute_fetchall("""
        SELECT DISTINCT person_id FROM remediation_log
        WHERE status = 'applied' AND resolved_at >= datetime('now', '-1 hour')
    """)
    for row in affected_persons:
        await update_person_centroid(db, row["person_id"])
    logger.info(f"Recomputed anchors for {len(affected_persons)} persons")
    
    logger.info("=== Full remediation complete ===")
    
    # Summary
    return {
        "audit_issues": audit_result["issues_found"],
        "contamination_pairs": len(contamination),
        "persons_verified": len(top_persons),
        "faces_reassigned": total_reassigned,
        "faces_reclaimed": reclaim_result["faces_auto_assigned"],
        "faces_suggested": reclaim_result["faces_suggested"],
        "anchors_recomputed": len(affected_persons),
    }
```

### 11.9 Settings for Remediation

**File:** `backend/database/settings_store.py` — add:

```python
"remediation_auto_apply": {
    "value": 1, "type": "bool",
    "label": "Auto-apply high-confidence corrections",
    "description": "When remediation finds a face clearly belonging to a different person (sim > 0.90 with margin > 0.10), auto-reassign without user review.",
    "group": "Remediation",
},
"remediation_outlier_threshold": {
    "value": 0.55, "type": "float", "min": 0.40, "max": 0.70, "step": 0.05,
    "label": "Outlier detection threshold",
    "description": "Faces with best-anchor similarity below this are flagged as potential wrong assignments.",
    "group": "Remediation",
},
"remediation_reclaim_auto_threshold": {
    "value": 0.90, "type": "float", "min": 0.80, "max": 0.98, "step": 0.01,
    "label": "Auto-reclaim threshold",
    "description": "Unnamed faces matching a person's anchor above this (with sufficient margin) are auto-assigned during reclaim.",
    "group": "Remediation",
},
"remediation_reclaim_suggest_threshold": {
    "value": 0.75, "type": "float", "min": 0.60, "max": 0.90, "step": 0.05,
    "label": "Reclaim suggestion threshold",
    "description": "Unnamed faces matching above this but below auto-reclaim generate suggestions for user review.",
    "group": "Remediation",
},
```

### 11.10 Remediation Trigger Points

The full remediation runs **once** after the initial anchor build is complete. Subsequent incremental remediations run:

1. **After each pipeline run** (in `_phase_auto_merge` epilogue): Quick reclaim pass for just the newly-ingested faces against all anchors.
2. **On idle** (suggestion worker): Process one pending remediation_log item per tick.
3. **On user request** via API: Trigger specific person verification or full re-audit.

**Integration into suggestion worker:**

```python
# In suggestion_worker.py main loop:
async def _idle_tick(db):
    # Priority 1: Build missing anchors
    if await _build_missing_anchors(db):
        return
    
    # Priority 2: Rebalance stale anchors
    if await _rebalance_stale_anchors(db):
        return
    
    # Priority 3: Process pending remediation
    if await _process_one_remediation_item(db):
        return
    
    # Priority 4: Normal suggestion generation
    await _generate_suggestions(db)
```

### 11.11 Validation for Remediation

- **After audit:** Check `remediation_log` has entries for persons with known issues
- **After reclaim:** Verify previously-unnamed faces of known persons are now correctly assigned
- **After contamination check:** Known problematic person pairs (if any) are surfaced
- **After full remediation:** Run `verify_person_assignments` on the worst-affected persons and confirm `accuracy` metric improved
- **Regression guard:** After remediation, run a pipeline ingest on new photos and verify the auto-name rate improved (more faces correctly identified without user intervention)

---

## Execution Order & Dependencies

```
Phase 1 (Schema)
    ↓
Phase 2 (Computation engine)  ←  can be tested in isolation
    ↓
Phase 3 (Matching integration)  ←  first user-visible improvement
    ↓
Phase 4 (Diversity selection)  ←  improves anchor quality
    ↓
Phase 5 (Temporal stratification)  ←  handles aging better
    ↓
Phase 6 (FAISS cascade)  ←  performance at scale
    ↓
Phase 7 (Incremental updates)  ←  efficiency for ongoing use
    ↓
Phase 8 (Health monitoring)  ←  observability & maintenance
    ↓
Phase 9 (Age-gap bridging)  ←  edge case handling
    ↓
Phase 10 (Migration strategy)  ←  should be built alongside Phase 1-3
    ↓
Phase 11 (Existing data remediation)  ←  runs after Phase 10 anchor build completes
```

**Minimum viable improvement:** Phases 1 + 2 + 3 + 10 deliver the core fix. Phase 11 heals historical data. Phases 4-9 are progressive enhancements.

**Remediation dependency:** Phase 11 REQUIRES Phases 1-3 and 10 to be complete (anchors must exist before auditing against them). It can run in parallel with Phases 4-9.

---

## Validation Approach

After each phase, the following can be verified:

- **Phase 1:** `sqlite3 <db_path> ".schema person_anchors"` shows table
- **Phase 2:** Run `update_person_anchors(db, person_id)` for a known problem person; verify multiple anchors created with reasonable intra-similarity
- **Phase 3:** Run pipeline on new photos of known person; verify auto-name fires at lower threshold with anchor match
- **Phase 4:** Compare `person_centroid_faces` selection before/after; verify diversity (inspect `date_taken` spread and pose variance)
- **Phase 5:** Verify anchors have non-null `time_range_start`/`time_range_end` and are ordered temporally
- **Phase 6:** Benchmark: time auto-merge phase with 500 persons × clusters. Should be < 2s
- **Phase 7:** Add 5 faces to person; verify only anchor centroid updates (no full recompute log message)
- **Phase 8:** Call health endpoint; verify spread/coverage metrics are reasonable
- **Phase 9:** For person spanning 20+ years, verify chain connectivity logged
- **Phase 11:** After remediation, verify: (a) outlier faces moved to correct person, (b) unnamed pool shrank as faces reclaimed, (c) no new false positives introduced
