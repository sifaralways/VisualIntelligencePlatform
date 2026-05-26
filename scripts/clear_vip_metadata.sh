#!/usr/bin/env bash
# =============================================================================
# clear_vip_metadata.sh — Strip all metadata written by the VIP app from every
# image in a folder (and all sub-folders) using ExifTool.
#
# Usage:
#   ./scripts/clear_vip_metadata.sh "/path/to/folder"
#   ./scripts/clear_vip_metadata.sh "/path/to/folder" --dry-run
#   ./scripts/clear_vip_metadata.sh "/path/to/folder" --clear-gps
#   ./scripts/clear_vip_metadata.sh "/path/to/folder" --yes
#
# Options:
#   --dry-run    Print what would be changed without writing anything.
#   --clear-gps  Also clear GPS coordinates.  Off by default because the
#                camera itself writes GPS — clearing it removes original data.
#   --yes        Run non-interactively (skip confirmation prompt).
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
ASSUME_YES=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=true ;;
        --clear-gps) CLEAR_GPS=true ;;
        --yes|-y)    ASSUME_YES=true ;;
        *)           FOLDER="$arg" ;;
    esac
done

if [[ -z "$FOLDER" ]]; then
    echo "Usage: $0 <folder> [--dry-run] [--clear-gps] [--yes]"
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
    echo "  [DRY RUN] Image files found (fields would be cleared on each):"
    echo ""
    # List every image file exiftool can read, with its current values for VIP fields
    exiftool -r -q \
        -ext cr3 -ext arw -ext nef -ext dng -ext rw2 -ext orf -ext raf -ext cr2 \
        -ext jpg -ext jpeg -ext heic -ext heif -ext png -ext webp -ext tiff -ext tif -ext avif \
        -p '  $Directory/$FileName' \
        "$FOLDER" 2>/dev/null | sort || true
    echo ""
    # Show a sample of current VIP field values from the first matching file
    echo "  [DRY RUN] VIP field values in sample file (empty = not yet written):"
    exiftool -r -q \
        -ext cr3 -ext arw -ext nef -ext dng -ext rw2 -ext orf -ext raf -ext cr2 \
        -ext jpg -ext jpeg -ext heic -ext heif -ext png -ext webp -ext tiff -ext tif -ext avif \
        -PersonInImage -Subject -Keywords -Location -HierarchicalSubject -Identifier \
        -fileOrder FileName \
        "$FOLDER" 2>/dev/null | head -40 || true
    echo ""
    echo "  Run without --dry-run to apply changes."
else
    echo "  ⚠️   This will modify files in place. Originals are backed up"
    echo "       only if exiftool is configured to keep backups."
    echo "       This script uses -overwrite_original (no *_original backups)."
    echo ""
    if [[ "$ASSUME_YES" != true ]]; then
        read -r -p "  Continue? [y/N] " confirm
        confirm_lower=$(echo "$confirm" | tr '[:upper:]' '[:lower:]')
        if [[ "$confirm_lower" != "y" ]]; then
            echo "  Aborted."
            exit 0
        fi
    fi
    echo ""

    # ── First pass: run exiftool on all files normally ────────────────────
    # Tee stderr to both the terminal (live) and a temp log so we can parse
    # format-mismatch errors for the second pass without silencing output.
    ERRLOG=$(mktemp)
    exiftool -r \
        "${CLEAR_ARGS[@]}" \
        -overwrite_original \
        -preserve \
        "$FOLDER" 2> >(tee "$ERRLOG" >&2)

    # ── Second pass: handle extension/content mismatches ─────────────────
    # Google Photos Takeout sometimes keeps the original extension (e.g. .DNG)
    # but exports the file as a different format (e.g. JPEG).  ExifTool refuses
    # to write to these files because the extension implies one parser but the
    # magic bytes imply another.
    # Fix: create a temp symlink with the correct extension so ExifTool uses
    # the right parser, process via the symlink, then remove it.
    #
    # Error line format:
    #   Error: Not a valid FOO (looks more like a BAR) - /path/to/file
    mismatch_count=0
    mismatch_errors=0
    while IFS= read -r errline; do
        # Match the format-mismatch error line
        if [[ "$errline" =~ ^[[:space:]]*Error:[[:space:]]*Not\ a\ valid\ [A-Za-z0-9]+\ \(looks\ more\ like\ a\ ([A-Za-z0-9]+)\)\ -\ (.+)$ ]]; then
            actual_fmt="${BASH_REMATCH[1]}"
            filepath="${BASH_REMATCH[2]}"

            # Map ExifTool format name → file extension
            case "${actual_fmt^^}" in
                JPEG|JPG) ext="jpg" ;;
                PNG)      ext="png" ;;
                TIFF|TIF) ext="tif" ;;
                WEBP)     ext="webp" ;;
                HEIC)     ext="heic" ;;
                CR2)      ext="cr2" ;;
                CR3)      ext="cr3" ;;
                ARW)      ext="arw" ;;
                NEF)      ext="nef" ;;
                *)        ext="${actual_fmt,,}" ;;
            esac

            # Create a temp directory + symlink with the correct extension
            tmpdir=$(mktemp -d)
            tmplink="$tmpdir/vip_reprocess.$ext"
            ln -s "$filepath" "$tmplink"

            echo "  ↻  Re-processing as $actual_fmt: $filepath"
            if exiftool \
                "${CLEAR_ARGS[@]}" \
                -overwrite_original \
                -preserve \
                "$tmplink" 2>&1; then
                mismatch_count=$((mismatch_count + 1))
            else
                echo "  ⚠️  Could not process: $filepath"
                mismatch_errors=$((mismatch_errors + 1))
            fi

            rm -rf "$tmpdir"
        fi
    done < <(grep -E "Not a valid .+ \(looks more like" "$ERRLOG" || true)

    rm -f "$ERRLOG"

    echo ""
    if [[ $mismatch_count -gt 0 ]]; then
        echo "  ↻  $mismatch_count format-mismatched file(s) re-processed successfully."
    fi
    if [[ $mismatch_errors -gt 0 ]]; then
        echo "  ⚠️  $mismatch_errors format-mismatched file(s) could not be processed."
    fi
    echo "  ✅  Done. All VIP metadata cleared in: $FOLDER"
fi
echo ""
