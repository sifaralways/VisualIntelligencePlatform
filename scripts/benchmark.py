#!/usr/bin/env python3
"""
VIP Phase 0 Benchmark Script

Run this before any other phase to establish baseline performance on your M2 Max.
Results feed the performance targets in SOLUTION_DESIGN.md §9.

Usage:
    python scripts/benchmark.py --media-dir /path/to/raw/files --count 100
"""

import argparse
import asyncio
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config import settings, ensure_dirs
from backend.scanner.preview_extractor import extract_preview


async def benchmark_preview_extraction(folder: Path, count: int) -> None:
    """Measure: how many CR3 embedded JPEG previews can we extract per second?"""
    files = list(folder.glob("**/*.CR3"))[:count]
    if not files:
        files = list(folder.glob("**/*.cr3"))[:count]
    if not files:
        print("[SKIP] No CR3 files found in", folder)
        return

    ensure_dirs()
    print(f"\n{'─'*50}")
    print(f"Preview Extraction Benchmark  ({len(files)} files)")
    print(f"{'─'*50}")

    start = time.perf_counter()
    extracted = 0
    failed = 0

    for path in files:
        result = await extract_preview(path)
        if result:
            extracted += 1
        else:
            failed += 1

    elapsed = time.perf_counter() - start
    rate = extracted / elapsed if elapsed > 0 else 0

    print(f"  Extracted : {extracted}")
    print(f"  Failed    : {failed}")
    print(f"  Time      : {elapsed:.1f}s")
    print(f"  Rate      : {rate:.1f} files/sec")
    print(f"  100K ETA  : {100_000 / rate / 3600:.1f} hours" if rate > 0 else "  Rate: N/A")


def benchmark_face_embedding(preview_dir: Path, count: int) -> None:
    """Measure: how many faces can we embed per second?"""
    from backend.ml.face_detector import FaceDetector
    from backend.ml.embedder import FaceEmbedder

    previews = list(preview_dir.glob("*.jpg"))[:count]
    if not previews:
        print("[SKIP] No preview JPEGs found in", preview_dir)
        return

    print(f"\n{'─'*50}")
    print(f"Face Detection + Embedding Benchmark  ({len(previews)} previews)")
    print(f"{'─'*50}")

    detector = FaceDetector()
    embedder = FaceEmbedder()
    detector.load()
    embedder.load()

    total_faces = 0
    embedded = 0

    start = time.perf_counter()
    for preview_path in previews:
        faces = detector.detect(preview_path)
        total_faces += len(faces)
        for face in faces:
            vec = embedder.embed(face.crop)
            if vec is not None:
                embedded += 1

    elapsed = time.perf_counter() - start
    face_rate = total_faces / elapsed if elapsed > 0 else 0

    print(f"  Previews processed : {len(previews)}")
    print(f"  Faces detected     : {total_faces} ({total_faces/len(previews):.1f} avg/image)")
    print(f"  Embeddings created : {embedded}")
    print(f"  Time               : {elapsed:.1f}s")
    print(f"  Face embed rate    : {face_rate:.1f} faces/sec")
    print(f"  200K faces ETA     : {200_000 / face_rate / 3600:.1f} hours" if face_rate > 0 else "  Rate: N/A")


async def main() -> None:
    parser = argparse.ArgumentParser(description="VIP Phase 0 Benchmarks")
    parser.add_argument("--media-dir", type=Path, required=True, help="Folder with RAW files")
    parser.add_argument("--count", type=int, default=100, help="Number of files to benchmark")
    parser.add_argument("--skip-ml", action="store_true", help="Skip ML benchmark (extraction only)")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════╗")
    print("║   VIP — Phase 0 Benchmarks               ║")
    print("╚══════════════════════════════════════════╝\n")
    print(f"  Media directory : {args.media_dir}")
    print(f"  Sample size     : {args.count} files")

    await benchmark_preview_extraction(args.media_dir, args.count)

    if not args.skip_ml:
        benchmark_face_embedding(settings.preview_dir, args.count)

    print(f"\n{'─'*50}")
    print("Benchmark complete. Update §9 of SOLUTION_DESIGN.md with results.")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
