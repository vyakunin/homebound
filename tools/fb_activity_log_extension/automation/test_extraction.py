#!/usr/bin/env python3
"""Integration test for the FB Activity Log extension.

Reads golden_set.yaml, drives the extension via drive_via_cdp.py for each
unique scope, then asserts the extracted posts.json / media_manifest.json
match the expected values.

Usage:
    uv run --with websockets --with pyyaml python3 \\
        tools/fb_activity_log_extension/automation/test_extraction.py

    # Just one entry by id:
    ... test_extraction.py --only nuff_said_reel_reshare

Exit code: 0 if all goldens pass, 1 if any fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum

import yaml


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
DOWNLOADS = pathlib.Path.home() / "Downloads"
DRIVER = HERE / "drive_via_cdp.py"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("fb_export_test")


class CheckResult(IntEnum):
    INVALID = 0
    PASS = 1
    FAIL = 2
    POST_NOT_FOUND = 3
    PENDING_HASH = 4   # entry has TBD media_sha256 — print computed for paste-in


@dataclass
class Scope:
    year: int
    month: int   # 1..12; required (test scope is month-precise)

    def __hash__(self) -> int:
        return hash((self.year, self.month))


@dataclass
class GoldenEntry:
    id: str
    scope: Scope
    post_key_suffix: str
    expect: dict


@dataclass
class CheckOutcome:
    entry_id: str
    result: CheckResult
    diffs: list[str]
    post_key: str | None
    media_count: int
    computed_hashes: list[str] = field(default_factory=list)
    pending_paste: str | None = None    # YAML snippet to paste back into golden_set


@dataclass
class MediaCheckResult:
    """Outcome of asserting expected media count + sha256 hashes."""
    diffs: list[str]
    computed_hashes: list[str]
    pending_paste: str | None    # YAML snippet for the user to paste back


@dataclass
class PostRecord:
    """Subset of posts.json[postsWithText][i] we actually assert against."""
    post_key: str
    text: str
    reshare_commentary: str | None   # None => not a reshare
    timestamp_raw_text: str

    @classmethod
    def from_raw(cls, r: dict) -> "PostRecord":
        ts = r.get("timestamp") or {}
        return cls(
            post_key=r.get("postKey") or r.get("url") or "",
            text=r.get("text") or "",
            reshare_commentary=r.get("reshareCommentary"),
            timestamp_raw_text=ts.get("rawText") or "",
        )

    @property
    def is_reshare(self) -> bool:
        return self.reshare_commentary is not None


@dataclass
class MediaItem:
    """Subset of media_manifest.json[i] we need."""
    source_permalink: str
    original_url: str
    filename: str

    @classmethod
    def from_raw(cls, r: dict) -> "MediaItem":
        return cls(
            source_permalink=r.get("sourcePermalink") or "",
            original_url=r.get("originalUrl") or "",
            filename=r.get("filename") or "",
        )


def load_golden(path: pathlib.Path) -> list[GoldenEntry]:
    raw = yaml.safe_load(path.read_text())
    entries = []
    for r in raw.get("posts") or []:
        s = r["scope"]
        entries.append(GoldenEntry(
            id=r["id"],
            scope=Scope(year=int(s["year"]), month=int(s["month"])),
            post_key_suffix=str(r.get("post_key_suffix") or "").strip(),
            expect=r.get("expect") or {},
        ))
    return entries


def run_driver_for_scope(scope: Scope, max_items: int, phase: str, with_media: bool) -> pathlib.Path:
    """Invoke drive_via_cdp.py for one (year, month) and return the export dir.

    Picks the newest fb-activity-export-* directory created during the run.
    Always runs with media on by default — that's what makes hash-based
    media assertions meaningful.
    """
    before = newest_export_dir()
    log.info("driving scope %d-%02d phase=%s max_items=%d media=%s",
             scope.year, scope.month, phase, max_items, "on" if with_media else "off")
    t0 = time.monotonic()
    cmd = [
        "uv", "run", "--with", "websockets", "--no-project", "python3",
        str(DRIVER),
        "--year", str(scope.year),
        "--month", str(scope.month),
        "--phase", phase,
        "--max-items", str(max_items),
    ]
    if with_media:
        cmd.append("--with-media")
    subprocess.run(cmd, check=True, cwd=ROOT, stdout=sys.stderr, stderr=sys.stderr)
    log.info("  scope %d-%02d done in %.1fs", scope.year, scope.month, time.monotonic() - t0)
    after = newest_export_dir()
    if not after or (before and after == before):
        raise RuntimeError("no new export dir appeared after driver run")
    return after


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hashes_for_media(export_dir: pathlib.Path, media: list[MediaItem]) -> list[str]:
    """Compute sha256 for each manifest entry's downloaded file (if present)."""
    out: list[str] = []
    media_root = export_dir / "media"
    for m in media:
        if not m.filename:
            out.append("<no-filename>")
            continue
        p = media_root / m.filename
        if not p.exists():
            out.append(f"<missing:{m.filename}>")
            continue
        out.append(sha256_of(p))
    return out


