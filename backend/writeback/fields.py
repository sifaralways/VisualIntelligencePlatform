"""
VIP Writeback — XMP field definitions and mapping.

Maps our internal Person model to ExifTool tag names.

Standard fields written (Spotlight-visible, app-agnostic):
  • XMP:PersonInImage       — IPTC Extension, person names array
  • XMP:Subject             — searchable keywords (names + scene tags)
  • IPTC:Keywords           — same as Subject, wider compatibility
  • XMP-mwg-rs:Regions      — MWG face regions (compatible with Lightroom, Capture One)

Internal UUIDs and embeddings are NOT written to files — DB-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FaceRegion:
    """A named face region for MWG RegionInfo."""
    name: str
    x: float    # normalised centre x
    y: float    # normalised centre y
    w: float    # normalised width
    h: float    # normalised height


def build_field_map(
    person_names: list[str],
    face_regions: list[FaceRegion] | None = None,
    extra_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the ExifTool field dictionary for a media file.

    Args:
        person_names:    Names of all identified people in the photo.
        face_regions:    Optional bounding box + name for MWG region metadata.
        extra_keywords:  Additional tags (scene, object labels) from Phase 6+.

    Returns:
        Dict suitable for ExifToolWriter.write(file_path, fields=...)
    """
    fields: dict[str, Any] = {}

    if person_names:
        fields["XMP:PersonInImage"] = person_names

        # Subject / Keywords — merge persons + extra keywords
        all_keywords = list(person_names)
        if extra_keywords:
            all_keywords.extend(extra_keywords)
        fields["XMP:Subject"] = all_keywords
        fields["IPTC:Keywords"] = all_keywords

    if face_regions:
        # MWG face region format
        # ExifTool writes these as XMP struct — one entry per face
        fields["XMP-mwg-rs:Regions"] = _build_mwg_regions(face_regions)

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
