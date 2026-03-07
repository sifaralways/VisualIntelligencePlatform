"""
VIP Writeback — XMP field definitions and mapping.

Fields written per photo:

  Person      → XMP:PersonInImage        (IPTC Extension, industry standard)
                XMP-mwg-rs:Regions       (MWG face boxes, Lightroom/CaptureOne)

  Object      → XMP:Subject keyword prefix "obj:"
                e.g. "obj:Car", "obj:TV", "obj:Appliance"

  Geography   → XMP:Subject keyword prefix "geo:"
                e.g. "geo:Mountains", "geo:Ocean", "geo:Forest"

  Places      → XMP:Location (IPTC Core free-text)
                XMP:Subject keyword prefix "place:"
                e.g. "place:Taj Mahal", "place:Harbour Bridge"
                If GPS coords available: also written to EXIF:GPSLatitude/Longitude

  IPTC:Keywords mirrors the full XMP:Subject array for broadest compatibility.

Internal UUIDs and embeddings are NOT written — DB-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Prefixes used by VIP when writing keywords to XMP:Subject.
# Used by the writeback engine to partition VIP-managed keywords
# from externally-added keywords (Lightroom, Photos, etc.) during
# merge-before-write so that external keywords are never wiped.
VIP_SUBJECT_PREFIXES: tuple[str, ...] = ("obj:", "animal:", "geo:", "place:")


@dataclass
class FaceRegion:
    """A named face region for MWG RegionInfo."""
    name: str
    x: float    # normalised centre x
    y: float    # normalised centre y
    w: float    # normalised width
    h: float    # normalised height


def build_field_map(
    person_names: list[str] | None = None,
    face_regions: list[FaceRegion] | None = None,
    objects: list[str] | None = None,
    animals: list[str] | None = None,
    geography: list[str] | None = None,
    places: list[str] | None = None,
    gps_lat: float | None = None,
    gps_lon: float | None = None,
    vip_id: str | None = None,
    hierarchical_subjects: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the ExifTool field dictionary for a media file.

    Args:
        person_names:          Names of identified people.
        face_regions:          Bounding boxes for MWG region metadata.
        objects:               Object labels (Car, TV, Appliance …).
        animals:               Animal/species labels (Dog, Golden Retriever …).
        geography:             Scene labels (Mountains, Ocean, Forest …).
        places:                Landmark / place names (Taj Mahal, Harbour Bridge …).
        gps_lat/lon:           GPS coordinates to write if not already present.
        vip_id:                Stable app UUID — written to XMP:Identifier for file tracking.
        hierarchical_subjects: "Category|Parent|Label" paths for XMP:HierarchicalSubject.
                               Standard Lightroom/Bridge hierarchical keyword format.

    Returns:
        Dict suitable for ExifToolWriter.write(file_path, fields=…)
    """
    fields: dict[str, Any] = {}
    all_keywords: list[str] = []

    # ── Stable app UUID ────────────────────────────────────────────────────
    if vip_id:
        fields["XMP:Identifier"] = vip_id

    # ── Person ──────────────────────────────────────────────────────────────
    if person_names:
        fields["XMP:PersonInImage"] = person_names
        all_keywords.extend(person_names)

    if face_regions:
        fields["XMP-mwg-rs:Regions"] = _build_mwg_regions(face_regions)

    # ── Object ──────────────────────────────────────────────────────────────
    if objects:
        all_keywords.extend(f"obj:{o}" for o in objects)

    # ── Animal ──────────────────────────────────────────────────────────────
    if animals:
        all_keywords.extend(f"animal:{a}" for a in animals)

    # ── Geography ───────────────────────────────────────────────────────────
    if geography:
        all_keywords.extend(f"geo:{g}" for g in geography)

    # ── Places ──────────────────────────────────────────────────────────────
    if places:
        fields["XMP:Location"] = places[0] if len(places) == 1 else "; ".join(places)
        all_keywords.extend(f"place:{p}" for p in places)

    # ── GPS ─────────────────────────────────────────────────────────────────
    if gps_lat is not None and gps_lon is not None:
        try:
            gps_lat = float(gps_lat)
            gps_lon = float(gps_lon)
        except (ValueError, TypeError):
            gps_lat = gps_lon = None
    if gps_lat is not None and gps_lon is not None:
        fields["EXIF:GPSLatitude"] = abs(gps_lat)
        fields["EXIF:GPSLatitudeRef"] = "N" if gps_lat >= 0 else "S"
        fields["EXIF:GPSLongitude"] = abs(gps_lon)
        fields["EXIF:GPSLongitudeRef"] = "E" if gps_lon >= 0 else "W"

    # ── Hierarchical keywords (XMP:HierarchicalSubject — Lightroom/Bridge) ──
    # Format: ["Category|Parent|Label", …] — enables nested keyword panels.
    if hierarchical_subjects:
        fields["XMP:HierarchicalSubject"] = hierarchical_subjects

    # ── Flat keyword arrays (Subject + IPTC:Keywords for compatibility) ──────
    if all_keywords:
        fields["XMP:Subject"] = all_keywords
        fields["IPTC:Keywords"] = all_keywords

    return fields


def _build_mwg_regions(regions: list[FaceRegion]) -> list[str]:
    """
    Format face regions for ExifTool's XMP-mwg-rs struct syntax.
    ExifTool CLI format:
      {Type=Face,Name=Alice,Area={X=0.5,Y=0.3,W=0.15,H=0.2,Unit=normalized}}
    """
    result = []
    for r in regions:
        result.append(
            f"{{Type=Face,Name={r.name},"
            f"Area={{X={r.x:.4f},Y={r.y:.4f},W={r.w:.4f},H={r.h:.4f},Unit=normalized}}}}"
        )
    return result