def newest_export_dir() -> pathlib.Path | None:
    dirs = sorted(
        DOWNLOADS.glob("fb-activity-export-*"),
        key=lambda p: p.stat().st_mtime,
    )
    return dirs[-1] if dirs else None


def find_post_record(posts: list[PostRecord], suffix: str) -> PostRecord | None:
    if not suffix or suffix == "TBD_FILL_IN_AFTER_FIRST_HARVEST":
        return None
    for p in posts:
        if suffix in p.post_key:
            return p
    return None


def media_for_post(manifest: list[MediaItem], post_key: str) -> list[MediaItem]:
    return [m for m in manifest if m.source_permalink == post_key]


def check_entry(entry: GoldenEntry, posts: list[PostRecord], manifest: list[MediaItem],
                export_dir: pathlib.Path) -> CheckOutcome:
    """Run every expectation on the extracted record. Returns a CheckOutcome."""
    p = find_post_record(posts, entry.post_key_suffix)
    if not p:
        return CheckOutcome(
            entry_id=entry.id,
            result=CheckResult.POST_NOT_FOUND,
            diffs=[f"post matching {entry.post_key_suffix!r} not in posts.json"],
            post_key=None,
            media_count=0,
        )
    media = media_for_post(manifest, p.post_key)
    return _check_expectations(entry, p, media, export_dir)


def _check_expectations(entry: GoldenEntry, p: PostRecord, media: list[MediaItem],
                        export_dir: pathlib.Path) -> CheckOutcome:
    """Apply every expect.* assertion to the harvested post + media."""
    diffs: list[str] = []
    e = entry.expect

    diffs += _check_reshare(e, p)
    diffs += _check_text(e, p)
    diffs += _check_timestamp(e, p)

    m = _check_media(e, media, export_dir)
    diffs += m.diffs

    if m.pending_paste:
        return CheckOutcome(
            entry_id=entry.id,
            result=CheckResult.PENDING_HASH,
            diffs=diffs,
            post_key=p.post_key,
            media_count=len(media),
            computed_hashes=m.computed_hashes,
            pending_paste=m.pending_paste,
        )
    return CheckOutcome(
        entry_id=entry.id,
        result=CheckResult.PASS if not diffs else CheckResult.FAIL,
        diffs=diffs,
        post_key=p.post_key,
        media_count=len(media),
        computed_hashes=m.computed_hashes,
    )


def _check_reshare(e: dict, p: PostRecord) -> list[str]:
    diffs: list[str] = []
    commentary = p.reshare_commentary or ""
    if "is_reshare" in e and bool(e["is_reshare"]) != p.is_reshare:
        diffs.append(f"is_reshare: expected {e['is_reshare']}, got {p.is_reshare}")
    if "reshare_commentary_exact" in e and commentary != e["reshare_commentary_exact"]:
        diffs.append(f"reshare_commentary_exact: expected {e['reshare_commentary_exact']!r}, got {commentary!r}")
    if "reshare_commentary_starts_with" in e and not commentary.startswith(e["reshare_commentary_starts_with"]):
        diffs.append(f"reshare_commentary should start with {e['reshare_commentary_starts_with']!r}, got {commentary!r}")
    for needle in e.get("reshare_commentary_contains", []) or []:
        if needle not in commentary:
            diffs.append(f"reshare_commentary should contain {needle!r}, got {commentary!r}")
    for needle in e.get("reshare_commentary_must_not_contain", []) or []:
        if needle in commentary:
            diffs.append(f"reshare_commentary must NOT contain {needle!r}, got {commentary!r}")
    if "reshared_from_url_contains" in e:
        # The extension doesn't currently expose reshared_from_url; once it
        # does, this assertion will find it on the PostRecord (extended).
        diffs.append(
            f"reshared_from_url should contain {e['reshared_from_url_contains']!r} "
            f"— extension does not yet populate this field"
        )
    if "commentary_or_reshare_commentary_starts_with" in e:
        s = e["commentary_or_reshare_commentary_starts_with"]
        if not (commentary.startswith(s) or p.text.startswith(s)):
            diffs.append(f"text or reshare_commentary should start with {s!r}, got text={p.text[:80]!r} commentary={commentary!r}")
    for needle in e.get("commentary_or_reshare_commentary_contains", []) or []:
        if needle not in commentary and needle not in p.text:
            diffs.append(f"text or reshare_commentary should contain {needle!r}, got text={p.text[:80]!r} commentary={commentary!r}")
    return diffs


def _check_text(e: dict, p: PostRecord) -> list[str]:
    diffs: list[str] = []
    if "text_starts_with" in e and not p.text.startswith(e["text_starts_with"]):
        diffs.append(f"text should start with {e['text_starts_with']!r}, got {p.text[:120]!r}")
    for needle in e.get("text_contains", []) or []:
        if needle not in p.text:
            diffs.append(f"text should contain {needle!r}, got {p.text[:200]!r}")
    return diffs


