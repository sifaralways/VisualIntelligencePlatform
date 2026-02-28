"""
VIP ML — Analysis document builder.

Assembles a Rekognition-compatible JSON document for a single media file by
joining data from media_files, media_tags, faces, and persons.

The resulting document is stored in photo_analysis.model_document and is
served by GET /api/analysis/{media_id}.

Schema:
  {
    "schema_version": "1.0",
    "vip_id":         "<UUID>",
    "media_id":       42,
    "file_path":      "/path/to/photo.cr3",
    "date_taken":     "2024-05-20T14:30:00",
    "camera":         "Canon EOS R5",
    "Labels":         [...],   # objects, animals, scenes, places — Rekognition format
    "Faces":          [...],   # face detections with bbox + person_id (not name)
    "Geography":      {...},   # GPS + reverse-geocode result
    "model_version":  "...",
    "generated_at":   "..."
  }

Notes:
- person_name is NEVER stored in the document — it lives in persons.name and
  is JOIN-resolved at API read time so that renaming a person instantly
  updates every photo without regenerating any document.
- Bounding boxes (Instances) are populated for Person labels via the faces table.
  For other objects, Instances is an empty list (acceptable per Rekognition schema).
- Confidence values come from media_tags.confidence when available; otherwise
  a per-model default is used.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

from backend.config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Static label taxonomy
# Maps lowercase label name → (list of parent names, top-level category)
# Used to populate Labels[].Parents and Labels[].Categories in the document.
# Labels not in this map get empty parents and an inferred category.
# ─────────────────────────────────────────────────────────────────────────────
_TAXONOMY: dict[str, tuple[list[str], str]] = {
    # ── People ────────────────────────────────────────────────────────────────
    "person":           ([], "Person Description"),
    "people":           (["Person"], "Person Description"),
    "crowd":            (["Person", "People"], "Person Description"),
    "group":            (["Person", "People"], "Person Description"),
    # ── Animals & Pets ────────────────────────────────────────────────────────
    "animal":           ([], "Animals and Pets"),
    "dog":              (["Animal", "Pet"], "Animals and Pets"),
    "cat":              (["Animal", "Pet"], "Animals and Pets"),
    "bird":             (["Animal"], "Animals and Pets"),
    "horse":            (["Animal"], "Animals and Pets"),
    "cow":              (["Animal"], "Animals and Pets"),
    "sheep":            (["Animal"], "Animals and Pets"),
    "bear":             (["Animal"], "Animals and Pets"),
    "elephant":         (["Animal"], "Animals and Pets"),
    "tiger":            (["Animal"], "Animals and Pets"),
    "lion":             (["Animal"], "Animals and Pets"),
    "deer":             (["Animal"], "Animals and Pets"),
    "rabbit":           (["Animal", "Pet"], "Animals and Pets"),
    "fish":             (["Animal"], "Animals and Pets"),
    "insects":          (["Animal"], "Animals and Pets"),
    "butterfly":        (["Animal", "Insect"], "Animals and Pets"),
    # ── Nature & Outdoors ─────────────────────────────────────────────────────
    "outdoors":         ([], "Nature and Outdoors"),
    "nature":           (["Outdoors"], "Nature and Outdoors"),
    "scenery":          (["Nature", "Outdoors"], "Nature and Outdoors"),
    "landscape":        (["Nature", "Outdoors"], "Nature and Outdoors"),
    "field":            ([], "Nature and Outdoors"),
    "grassland":        (["Field", "Nature"], "Nature and Outdoors"),
    "meadow":           (["Field", "Grassland", "Nature"], "Nature and Outdoors"),
    "forest":           (["Nature", "Outdoors"], "Nature and Outdoors"),
    "jungle":           (["Forest", "Nature"], "Nature and Outdoors"),
    "beach":            (["Nature", "Outdoors"], "Nature and Outdoors"),
    "ocean":            (["Water", "Nature"], "Nature and Outdoors"),
    "sea":              (["Water", "Nature"], "Nature and Outdoors"),
    "lake":             (["Water", "Nature"], "Nature and Outdoors"),
    "river":            (["Water", "Nature"], "Nature and Outdoors"),
    "water":            ([], "Nature and Outdoors"),
    "mountain":         (["Nature", "Outdoors"], "Nature and Outdoors"),
    "mountains":        (["Mountain", "Nature"], "Nature and Outdoors"),
    "hill":             (["Nature", "Outdoors"], "Nature and Outdoors"),
    "valley":           (["Nature", "Outdoors"], "Nature and Outdoors"),
    "desert":           (["Nature", "Outdoors"], "Nature and Outdoors"),
    "snow":             (["Nature", "Outdoors"], "Nature and Outdoors"),
    "ice":              (["Nature", "Outdoors"], "Nature and Outdoors"),
    "sky":              (["Nature", "Outdoors"], "Nature and Outdoors"),
    "cloud":            (["Sky", "Nature"], "Nature and Outdoors"),
    "clouds":           (["Sky", "Nature"], "Nature and Outdoors"),
    "sunset":           (["Sky", "Nature"], "Nature and Outdoors"),
    "sunrise":          (["Sky", "Nature"], "Nature and Outdoors"),
    "countryside":      (["Nature", "Outdoors"], "Nature and Outdoors"),
    "rural":            (["Countryside", "Nature"], "Nature and Outdoors"),
    "park":             (["Outdoors"], "Nature and Outdoors"),
    # ── Plants & Flowers ──────────────────────────────────────────────────────
    "plant":            ([], "Plants and Flowers"),
    "tree":             (["Plant"], "Plants and Flowers"),
    "flower":           (["Plant"], "Plants and Flowers"),
    "grass":            (["Plant"], "Plants and Flowers"),
    "bush":             (["Plant"], "Plants and Flowers"),
    "vegetation":       (["Plant"], "Plants and Flowers"),
    # ── Buildings & Architecture ──────────────────────────────────────────────
    "architecture":     ([], "Buildings and Architecture"),
    "building":         (["Architecture"], "Buildings and Architecture"),
    "house":            (["Building", "Architecture"], "Buildings and Architecture"),
    "apartment":        (["Building", "Architecture"], "Buildings and Architecture"),
    "office":           (["Building", "Architecture"], "Buildings and Architecture"),
    "church":           (["Building", "Architecture"], "Buildings and Architecture"),
    "temple":           (["Building", "Architecture"], "Buildings and Architecture"),
    "mosque":           (["Building", "Architecture"], "Buildings and Architecture"),
    "bridge":           (["Architecture"], "Buildings and Architecture"),
    "tower":            (["Architecture", "Building"], "Buildings and Architecture"),
    "stadium":          (["Building", "Architecture"], "Buildings and Architecture"),
    "castle":           (["Building", "Architecture"], "Buildings and Architecture"),
    "ruins":            (["Architecture"], "Buildings and Architecture"),
    "urban":            ([], "Buildings and Architecture"),
    "city":             (["Urban"], "Buildings and Architecture"),
    "street":           (["Urban"], "Buildings and Architecture"),
    "road":             ([], "Transportation"),
    # ── Vehicles & Transportation ─────────────────────────────────────────────
    "vehicle":          ([], "Vehicles and Transportation"),
    "car":              (["Vehicle"], "Vehicles and Transportation"),
    "truck":            (["Vehicle"], "Vehicles and Transportation"),
    "bus":              (["Vehicle"], "Vehicles and Transportation"),
    "van":              (["Vehicle"], "Vehicles and Transportation"),
    "motorcycle":       (["Vehicle"], "Vehicles and Transportation"),
    "bicycle":          (["Vehicle"], "Vehicles and Transportation"),
    "train":            (["Vehicle"], "Vehicles and Transportation"),
    "airplane":         (["Vehicle"], "Vehicles and Transportation"),
    "helicopter":       (["Vehicle"], "Vehicles and Transportation"),
    "boat":             (["Vehicle"], "Vehicles and Transportation"),
    "ship":             (["Vehicle", "Boat"], "Vehicles and Transportation"),
    # ── Food & Beverage ───────────────────────────────────────────────────────
    "food":             ([], "Food and Beverage"),
    "drink":            ([], "Food and Beverage"),
    "beverage":         (["Drink"], "Food and Beverage"),
    "fruit":            (["Food"], "Food and Beverage"),
    "vegetable":        (["Food"], "Food and Beverage"),
    "meal":             (["Food"], "Food and Beverage"),
    "bakery":           (["Food"], "Food and Beverage"),
    "dessert":          (["Food"], "Food and Beverage"),
    # ── Apparel & Accessories ─────────────────────────────────────────────────
    "clothing":         ([], "Apparel and Accessories"),
    "shirt":            (["Clothing"], "Apparel and Accessories"),
    "dress":            (["Clothing"], "Apparel and Accessories"),
    "suit":             (["Clothing"], "Apparel and Accessories"),
    "jacket":           (["Clothing"], "Apparel and Accessories"),
    "hat":              (["Clothing"], "Apparel and Accessories"),
    "sari":             (["Clothing"], "Apparel and Accessories"),
    "accessories":      ([], "Apparel and Accessories"),
    # ── Furniture & Furnishings ───────────────────────────────────────────────
    "furniture":        ([], "Furniture and Furnishings"),
    "chair":            (["Furniture"], "Furniture and Furnishings"),
    "table":            (["Furniture"], "Furniture and Furnishings"),
    "sofa":             (["Furniture"], "Furniture and Furnishings"),
    "bed":              (["Furniture"], "Furniture and Furnishings"),
    "desk":             (["Furniture"], "Furniture and Furnishings"),
    # ── Electronics & Technology ──────────────────────────────────────────────
    "electronics":      ([], "Electronics and Technology"),
    "phone":            (["Electronics"], "Electronics and Technology"),
    "laptop":           (["Electronics"], "Electronics and Technology"),
    "computer":         (["Electronics"], "Electronics and Technology"),
    "television":       (["Electronics"], "Electronics and Technology"),
    "tv":               (["Electronics", "Television"], "Electronics and Technology"),
    "camera":           (["Electronics"], "Electronics and Technology"),
    # ── Events & Attractions ──────────────────────────────────────────────────
    "party":            (["Fun"], "Events and Attractions"),
    "fun":              ([], "Events and Attractions"),
    "celebration":      ([], "Events and Attractions"),
    "wedding":          (["Celebration"], "Events and Attractions"),
    "festival":         (["Celebration"], "Events and Attractions"),
    "sport":            ([], "Sports and Activities"),
    "sports":           ([], "Sports and Activities"),
    # ── Travel & Adventure ────────────────────────────────────────────────────
    "travel":           ([], "Travel and Adventure"),
    "adventure":        ([], "Travel and Adventure"),
    "tourism":          (["Travel"], "Travel and Adventure"),
}

# Default confidence when not available from DB (model ran but didn't store score)
_DEFAULT_CONFIDENCE: dict[str, float] = {
    "yolov11":    0.75,
    "places365":  0.80,
    "bioclip":    0.75,
    "clip":       0.70,
    "nominatim":  0.95,
    "insightface": 0.90,
}

# Category → source model (for Source field in Labels)
_CATEGORY_SOURCE: dict[str, str] = {
    "object":    "yolov11",
    "animal":    "bioclip",
    "geography": "places365",
    "place":     "clip",
}

MODEL_VERSION = "yolo11s/places365/bioclip/insightface-buffalo_l"


def _lookup_taxonomy(label: str) -> tuple[list[str], str, list[str]]:
    """Return (parents, category, aliases) for a label.  Falls back gracefully."""
    entry = _TAXONOMY.get(label.lower())
    if entry:
        parents, category = entry
        return parents, category, []
    # Heuristic fallback
    return [], "General", []


def _label_entry(
    name: str,
    confidence: float,
    source: str,
    instances: list[dict] | None = None,
) -> dict:
    """Build a single Labels[] entry in Rekognition format."""
    parents, category, aliases = _lookup_taxonomy(name)
    return {
        "Name":       name,
        "Confidence": round(confidence * 100, 4),   # store as percent (0–100) like Rekognition
        "Source":     source,
        "Instances":  instances or [],
        "Parents":    [{"Name": p} for p in parents],
        "Categories": [{"Name": category}],
        "Aliases":    [{"Name": a} for a in aliases],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main builder
# ─────────────────────────────────────────────────────────────────────────────

async def build_analysis_document(
    media_id: int,
    db: aiosqlite.Connection,
) -> dict:
    """
    Build the full analysis document for a media file.

    Args:
        media_id:  Primary key of media_files row.
        db:        Open aiosqlite connection (caller manages transaction).

    Returns:
        Dict ready for json.dumps() and storage in photo_analysis.model_document.
        person_name is NOT included — resolved at API read time from persons table.
    """

    # ── 1. Media file metadata ───────────────────────────────────────────────
    row = await (await db.execute("""
        SELECT vip_id, file_path, date_taken, camera_make, camera_model,
               gps_lat, gps_lon, width, height, file_format
        FROM media_files WHERE id = ?
    """, (media_id,))).fetchone()

    if row is None:
        raise ValueError(f"media_id={media_id} not found")

    vip_id       = row["vip_id"]
    file_path    = row["file_path"]
    date_taken   = row["date_taken"]
    gps_lat      = row["gps_lat"]
    gps_lon      = row["gps_lon"]

    camera_parts = [p for p in [row["camera_make"], row["camera_model"]] if p]
    camera       = " ".join(camera_parts) if camera_parts else None

    # ── 2. ML-generated labels ───────────────────────────────────────────────
    tag_rows = await db.execute_fetchall("""
        SELECT category, label, confidence, model
        FROM media_tags
        WHERE media_file_id = ?
        ORDER BY category, rowid
    """, (media_id,))

    labels: list[dict] = []
    for t in tag_rows:
        cat        = t["category"]          # object | animal | geography | place
        label_name = t["label"]
        raw_conf   = t["confidence"]
        source     = _CATEGORY_SOURCE.get(cat, t["model"] or "unknown")

        confidence = raw_conf if raw_conf is not None else _DEFAULT_CONFIDENCE.get(source, 0.70)
        labels.append(_label_entry(label_name, confidence, source))

    # ── 3. Faces ─────────────────────────────────────────────────────────────
    face_rows = await db.execute_fetchall("""
        SELECT f.id, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h,
               f.detection_conf, f.person_id, f.face_attributes
        FROM faces f
        WHERE f.media_file_id = ?
        ORDER BY f.detection_conf DESC
    """, (media_id,))

    faces: list[dict] = []

    # Person instances for the Labels.Person entry (bbox per detected person)
    person_instances: list[dict] = []

    for f in face_rows:
        bbox = {
            "Left":   round(f["bbox_x"]  or 0.0, 6),
            "Top":    round(f["bbox_y"]  or 0.0, 6),
            "Width":  round(f["bbox_w"]  or 0.0, 6),
            "Height": round(f["bbox_h"]  or 0.0, 6),
        }
        conf = f["detection_conf"] or 0.0

        # Load stored rich attributes (age, gender, pose, landmarks, quality, stubs)
        attrs: dict = {}
        if f["face_attributes"]:
            try:
                attrs = json.loads(f["face_attributes"])
            except Exception:
                pass

        face_entry: dict = {
            "face_id":        f["id"],
            "person_id":      f["person_id"],   # null until named — resolved at read time
            "detection_conf": round(conf, 6),
            "bbox":           bbox,
            # ── Rekognition-format attributes ──────────────────────────────
            "AgeRange":    attrs.get("AgeRange"),       # {Low, High} or null
            "Gender":      attrs.get("Gender"),          # {Value, Confidence} or null
            "Pose":        attrs.get("Pose"),            # {Yaw, Pitch, Roll} or null
            "Landmarks":   attrs.get("Landmarks"),       # [{Type, X, Y}] or null
            "Quality":     attrs.get("Quality"),         # {Brightness, Sharpness} or null
            # ── Stub fields (future phases / extra models) ─────────────────
            "Smile":        attrs.get("Smile"),          # null until attribute model added
            "Eyeglasses":   attrs.get("Eyeglasses"),
            "Sunglasses":   attrs.get("Sunglasses"),
            "EyesOpen":     attrs.get("EyesOpen"),
            "MouthOpen":    attrs.get("MouthOpen"),
            "Beard":        attrs.get("Beard"),
            "Emotions":     attrs.get("Emotions"),       # null until DeepFace added
            "FaceOccluded": attrs.get("FaceOccluded"),
        }
        faces.append(face_entry)

        if f["bbox_x"] is not None:
            person_instances.append({
                "BoundingBox": bbox,
                "Confidence":  round(conf * 100, 4),
            })

    # Inject a "Person" label if any faces were detected
    if faces:
        person_label = _label_entry("Person", max(f["detection_conf"] for f in face_rows) or 0.9, "insightface", person_instances)
        person_label["Aliases"] = [{"Name": "Human"}]
        labels.insert(0, person_label)

    # ── 4. Geography ─────────────────────────────────────────────────────────
    # Pull place/geography tags into geography block for convenience
    geo_labels = [t["label"] for t in tag_rows if t["category"] in ("geography", "place")]
    geography: dict[str, object] = {
        "gps_lat": gps_lat,
        "gps_lon": gps_lon,
        "labels":  geo_labels,
    }

    # ── 5. Assemble document ─────────────────────────────────────────────────
    doc = {
        "schema_version": "1.0",
        "vip_id":         vip_id,
        "media_id":       media_id,
        "file_path":      file_path,
        "date_taken":     date_taken,
        "camera":         camera,
        "image_size":     {"width": row["width"], "height": row["height"]},
        "file_format":    row["file_format"],
        "Labels":         labels,
        "Faces":          faces,
        "Geography":      geography,
        "model_version":  MODEL_VERSION,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
    }
    return doc


async def save_analysis_document(media_id: int, doc: dict, db: aiosqlite.Connection) -> None:
    """Upsert the analysis document and record the model version."""
    doc_json = json.dumps(doc, ensure_ascii=False)
    await db.execute("""
        INSERT INTO photo_analysis (media_file_id, model_document, model_version, generated_at, updated_at)
        VALUES (?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(media_file_id) DO UPDATE SET
            model_document = excluded.model_document,
            model_version  = excluded.model_version,
            updated_at     = datetime('now')
    """, (media_id, doc_json, MODEL_VERSION))
