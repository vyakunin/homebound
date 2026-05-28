#!/usr/bin/env python3
"""Integration test for the FB Activity Log extension.

Reads golden_set.yaml, drives the extension via drive_via_cdp.py for each
unique scope, then asserts the extracted posts.json / media_manifest.json
match the expected values.

Each golden entry is located by ``source_id`` (same key as import dedup in
``extractors.harvest_post_identity``). ``post_key_suffix`` is a permalink
anchor for re-fetch / ``--refresh-source-ids`` only.

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

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractors.harvest_post_identity import source_id_for_harvest_post  # noqa: E402

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
    PENDING_HASH = 4         # entry has TBD media_sha256 — print computed for paste-in
    MONTH_SET_DRIFT = 5      # harvested source_id set differs from month_pin


@dataclass
class Scope:
    year: int
    month: int   # 1..12; required (test scope is month-precise)

    def __hash__(self) -> int:
        return hash((self.year, self.month))

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


@dataclass
class PinnedSourceId:
    """One source_id in a month_pin, optionally annotated with a human label."""
    source_id: str
    label: str   # informational only; not enforced


@dataclass
class MonthPin:
    """Exact set of post source_ids the harvest must produce for this scope.

    Authoritative — no count_floor proxies, no regex heuristics. If the
    extension produces a different set (extras or misses), the test
    fails with the precise diff so the user can decide:
      a) extension regression → fix code, re-harvest, re-pass.
      b) FB ground truth changed (new/deleted post) → run
         --refresh-month-pins to re-pin.
    """
    scope: Scope
    expected: list[PinnedSourceId]

    @property
    def expected_set(self) -> set[str]:
        return {p.source_id for p in self.expected}


@dataclass
class GoldenEntry:
    id: str
    scope: Scope
    source_id: str
    post_key_suffix: str   # permalink anchor for re-fetch / refreshing ground truth
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
    reshared_from_url: str           # '' when extension didn't populate it
    timestamp_raw_text: str

    @classmethod
    def from_raw(cls, r: dict) -> "PostRecord":
        ts = r.get("timestamp") or {}
        return cls(
            post_key=r.get("postKey") or r.get("url") or "",
            text=r.get("text") or "",
            reshare_commentary=r.get("reshareCommentary"),
            reshared_from_url=r.get("reshared_from_url") or r.get("reshareUrl") or "",
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


@dataclass
class ScopeInvariant:
    id: str
    rule: str   # one of: media_hashes_not_all_identical
    min_posts_with_media: int


@dataclass
class GoldenFile:
    entries: list[GoldenEntry]
    scope_invariants: list[ScopeInvariant]
    month_pins: list[MonthPin]


def _parse_pinned_source_ids(raw_list: list) -> list[PinnedSourceId]:
    """Accept either bare-string entries (`al_abc`) or {source_id, label} dicts."""
    out: list[PinnedSourceId] = []
    for item in raw_list or []:
        if isinstance(item, str):
            out.append(PinnedSourceId(source_id=item.strip(), label=""))
        elif isinstance(item, dict):
            out.append(PinnedSourceId(
                source_id=str(item.get("source_id") or "").strip(),
                label=str(item.get("label") or "").strip(),
            ))
    return [p for p in out if p.source_id]


def load_golden(path: pathlib.Path) -> GoldenFile:
    raw = yaml.safe_load(path.read_text())
    entries: list[GoldenEntry] = []
    for r in raw.get("posts") or []:
        s = r["scope"]
        entries.append(GoldenEntry(
            id=r["id"],
            scope=Scope(year=int(s["year"]), month=int(s["month"])),
            source_id=str(r.get("source_id") or "").strip(),
            post_key_suffix=str(r.get("post_key_suffix") or "").strip(),
            expect=r.get("expect") or {},
        ))
    invariants: list[ScopeInvariant] = []
    for r in raw.get("scope_invariants") or []:
        invariants.append(ScopeInvariant(
            id=r["id"],
            rule=str(r["rule"]),
            min_posts_with_media=int(r.get("min_posts_with_media") or 0),
        ))
    month_pins: list[MonthPin] = []
    for r in raw.get("month_pins") or []:
        s = r["scope"]
        month_pins.append(MonthPin(
            scope=Scope(year=int(s["year"]), month=int(s["month"])),
            expected=_parse_pinned_source_ids(r.get("expected_source_ids") or []),
        ))
    return GoldenFile(entries=entries, scope_invariants=invariants, month_pins=month_pins)


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


def _row_matches_expect(raw: dict, expect: dict) -> bool:
    """True when this harvest row satisfies disambiguating expect fields."""
    if not expect:
        return True
    ts = (raw.get("timestamp") or {}).get("rawText") or ""
    if expect.get("timestamp_rawText") and ts != expect["timestamp_rawText"]:
        return False
    rc = raw.get("reshareCommentary")
    if "reshare_commentary_exact" in expect:
        got = rc if rc is not None else ""
        if got != expect["reshare_commentary_exact"]:
            return False
    if "reshare_commentary_starts_with" in expect:
        got = rc or ""
        if not got.startswith(expect["reshare_commentary_starts_with"]):
            return False
    return True


def find_post_raw(
    posts_raw: list[dict],
    source_id: str,
    expect: dict | None = None,
    permalink_suffix: str = "",
) -> dict | None:
    """Locate harvest row(s) by import dedup source_id.

    When several rows share a source_id (same cleaned ``text``, different
    ``reshareCommentary``), pick the one matching ``expect`` if provided.
    """
    if not source_id or source_id == "TBD_FILL_IN_AFTER_FIRST_HARVEST":
        return None
    matches = [raw for raw in posts_raw if source_id_for_harvest_post(raw) == source_id]
    if not matches:
        return None
    if expect:
        for raw in matches:
            if _row_matches_expect(raw, expect):
                return raw
    if permalink_suffix:
        for raw in matches:
            pk = raw.get("postKey") or raw.get("url") or ""
            if permalink_suffix in pk:
                return raw
    return matches[0]


def find_post_by_permalink(posts_raw: list[dict], suffix: str) -> dict | None:
    """Fallback for refresh tooling: locate row by permalink substring."""
    if not suffix or suffix == "TBD_FILL_IN_AFTER_FIRST_HARVEST":
        return None
    for raw in posts_raw:
        pk = raw.get("postKey") or raw.get("url") or ""
        if suffix in pk:
            return raw
    return None


def find_post_record(
    posts_raw: list[dict],
    source_id: str,
    expect: dict | None = None,
    permalink_suffix: str = "",
) -> PostRecord | None:
    raw = find_post_raw(
        posts_raw, source_id, expect=expect, permalink_suffix=permalink_suffix,
    )
    return PostRecord.from_raw(raw) if raw else None


def media_for_post(manifest: list[MediaItem], post_key: str) -> list[MediaItem]:
    return [m for m in manifest if m.source_permalink == post_key]


def check_entry(entry: GoldenEntry, posts_raw: list[dict], manifest: list[MediaItem],
                export_dir: pathlib.Path) -> CheckOutcome:
    """Run every expectation on the extracted record. Returns a CheckOutcome."""
    must_be_absent = bool(entry.expect.get("must_be_absent"))
    if must_be_absent:
        # Negative-match entry: still keyed by permalink fragment (no stable source_id).
        for raw in posts_raw:
            pk = raw.get("postKey") or raw.get("url") or ""
            if entry.post_key_suffix in pk:
                return CheckOutcome(
                    entry_id=entry.id, result=CheckResult.FAIL,
                    diffs=[f"phantom row present: postKey={pk!r} matches forbidden suffix {entry.post_key_suffix!r}"],
                    post_key=pk, media_count=0,
                )
        return CheckOutcome(
            entry_id=entry.id, result=CheckResult.PASS, diffs=[],
            post_key=None, media_count=0,
        )

    p = find_post_record(
        posts_raw, entry.source_id,
        expect=entry.expect, permalink_suffix=entry.post_key_suffix,
    )
    if not p:
        return CheckOutcome(
            entry_id=entry.id,
            result=CheckResult.POST_NOT_FOUND,
            diffs=[f"post with source_id={entry.source_id!r} not in posts.json "
                   f"(permalink anchor {entry.post_key_suffix!r} is for re-fetch only)"],
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
        if e["reshared_from_url_contains"] not in p.reshared_from_url:
            diffs.append(
                f"reshared_from_url should contain {e['reshared_from_url_contains']!r}, "
                f"got {p.reshared_from_url!r}"
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
    for forbidden in e.get("text_must_not_contain", []) or []:
        if forbidden in p.text:
            diffs.append(f"text must NOT contain {forbidden!r}, got {p.text[:200]!r}")
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


def refresh_source_ids(golden_path: pathlib.Path, export_dir: pathlib.Path) -> None:
    """Print source_id + permalink updates by matching golden permalink anchors."""
    golden = load_golden(golden_path)
    posts_raw = (json.loads((export_dir / "posts.json").read_text()) or {}).get("postsWithText") or []
    print(f"# From export {export_dir.name}", file=sys.stderr)
    for entry in golden.entries:
        if entry.expect.get("must_be_absent"):
            continue
        raw = find_post_by_permalink(posts_raw, entry.post_key_suffix)
        if not raw:
            print(f"# MISS {entry.id}: permalink {entry.post_key_suffix!r} not in export", file=sys.stderr)
            continue
        pk = raw.get("postKey") or raw.get("url") or ""
        sid = source_id_for_harvest_post(raw)
        pfbid = pk.rsplit("/", 1)[-1] if "/posts/" in pk else pk
        print(f"  # {entry.id}")
        print(f"  source_id: {sid}")
        print(f"  post_key_suffix: {pfbid}")


def refresh_month_pin(scope: Scope, export_dir: pathlib.Path,
                      existing_labels: dict[str, str] | None = None) -> None:
    """Print a month_pins[].expected_source_ids YAML block for the given scope.

    Re-uses labels from the prior pin (mapped by source_id) so a re-pin
    after a parser change preserves the per-id "# label" comments.
    """
    existing_labels = existing_labels or {}
    posts_raw = (json.loads((export_dir / "posts.json").read_text()) or {}).get("postsWithText") or []
    sids = [(source_id_for_harvest_post(r), r) for r in posts_raw]
    sids.sort(key=lambda t: ((t[1].get("timestamp") or {}).get("utime") or 0, t[0]))
    print(f"# From export {export_dir.name}  (scope {scope.label}, {len(sids)} posts)", file=sys.stderr)
    print(f"  - scope: {{year: {scope.year}, month: {scope.month}}}")
    print(f"    expected_source_ids:")
    for sid, raw in sids:
        ts = (raw.get("timestamp") or {}).get("rawText") or ""
        pk = raw.get("postKey") or raw.get("url") or ""
        tail = pk.rsplit("/", 1)[-1][:24] if pk else ""
        lbl = existing_labels.get(sid, "")
        annot_parts = [p for p in (lbl, ts, tail) if p]
        annot = "  # " + " | ".join(annot_parts) if annot_parts else ""
        print(f"      - {sid}{annot}")


def _existing_month_pin_labels(golden_path: pathlib.Path, scope: Scope) -> dict[str, str]:
    g = load_golden(golden_path)
    for pin in g.month_pins:
        if pin.scope == scope:
            return {p.source_id: p.label for p in pin.expected if p.label}
    return {}


def print_outcome(o: CheckOutcome) -> None:
    icon = {
        CheckResult.PASS: "OK",
        CheckResult.FAIL: "FAIL",
        CheckResult.POST_NOT_FOUND: "MISS",
        CheckResult.PENDING_HASH: "PENDING",
        CheckResult.MONTH_SET_DRIFT: "DRIFT",
    }[o.result]
    print(f"[{icon}] {o.entry_id} (media={o.media_count})", file=sys.stderr)
    for d in o.diffs:
        print(f"      - {d}", file=sys.stderr)
    if o.pending_paste:
        print(f"      paste into golden_set.yaml under {o.entry_id}.expect:", file=sys.stderr)
        for line in o.pending_paste.split("\n"):
            print(f"        {line}", file=sys.stderr)


def harvested_source_ids(posts_raw: list[dict]) -> list[tuple[str, dict]]:
    """List (source_id, raw_row) for every harvested post in the scope."""
    return [(source_id_for_harvest_post(r), r) for r in posts_raw]


def check_no_duplicate_source_ids(scope: Scope, posts_raw: list[dict]) -> CheckOutcome:
    """Within a single (year, month) harvest, every harvest row must have a
    DISTINCT source_id. Two rows with the same source_id means the harvester
    captured the same logical post twice (phantom row, double activity-log
    anchor, etc.) — caught here so the precise-set pin can stay set-valued
    while still detecting double-capture.
    """
    counts: dict[str, list[dict]] = {}
    for r in posts_raw:
        sid = source_id_for_harvest_post(r)
        counts.setdefault(sid, []).append(r)
    dups = {sid: rows for sid, rows in counts.items() if len(rows) > 1}
    entry_id = f"no_duplicate_source_ids@{scope.label}"
    if not dups:
        return CheckOutcome(
            entry_id=entry_id, result=CheckResult.PASS, diffs=[],
            post_key=None, media_count=len(counts),
        )
    diffs: list[str] = [f"{len(dups)} source_id(s) appear in >1 harvest row:"]
    for sid, rows in sorted(dups.items()):
        diffs.append(f"  - {sid} ({len(rows)}x):")
        for r in rows:
            pk = r.get("postKey") or r.get("url") or ""
            ts = (r.get("timestamp") or {}).get("rawText") or ""
            diffs.append(f"      postKey={pk[:80]}  ts={ts!r}")
    return CheckOutcome(
        entry_id=entry_id, result=CheckResult.FAIL, diffs=diffs,
        post_key=None, media_count=len(counts),
    )


def check_month_pin(pin: MonthPin, posts_raw: list[dict]) -> CheckOutcome:
    """Assert the harvested source_id set EQUALS the pinned set — no proxies.

    On drift, report both directions:
      - missing: pinned source_id absent from harvest (regression — parser
        changed identity, or the post disappeared from the activity log,
        or the harvest cap hit before reaching it).
      - extra: harvested source_id not in pin (new post on FB → re-pin
        intentionally, or phantom row → fix extension).
    """
    sids = harvested_source_ids(posts_raw)
    harvested_set = {sid for sid, _ in sids}
    expected_set = pin.expected_set

    missing = sorted(expected_set - harvested_set)
    extra = sorted(harvested_set - expected_set)

    entry_id = f"month_pin@{pin.scope.label}"
    if not missing and not extra:
        return CheckOutcome(
            entry_id=entry_id, result=CheckResult.PASS, diffs=[],
            post_key=None, media_count=len(harvested_set),
        )

    label_by_sid = {p.source_id: p.label for p in pin.expected}
    raw_by_sid = {sid: raw for sid, raw in sids}
    diffs: list[str] = []
    if missing:
        diffs.append(f"missing {len(missing)} pinned source_id(s):")
        for sid in missing:
            lbl = label_by_sid.get(sid, "")
            diffs.append(f"    - {sid}{(' # ' + lbl) if lbl else ''}")
    if extra:
        diffs.append(f"unexpected {len(extra)} extra source_id(s) in harvest:")
        for sid in extra:
            raw = raw_by_sid.get(sid) or {}
            pk = raw.get("postKey") or raw.get("url") or ""
            ts = (raw.get("timestamp") or {}).get("rawText") or ""
            diffs.append(f"    + {sid}  ts={ts!r}  postKey={pk[:80]}")

    return CheckOutcome(
        entry_id=entry_id,
        result=CheckResult.MONTH_SET_DRIFT,
        diffs=diffs,
        post_key=None,
        media_count=len(harvested_set),
    )


def check_scope_invariant(invariant: ScopeInvariant, scope: Scope, export_dir: pathlib.Path) -> CheckOutcome:
    """Assert a scope-wide invariant. Currently supports:
       - media_hashes_not_all_identical: at least one post in the scope must
         have a media hash different from the others.
    """
    posts_raw = (json.loads((export_dir / "posts.json").read_text()) or {}).get("postsWithText") or []
    manifest_raw = json.loads((export_dir / "media_manifest.json").read_text()) if (export_dir / "media_manifest.json").exists() else []
    manifest = [MediaItem.from_raw(r) for r in manifest_raw]

    if invariant.rule == "media_hashes_not_all_identical":
        per_post_hashes: dict[str, list[str]] = {}
        for m in manifest:
            per_post_hashes.setdefault(m.source_permalink, []).append(sha256_of(export_dir / "media" / m.filename) if (export_dir / "media" / m.filename).exists() else "<missing>")
        posts_with_media = [k for k, v in per_post_hashes.items() if any(h != "<missing>" for h in v)]
        if len(posts_with_media) < invariant.min_posts_with_media:
            return CheckOutcome(
                entry_id=f"{invariant.id}@{scope.year}-{scope.month:02d}",
                result=CheckResult.PASS,
                diffs=[],
                post_key=None,
                media_count=0,
            )
        first_hash_per_post = [sorted(per_post_hashes[k])[0] for k in posts_with_media]
        if len(set(first_hash_per_post)) == 1:
            sample = first_hash_per_post[0]
            return CheckOutcome(
                entry_id=f"{invariant.id}@{scope.year}-{scope.month:02d}",
                result=CheckResult.FAIL,
                diffs=[
                    f"{len(posts_with_media)} posts all share the same media hash {sample[:16]}…",
                    "Symptom: FB SPA/service-worker is bleeding state across sequential tab extractions.",
                ],
                post_key=None,
                media_count=len(posts_with_media),
            )
        return CheckOutcome(
            entry_id=f"{invariant.id}@{scope.year}-{scope.month:02d}",
            result=CheckResult.PASS,
            diffs=[],
            post_key=None,
            media_count=len(posts_with_media),
        )
    return CheckOutcome(
        entry_id=invariant.id,
        result=CheckResult.FAIL,
        diffs=[f"unknown rule {invariant.rule!r}"],
        post_key=None,
        media_count=0,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", default=str(HERE / "golden_set.yaml"))
    ap.add_argument("--only", default=None, help="run only the entry with this id")
    ap.add_argument("--max-items", type=int, default=50,
                    help="per-scope cap (default 50 — high enough to reach Apr/May targets within a month)")
    ap.add_argument("--reuse-latest", action="store_true",
                    help="skip the driver, assert against the newest export dir as-is")
    ap.add_argument("--refresh-source-ids", action="store_true",
                    help="print source_id/post_key_suffix YAML from newest export (requires --reuse-latest)")
    ap.add_argument("--refresh-month-pin", default=None, metavar="YYYY-MM",
                    help="print month_pins YAML block for the given scope from newest export "
                         "(requires --reuse-latest); paste under `month_pins:` in golden_set.yaml")
    args = ap.parse_args()

    if args.refresh_source_ids and not args.reuse_latest:
        sys.exit("--refresh-source-ids requires --reuse-latest")
    if args.refresh_month_pin and not args.reuse_latest:
        sys.exit("--refresh-month-pin requires --reuse-latest")
    if args.refresh_source_ids:
        d = newest_export_dir()
        if not d:
            sys.exit("no fb-activity-export-* dir under ~/Downloads")
        refresh_source_ids(pathlib.Path(args.golden), d)
        return
    if args.refresh_month_pin:
        try:
            yr, mo = args.refresh_month_pin.split("-")
            scope = Scope(year=int(yr), month=int(mo))
        except (ValueError, AttributeError):
            sys.exit(f"--refresh-month-pin: expected YYYY-MM, got {args.refresh_month_pin!r}")
        d = newest_export_dir()
        if not d:
            sys.exit("no fb-activity-export-* dir under ~/Downloads")
        labels = _existing_month_pin_labels(pathlib.Path(args.golden), scope)
        refresh_month_pin(scope, d, existing_labels=labels)
        return

    golden = load_golden(pathlib.Path(args.golden))
    entries = golden.entries
    if args.only:
        entries = [e for e in entries if e.id == args.only]
        if not entries:
            sys.exit(f"--only {args.only!r}: no matching entry in {args.golden}")

    # Drive once per unique scope — union of per-post entry scopes AND month_pin scopes.
    all_scopes: set[Scope] = {e.scope for e in entries} | {p.scope for p in golden.month_pins}
    scope_to_dir: dict[Scope, pathlib.Path] = {}
    if args.reuse_latest:
        d = newest_export_dir()
        if not d:
            sys.exit("no fb-activity-export-* dir under ~/Downloads to reuse")
        for s in all_scopes:
            scope_to_dir[s] = d
        log.info("reusing newest export dir for all scopes: %s", d.name)
    else:
        for scope in all_scopes:
            # max_items=0 (uncapped) for scopes that have a month_pin: exact-set
            # assertion is meaningless under a cap.
            pinned = any(p.scope == scope for p in golden.month_pins)
            cap = 0 if pinned else args.max_items
            scope_to_dir[scope] = run_driver_for_scope(scope, cap, "posts", with_media=True)

    all_outcomes: list[CheckOutcome] = []
    for e in entries:
        d = scope_to_dir[e.scope]
        posts_raw = (json.loads((d / "posts.json").read_text()) or {}).get("postsWithText") or []
        mm_path = d / "media_manifest.json"
        mm_raw = json.loads(mm_path.read_text()) if mm_path.exists() else []
        manifest = [MediaItem.from_raw(r) for r in mm_raw]
        o = check_entry(e, posts_raw, manifest, d)
        print_outcome(o)
        all_outcomes.append(o)

    # Month-level exact-set pins.
    for pin in golden.month_pins:
        d = scope_to_dir.get(pin.scope)
        if not d:
            continue
        posts_raw = (json.loads((d / "posts.json").read_text()) or {}).get("postsWithText") or []
        o = check_month_pin(pin, posts_raw)
        print_outcome(o)
        all_outcomes.append(o)
        # Same scope: "no duplicate source_ids" implicit invariant.
        o = check_no_duplicate_source_ids(pin.scope, posts_raw)
        print_outcome(o)
        all_outcomes.append(o)

    # Scope-wide invariants — applied once per (scope, invariant) pair.
    for scope, export_dir in scope_to_dir.items():
        for inv in golden.scope_invariants:
            o = check_scope_invariant(inv, scope, export_dir)
            print_outcome(o)
            all_outcomes.append(o)

    counts = {r: sum(1 for o in all_outcomes if o.result == r) for r in CheckResult}
    print(
        f"\n{counts[CheckResult.PASS]} pass / {counts[CheckResult.FAIL]} fail / "
        f"{counts[CheckResult.POST_NOT_FOUND]} missing / "
        f"{counts[CheckResult.PENDING_HASH]} pending / "
        f"{counts[CheckResult.MONTH_SET_DRIFT]} month-drift — total {len(all_outcomes)}",
        file=sys.stderr,
    )
    # PENDING is informational on first run (asks user to paste hashes back).
    # FAIL, MISS, or MONTH_SET_DRIFT is a real failure.
    bad = (counts[CheckResult.FAIL] + counts[CheckResult.POST_NOT_FOUND]
           + counts[CheckResult.MONTH_SET_DRIFT])
    sys.exit(0 if bad == 0 else 1)


if __name__ == "__main__":
    main()