def _check_timestamp(e: dict, p: PostRecord) -> list[str]:
    diffs: list[str] = []
    if "timestamp_rawText" in e and p.timestamp_raw_text != e["timestamp_rawText"]:
        diffs.append(f"timestamp.rawText: expected {e['timestamp_rawText']!r}, got {p.timestamp_raw_text!r}")
    return diffs


def _check_media(e: dict, media: list[MediaItem], export_dir: pathlib.Path) -> MediaCheckResult:
    """Assert exact count + sha256 set.

    When the golden has TBD hashes (initial run), returns a result whose
    `pending_paste` is a YAML snippet the user can paste back after
    visually verifying the saved file is the correct content.
    """
    diffs: list[str] = []
    if "media_count_exact" in e and len(media) != e["media_count_exact"]:
        diffs.append(f"media count {len(media)} != expected {e['media_count_exact']}")

    computed = hashes_for_media(export_dir, media)
    expected = list(e.get("media_sha256") or [])
    has_tbd = any(h == "TBD" or h == "<tbd>" for h in expected)

    if not expected or has_tbd:
        paste = None
        if computed:
            paste = "media_sha256:\n" + "\n".join(f"  - {h}" for h in sorted(computed))
        return MediaCheckResult(diffs=diffs, computed_hashes=computed, pending_paste=paste)

    if sorted(computed) != sorted(expected):
        diffs.append(
            f"media sha256 mismatch:\n"
            f"        expected: {sorted(expected)}\n"
            f"        got:      {sorted(computed)}"
        )
    return MediaCheckResult(diffs=diffs, computed_hashes=computed, pending_paste=None)


def print_outcome(o: CheckOutcome) -> None:
    icon = {
        CheckResult.PASS: "OK",
        CheckResult.FAIL: "FAIL",
        CheckResult.POST_NOT_FOUND: "MISS",
        CheckResult.PENDING_HASH: "PENDING",
    }[o.result]
    print(f"[{icon}] {o.entry_id} (media={o.media_count})", file=sys.stderr)
    for d in o.diffs:
        print(f"      - {d}", file=sys.stderr)
    if o.pending_paste:
        print(f"      paste into golden_set.yaml under {o.entry_id}.expect:", file=sys.stderr)
        for line in o.pending_paste.split("\n"):
            print(f"        {line}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(HERE / "golden_set.yaml"))
    ap.add_argument("--only", default=None, help="run only the entry with this id")
    ap.add_argument("--max-items", type=int, default=50,
                    help="per-scope cap (default 50 — high enough to reach Apr/May targets within a month)")
    ap.add_argument("--reuse-latest", action="store_true",
                    help="skip the driver, assert against the newest export dir as-is")
    args = ap.parse_args()

    entries = load_golden(pathlib.Path(args.golden))
    if args.only:
        entries = [e for e in entries if e.id == args.only]
        if not entries:
            sys.exit(f"--only {args.only!r}: no matching entry in {args.golden}")

    # Drive once per unique scope.
    scope_to_dir: dict[Scope, pathlib.Path] = {}
    if args.reuse_latest:
        d = newest_export_dir()
        if not d:
            sys.exit("no fb-activity-export-* dir under ~/Downloads to reuse")
        for e in entries:
            scope_to_dir[e.scope] = d
        log.info("reusing newest export dir for all scopes: %s", d.name)
    else:
        for scope in {e.scope for e in entries}:
            scope_to_dir[scope] = run_driver_for_scope(scope, args.max_items, "posts", with_media=True)

    all_outcomes: list[CheckOutcome] = []
    for e in entries:
        d = scope_to_dir[e.scope]
        posts_raw = (json.loads((d / "posts.json").read_text()) or {}).get("postsWithText") or []
        posts = [PostRecord.from_raw(r) for r in posts_raw]
        mm_path = d / "media_manifest.json"
        mm_raw = json.loads(mm_path.read_text()) if mm_path.exists() else []
        manifest = [MediaItem.from_raw(r) for r in mm_raw]
        o = check_entry(e, posts, manifest, d)
        print_outcome(o)
        all_outcomes.append(o)

    counts = {r: sum(1 for o in all_outcomes if o.result == r) for r in CheckResult}
    print(
        f"\n{counts[CheckResult.PASS]} pass / {counts[CheckResult.FAIL]} fail / "
        f"{counts[CheckResult.POST_NOT_FOUND]} missing / "
        f"{counts[CheckResult.PENDING_HASH]} pending — total {len(all_outcomes)}",
        file=sys.stderr,
    )
    # PENDING is informational on first run (asks user to paste hashes back).
    # FAIL or MISS is a real failure.
    sys.exit(0 if (counts[CheckResult.FAIL] + counts[CheckResult.POST_NOT_FOUND]) == 0 else 1)


if __name__ == "__main__":
    main()
