#!/usr/bin/env python3
"""Extract a merged Activity Log export and compare to prod DB (read-only).

Steps:
  1. Merge export dirs (or use --source single dir)
  2. ``activity_log.extract`` → posts.binpb locally
  3. SSH: load prod FB posts from Postgres
  4. Print counts + sampled diffs (new / dropped / changed text)

Does NOT modify prod. For a full import dry-run on the server after
uploading binpb, use ``manage.py import_posts --dry-run --update-existing``.

Usage:
    python3 tools/compare_fb_import_to_prod.py \\
        --merged ~/Downloads/fb-activity-export-merged-2026-05-23 \\
        --ssh homeserver

    python3 tools/compare_fb_import_to_prod.py \\
        --merge-pattern 'fb-activity-export-v2.8.32-2026-05-23*' \\
        --ssh homeserver --sample 25
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def load_prod_posts(ssh_host: str, ssh_key: str | None) -> dict[str, dict]:
    """source_id -> {content_text, source_url, created_at, reshared}"""
    sql = """
SELECT source_id,
       left(content_text, 400) AS content_preview,
       left(COALESCE(reshared_content_text, ''), 200) AS reshare_preview,
       source_url,
       created_at::date AS created
FROM blog_post
WHERE source = 3
ORDER BY source_id;
"""
    ssh_cmd = ["ssh", "-T"]
    if ssh_key:
        ssh_cmd += ["-i", ssh_key]
    ssh_cmd += [ssh_host, "docker exec -i pb_postgres psql -U blog -d personal_blog -t -A -F '|'"]
    proc = subprocess.run(ssh_cmd, input=sql, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    out: dict[str, dict] = {}
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue
        sid, content, reshare, url, created = parts
        out[sid] = {
            "content_preview": content,
            "reshare_preview": reshare,
            "source_url": url,
            "created": created,
        }
    return out


def load_incoming_binpb(binpb: Path) -> dict[str, dict]:
    """Read binpb via bazel runfiles (needs ``bazel build //extractors:activity_log_bin``)."""
    rf = ROOT / "bazel-bin/extractors/activity_log_bin.runfiles/_main"
    env = {**dict(os.environ), "PYTHONPATH": str(rf)}
    script = f"""
import json, sys
sys.path.insert(0, {str(rf)!r})
from extractors.posts_io import read_records
out = {{}}
for rec in read_records({str(binpb)!r}):
    sid = rec.source_id or ""
    if not sid: continue
    content = (rec.content_text or "")[:400]
    reshare = ""
    rf = getattr(rec, "reshared_from", None)
    if rf:
        reshare = (getattr(rf, "content_text", None) or "")[:200]
    out[sid] = {{"content_preview": content, "reshare_preview": reshare, "source_url": rec.source_url or ""}}
print(json.dumps(out))
"""
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def extract_export(source: Path, out_dir: Path) -> Path:
    """Run bazel activity_log_bin extract."""
    _run([
        "bazel", "run", "//extractors:activity_log_bin", "--",
        "--input", str(source),
        "--output-dir", str(out_dir),
    ], cwd=ROOT)
    return out_dir / "posts.binpb"


def _snippet(s: str, n: int = 120) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s[:n] + ("…" if len(s) > n else "")


def print_samples(title: str, ids: list[str], prod: dict, incoming: dict, n: int) -> None:
    print(f"\n=== {title} ({len(ids)} total, showing up to {n}) ===")
    for sid in ids[:n]:
        p = prod.get(sid, {})
        i = incoming.get(sid, {})
        print(f"  [{sid}]")
        if sid in prod and sid in incoming:
            if p.get("content_preview") != i.get("content_preview"):
                print(f"    prod:     {_snippet(p.get('content_preview', ''))}")
                print(f"    incoming: {_snippet(i.get('content_preview', ''))}")
            if p.get("reshare_preview") != i.get("reshare_preview"):
                print(f"    prod reshare:     {_snippet(p.get('reshare_preview', ''))}")
                print(f"    incoming reshare: {_snippet(i.get('reshare_preview', ''))}")
        elif sid in incoming:
            print(f"    NEW incoming: {_snippet(i.get('content_preview', ''))}")
            print(f"    url: {i.get('source_url', '')[:80]}")
        else:
            print(f"    DROP prod: {_snippet(p.get('content_preview', ''))}")
            print(f"    url: {p.get('source_url', '')[:80]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--merged", type=Path, default=None, help="merged export directory")
    ap.add_argument("--merge-pattern", default=None, help="merge from Downloads first")
    ap.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    ap.add_argument("--since", default="2026-05-23")
    ap.add_argument("--ssh", default="vyakunin@homeserver")
    ap.add_argument("--ssh-key", default=str(Path.home() / ".ssh/homeserver_ed25519"))
    ap.add_argument("--sample", type=int, default=20)
    args = ap.parse_args()

    source = args.merged
    if args.merge_pattern:
        out_merged = args.downloads / f"fb-activity-export-merged-{args.since}"
        _run([
            sys.executable, str(ROOT / "tools/merge_activity_exports.py"),
            "--downloads", str(args.downloads),
            "--pattern", args.merge_pattern,
            "--since", args.since,
            "--output", str(out_merged),
        ])
        source = out_merged
    if not source or not source.exists():
        sys.exit("need --merged or --merge-pattern")

    tmp = Path(tempfile.mkdtemp(prefix="fb_compare_"))
    print(f"Extracting {source.name} → {tmp} …")
    binpb = extract_export(source, tmp)
    print(f"  binpb: {binpb} ({binpb.stat().st_size} bytes)")

    print(f"Loading prod FB posts via {args.ssh} …")
    prod = load_prod_posts(args.ssh, args.ssh_key)
    incoming = load_incoming_binpb(binpb)

    only_prod = sorted(set(prod) - set(incoming))
    only_in = sorted(set(incoming) - set(prod))
    both = set(prod) & set(incoming)
    changed_content = [s for s in both if prod[s]["content_preview"] != incoming[s]["content_preview"]]
    changed_reshare = [
        s for s in both
        if prod[s]["reshare_preview"] != incoming[s]["reshare_preview"]
    ]

    print("\n--- Summary ---")
    print(f"  prod FB posts:     {len(prod)}")
    print(f"  incoming extract:  {len(incoming)}")
    print(f"  only on prod:      {len(only_prod)}  (lost if wipe+reimport)")
    print(f"  only in incoming:  {len(only_in)}  (new rows)")
    print(f"  same id, Δ content: {len(changed_content)}")
    print(f"  same id, Δ reshare: {len(changed_reshare)}")

    print_samples("NEW (incoming only)", only_in, prod, incoming, args.sample)
    print_samples("DROPPED (prod only)", only_prod, prod, incoming, args.sample)
    print_samples("CHANGED content", changed_content, prod, incoming, args.sample)
    print_samples("CHANGED reshared body", changed_reshare, prod, incoming, min(args.sample, 10))


if __name__ == "__main__":
    main()
