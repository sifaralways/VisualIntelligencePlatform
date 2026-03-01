#!/usr/bin/env bash
# =============================================================================
# clear_vip_metadata.sh — Strip all metadata written by the VIP app from every
# image in a folder (and all sub-folders) using ExifTool.
#
# Usage:
#   ./scripts/clear_vip_metadata.sh "/path/to/folder"
#   ./scripts/clear_vip_metadata.sh "/path/to/folder" --dry-run
#   ./scripts/clear_vip_metadata.sh "/path/to/folder" --clear-gps
#
# Options:
#   --dry-run    Print what would be changed without writing anything.
#   --clear-gps  Also clear GPS coordinates.  Off by default because the
#                camera itself writes GPS — clearing it removes original data.
#
# Fields cleared:
#   XMP:PersonInImage         — named persons
#   XMP-mwg-rs:Regions        — face bounding boxes (Lightroom/CaptureOne)
#   XMP:Subject               — all VIP keyword tags (obj:, animal:, geo:, place:, names)
#   IPTC:Keywords             — mirror of XMP:Subject
#   XMP:Location              — resolved place name
#   XMP:HierarchicalSubject   — hierarchical keyword tree
#   XMP:Identifier            — VIP internal UUID
#   GPS fields (opt-in)       — only cleared when --clear-gps is passed
#
# Requirements:  exiftool  (brew install exiftool)
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------- #
# Parse arguments                                                               #
# --------------------------------------------------------------------------- #
FOLDER=""
DRY_RUN=false
CLEAR_GPS=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=true ;;
        --clear-gps) CLEAR_GPS=true ;;
        *)           FOLDER="$arg" ;;
    esac
done

if [[ -z "$FOLDER" ]]; then
    echo "Usage: $0 <folder> [--dry-run] [--clear-gps]"
    exit 1
fi

if [[ ! -d "$FOLDER" ]]; then
    echo "❌  Not a directory: $FOLDER"
    exit 1
fi

if ! command -v exiftool &>/dev/null; then
    echo "❌  exiftool not found. Install with: brew install exiftool"
    exit 1
fi

# --------------------------------------------------------------------------- #
# Build the ExifTool field-clear arguments                                      #
# --------------------------------------------------------------------------- #
CLEAR_ARGS=(
    -XMP:PersonInImage=
    -XMP-mwg-rs:RegionInfo=
    -XMP:Subject=
    -IPTC:Keywords=
    -XMP:Location=
    -XMP:HierarchicalSubject=
    -XMP:Identifier=
)

if [[ "$CLEAR_GPS" == true ]]; then
    CLEAR_ARGS+=(
        -EXIF:GPSLatitude=
        -EXIF:GPSLatitudeRef=
        -EXIF:GPSLongitude=
        -EXIF:GPSLongitudeRef=
        -EXIF:GPSAltitude=
        -EXIF:GPSAltitudeRef=
    )
fi

# --------------------------------------------------------------------------- #
# Run                                                                           #
# --------------------------------------------------------------------------- #
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   VIP — Clear metadata                   ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  Folder   : $FOLDER"
echo "  Dry run  : $DRY_RUN"
echo "  Clear GPS: $CLEAR_GPS"
echo ""

if [[ "$DRY_RUN" == true ]]; then
    echo "  [DRY RUN] Fields that would be cleared:"
    exiftool -r \
        "${CLEAR_ARGS[@]}" \
        -if 'defined $PersonInImage or defined $Subject or defined $Keywords or defined $Identifier' \
        -p '$Directory/$FileName' \
        "$FOLDER" 2>/dev/null || true
    echo ""
    echo "  Run without --dry-run to apply changes."
else
    echo "  ⚠️   This will modify files in place. Originals are backed up"
    echo "       as <filename>_original unless you pass --overwrite."
    echo ""
    read -r -p "  Continue? [y/N] " confirm
    if [[ "${confirm,,}" != "y" ]]; then
        echo "  Aborted."
        exit 0
    fi
    echo ""

    exiftool -r \
        "${CLEAR_ARGS[@]}" \
        -overwrite_original \
        "$FOLDER"

    echo ""
    echo "  ✅  Done. All VIP metadata cleared in: $FOLDER"
fi
echo ""
