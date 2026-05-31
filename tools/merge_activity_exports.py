#!/usr/bin/env python3
"""Merge per-year fb-activity-export-* directories into one import-ready export.

The year-by-year CDP driver writes one directory per year. This script
deduplicates posts (and optionally comments) by the same source_id logic
as ``extractors.harvest_post_identity`` / ``activity_log.extract``.

Usage:
    python3 tools/merge_activity_exports.py \\
        --downloads ~/Downloads \\
        --pattern 'fb-activity-export-v2.8.32-2026-05-23*' \\
        --output ~/Downloads/fb-activity-export-merged-2026-05-23

    # Only dirs touched after a timestamp (mtime):
    python3 tools/merge_activity_exports.py --since 2026-05-23
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.harvest_post_identity import source_id_for_harvest_post  # noqa: E402


def _pick_better(existing: dict | None, candidate: dict) -> dict:
    if existing is None:
        return candidate
    # Same rule as activity_log.extract: keep longer row text.
    if len(candidate.get("text") or "") > len(existing.get("text") or ""):
        return candidate
    return existing


def merge_posts(dirs: list[Path]) -> list[dict]:
    by_sid: dict[str, dict] = {}
    for d in dirs:
        pj = d / "posts.json"
        if not pj.exists():
            continue
        for raw in json.loads(pj.read_text()).get("postsWithText") or []:
            sid = source_id_for_harvest_post(raw)
            by_sid[sid] = _pick_better(by_sid.get(sid), raw)
    return list(by_sid.values())


def merge_comments(dirs: list[Path]) -> list[dict]:
    by_key: dict[str, dict] = {}
    for d in dirs:
        cj = d / "comments.json"
        if not cj.exists():
            continue
        for raw in json.loads(cj.read_text()).get("commentsWithText") or []:
            key = str(raw.get("commentId") or raw.get("url") or "")
            if not key:
                continue
            prev = by_key.get(key)
            if prev is None or len(raw.get("text") or "") > len(prev.get("text") or ""):
                by_key[key] = raw
    return list(by_key.values())


def copy_media(dirs: list[Path], out: Path) -> int:
    """Copy media into one dir under content-addressed (sha256) filenames.

    The extension names media per-dir-locally (base64 of a shared URL prefix +
    a per-dir sequential counter), so `aHR0cHM6Ly93d3cu_00000.jpg` is a DIFFERENT
    image in every export dir. Merging by bare filename collapses ~1100 files to
    ~40. We instead rename each file to ``sha256(bytes)[:24]+ext`` — globally
    unique AND content-addressed, so byte-identical captures of the same image
    (e.g. a busy month re-run) dedup for free.

    Each kept manifest entry's ``filename`` is rewritten to the sha name so the
    importer (which copies by ``entry['filename']`` and links to a post by
    ``entry['sourcePermalink']``) resolves the file. Dirs are iterated in the
    SAME sorted order as ``merge_posts``: the first dir's post row wins the
    source_id dedup AND its manifest entry wins here, so the winning row's
    postKey pfbid and the kept entry's sourcePermalink pfbid stay consistent
    (reshare pfbids rotate across harvests).
    """
    import hashlib

    media_out = out / "media"
    media_out.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    sha_to_name: dict[str, str] = {}
    n_files = 0
    for d in dirs:
        mm = d / "media_manifest.json"
        src_media = d / "media"
        if not mm.exists():
            continue
        for entry in json.loads(mm.read_text()):
            fn = entry.get("filename") or ""
            if not fn:
                continue
            src = src_media / fn
            if not src.exists():
                continue
            sha = hashlib.sha256(src.read_bytes()).hexdigest()
            new_name = sha_to_name.get(sha)
            if new_name is None:
                new_name = sha[:24] + Path(fn).suffix
                shutil.copy2(src, media_out / new_name)
                sha_to_name[sha] = new_name
                n_files += 1
            # Rewrite the entry to point at the content-addressed file. Same image
            # bytes referenced by multiple posts keep separate manifest entries
            # (distinct sourcePermalink) so per-post linkage survives.
            manifest.append({**entry, "filename": new_name})
    (out / "media_manifest.json").write_text(json.dumps(manifest, indent=2))
    return n_files


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    ap.add_argument("--pattern", default="fb-activity-export-*",
                    help="glob under downloads (default: all exports)")
    ap.add_argument("--output", type=Path, required=True,
                    help="output directory (created fresh)")
    ap.add_argument("--since", default=None,
                    help="only dirs with mtime on/after YYYY-MM-DD")
    ap.add_argument("--max-posts-per-dir", type=int, default=0,
                    help="skip dirs whose posts.json holds more than N posts. "
                         "Use to exclude whole-archive (year-level / skip-media map) "
                         "dirs from a per-month merge: their reshare pfbids may have "
                         "rotated vs the per-month media-on captures, and a longer "
                         "map row winning the dedup would orphan media linked by the "
                         "per-month manifest's sourcePermalink. 0 = no limit.")
    args = ap.parse_args()

    since_ts = 0.0
    if args.since:
        from datetime import datetime
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    dirs = sorted(args.downloads.glob(args.pattern))
    if since_ts:
        dirs = [d for d in dirs if d.is_dir() and d.stat().st_mtime >= since_ts]
    dirs = [d for d in dirs if d.is_dir() and (d / "posts.json").exists()]
    if args.max_posts_per_dir:
        kept = []
        for d in dirs:
            n = len(json.loads((d / "posts.json").read_text()).get("postsWithText") or [])
            if n > args.max_posts_per_dir:
                print(f"  skip {d.name}: {n} posts > --max-posts-per-dir "
                      f"{args.max_posts_per_dir} (whole-archive dir)")
            else:
                kept.append(d)
        dirs = kept
    if not dirs:
        sys.exit(f"no export dirs matching {args.pattern!r} under {args.downloads}")

    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)

    posts = merge_posts(dirs)
    comments = merge_comments(dirs)
    n_media = copy_media(dirs, args.output)

    (args.output / "posts.json").write_text(json.dumps({
        "collectedAt": "2026-05-23T00:00:00.000Z",
        "postsWithText": posts,
        "mergedFrom": [d.name for d in dirs],
    }, indent=2))
    (args.output / "comments.json").write_text(json.dumps({
        "collectedAt": "2026-05-23T00:00:00.000Z",
        "commentsWithText": comments,
    }, indent=2))
    if not (args.output / "media_manifest.json").exists():
        (args.output / "media_manifest.json").write_text("[]")

    print(f"Merged {len(dirs)} export dir(s) → {args.output}")
    print(f"  posts: {len(posts)}  comments: {len(comments)}  media files: {n_media}")


if __name__ == "__main__":
    main()
