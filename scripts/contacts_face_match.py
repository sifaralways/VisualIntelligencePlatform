#!/usr/bin/env python3
"""
VIP Contacts Face Match Diagnostic
====================================
Read-only diagnostic script. Touches NO data in the VIP database.

What it does:
  1. Exports contacts with photos from macOS Contacts app via AppleScript.
  2. Runs each contact photo through the same InsightFace model VIP uses.
  3. Compares each contact embedding against all unnamed cluster centroids
     in the VIP database using cosine similarity.
  4. Prints a ranked suggestion table and saves a CSV.

Run from the repo root with the venv active:
    python scripts/contacts_face_match.py
    python scripts/contacts_face_match.py --threshold 0.55 --out contacts_matches.csv
"""

import argparse
import asyncio
import base64
import csv
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Repo root on sys.path so backend imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import settings
from backend.database.db import get_db


# ---------------------------------------------------------------------------
# Step 1 — Export contacts with photos via AppleScript
# ---------------------------------------------------------------------------

def export_contacts_with_photos() -> list[dict]:
    """
    Returns list of {name: str, image_bytes: bytes} for every contact
    that has a photo. Uses vCard AppleScript export.
    """
    import subprocess

    print("📱 Exporting contacts from macOS Contacts app…")
    print("   (You may see a permission prompt on first run — click Allow)")

    script = """
tell application "Contacts"
    set result_list to {}
    repeat with p in every person
        set hasImg to false
        try
            set imgData to image of p
            if imgData is not missing value then
                set hasImg to true
            end if
        end try
        if hasImg then
            set pName to (name of p) as text
            set vcText to vcard of p
            set end of result_list to pName & "~~SEP~~" & vcText & "~~END~~"
        end if
    end repeat
    return result_list as text
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"⚠️  AppleScript error: {result.stderr.strip()}", file=sys.stderr)
        return []

    raw = result.stdout.strip()
    if not raw:
        return []

    contacts = []
    records = [r.strip() for r in raw.split("~~END~~") if "~~SEP~~" in r]
    for record in records:
        name, _, vcard_text = record.partition("~~SEP~~")
        name = name.strip()
        img = _extract_photo_from_vcard(vcard_text)
        if img is not None:
            contacts.append({"name": name, "image_bytes": img})

    print(f"   Found {len(contacts)} contacts with photos.")
    return contacts


def _extract_photo_from_vcard(vcard_text: str) -> bytes | None:
    """
    Parse a vCard block and extract the embedded PHOTO as raw bytes.
    Handles vCard 3.0 (ENCODING=b) and 4.0 (data URI) formats.
    """
    lines = vcard_text.replace("\r\n", "\n").split("\n")
    # Unfold continuation lines (RFC 6350 §3.2)
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    b64: str | None = None
    for line in unfolded:
        upper = line.upper()
        if upper.startswith("PHOTO"):
            if "BASE64," in upper or "DATA:" in upper:
                # vCard 4.0: PHOTO:data:image/jpeg;base64,<b64>
                _, _, after = line.partition(",")
                b64 = after.strip()
                break
            if "ENCODING=B" in upper or "ENCODING=BASE64" in upper:
                # vCard 3.0: PHOTO;ENCODING=b;TYPE=JPEG:<b64>
                _, _, after = line.partition(":")
                b64 = after.strip()
                break

    if not b64:
        return None
    try:
        return base64.b64decode(b64 + "==")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Step 2 — Embed each contact photo using the VIP InsightFace model
# ---------------------------------------------------------------------------

def _load_detector():
    """Load the same InsightFace detector VIP uses (accuracy mode, CPU 1280)."""
    import backend.database.settings_store as ss
    from backend.ml.face_detector import FaceDetector

    asyncio.run(ss.load_cache())
    # Override to accuracy mode for contact photos — best embedding quality
    ss._cache["face_detection_mode"] = 0
    detector = FaceDetector()
    detector.load()
    return detector


def embed_contact(detector, image_bytes: bytes) -> "np.ndarray | None":
    """Detect dominant face in a contact photo and return its 512-D ArcFace embedding."""
    try:
        img_pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_arr = np.array(img_pil)
    except Exception:
        return None
    return detector.embed_from_array(img_arr)


# ---------------------------------------------------------------------------
# Step 3 — Load unnamed cluster centroids from the DB
# ---------------------------------------------------------------------------

async def _load_unnamed_clusters() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT c.id AS cluster_id, c.member_count, c.centroid,
                   MIN(f.thumbnail_path) AS rep_thumb
            FROM clusters c
            LEFT JOIN faces f ON f.cluster_id = c.id AND f.thumbnail_path IS NOT NULL
            WHERE c.person_id IS NULL
            GROUP BY c.id
            ORDER BY c.member_count DESC
        """)

    clusters = []
    for row in rows:
        if not row["centroid"]:
            continue
        try:
            arr = np.frombuffer(row["centroid"], dtype=np.float32).copy()
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr /= norm
            clusters.append({
                "cluster_id":   row["cluster_id"],
                "member_count": row["member_count"],
                "centroid":     arr,
                "rep_thumb":    row["rep_thumb"] or "",
            })
        except Exception:
            continue
    return clusters


# ---------------------------------------------------------------------------
# Step 4 — Compare
# ---------------------------------------------------------------------------

def match_contacts_to_clusters(
    contacts_with_embeddings: list[dict],
    clusters: list[dict],
    threshold: float,
) -> list[dict]:
    matches: list[dict] = []
    for contact in contacts_with_embeddings:
        emb = contact["embedding"]
        if emb is None:
            continue
        best_sim, best_cluster = -1.0, None
        for cluster in clusters:
            sim = float(np.dot(emb, cluster["centroid"]))
            if sim > best_sim:
                best_sim, best_cluster = sim, cluster
        if best_cluster is not None and best_sim >= threshold:
            matches.append({
                "contact_name":   contact["name"],
                "cluster_id":     best_cluster["cluster_id"],
                "cluster_size":   best_cluster["member_count"],
                "similarity_pct": round(best_sim * 100, 1),
                "auto_name":      best_sim >= 0.90,
                "rep_thumb":      best_cluster["rep_thumb"],
            })
    matches.sort(key=lambda r: r["similarity_pct"], reverse=True)
    return matches


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Match macOS Contacts photos against VIP unnamed face clusters (read-only)."
    )
    parser.add_argument(
        "--threshold", type=float, default=0.60,
        help="Min cosine similarity (0–1) to include in results. Default: 0.60",
    )
    parser.add_argument(
        "--out", type=str, default="contacts_matches.csv",
        help="Output CSV path. Default: contacts_matches.csv",
    )
    args = parser.parse_args()

    print()
    print("╔════════════════════════════════════════════════════╗")
    print("║   VIP — Contacts Face Match Diagnostic             ║")
    print("║   READ-ONLY — no data will be written to DB        ║")
    print("╚════════════════════════════════════════════════════╝")
    print()

    # 1. Export contacts
    contacts = export_contacts_with_photos()
    if not contacts:
        print("❌  No contacts with photos found. Check Contacts app permission.")
        sys.exit(1)

    # 2. Load detector
    print("\n🧠 Loading InsightFace model (this takes ~10 s on first run)…")
    try:
        detector = _load_detector()
    except Exception as exc:
        print(f"❌  Failed to load face detector: {exc}")
        print("    Make sure you are running with the VIP venv active.")
        sys.exit(1)

    # 3. Embed contact photos
    print(f"\n🔍 Detecting faces in {len(contacts)} contact photos…")
    embedded = []
    no_face = 0
    for i, c in enumerate(contacts, 1):
        emb = embed_contact(detector, c["image_bytes"])
        embedded.append({"name": c["name"], "embedding": emb})
        if emb is None:
            no_face += 1
        if i % 20 == 0 or i == len(contacts):
            print(f"   {i}/{len(contacts)} processed  ({no_face} with no detectable face)")

    with_face = [e for e in embedded if e["embedding"] is not None]
    print(f"\n   ✅  {len(with_face)}/{len(contacts)} contacts had a detectable face.")

    if not with_face:
        print("❌  No contact photos yielded a face embedding.")
        sys.exit(0)

    # 4. Load cluster centroids
    print(f"\n📂 Loading unnamed clusters from DB…")
    try:
        clusters = asyncio.run(_load_unnamed_clusters())
    except Exception as exc:
        print(f"❌  Failed to read VIP database: {exc}")
        sys.exit(1)
    print(f"   Found {len(clusters)} unnamed clusters.")
    if not clusters:
        print("   No unnamed clusters — nothing to match against.")
        sys.exit(0)

    # 5. Match
    print(f"\n🔗 Matching (threshold={args.threshold:.0%})…")
    matches = match_contacts_to_clusters(with_face, clusters, args.threshold)

    # 6. Print table
    print()
    if not matches:
        print(f"No matches above {args.threshold:.0%} threshold.")
        print("Try lowering --threshold (e.g. --threshold 0.50)")
        sys.exit(0)

    col_w = [28, 10, 12, 10, 12]
    headers = ["Contact Name", "Cluster", "Similarity", "Faces", "Auto-name?"]
    div = "  " + "─" * (sum(col_w) + 2 * len(col_w))
    print(div)
    print("  " + "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers)))
    print(div)
    for m in matches:
        auto_label = "✅ auto" if m["auto_name"] else "👀 review"
        row = [
            m["contact_name"][: col_w[0] - 1],
            str(m["cluster_id"]),
            f"{m['similarity_pct']}%",
            str(m["cluster_size"]),
            auto_label,
        ]
        print("  " + "  ".join(v.ljust(col_w[i]) for i, v in enumerate(row)))
    print(div)

    auto_count   = sum(1 for m in matches if m["auto_name"])
    review_count = len(matches) - auto_count
    print(f"\n  {len(matches)} matches above {args.threshold:.0%}:")
    print(f"    ✅  {auto_count}  at ≥90% → safe to auto-name immediately")
    print(f"    👀  {review_count}  between {args.threshold:.0%}–90% → surface as suggestions")
    print()

    # 7. Save CSV
    out_path = Path(args.out)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "contact_name", "cluster_id", "cluster_size",
            "similarity_pct", "auto_name", "rep_thumb",
        ])
        writer.writeheader()
        writer.writerows(matches)
    print(f"  💾  Saved to: {out_path.resolve()}")

    # Stats on contacts that had a face but no match
    matched_names = {m["contact_name"] for m in matches}
    no_match = [e["name"] for e in with_face if e["name"] not in matched_names]
    if no_match:
        print(f"\n  ℹ️   {len(no_match)} contacts had a face but no cluster match above threshold:")
        for name in no_match[:10]:
            print(f"       • {name}")
        if len(no_match) > 10:
            print(f"       … and {len(no_match) - 10} more (lower --threshold to see them)")
    print()


if __name__ == "__main__":
    main()
