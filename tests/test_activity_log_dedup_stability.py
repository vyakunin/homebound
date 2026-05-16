"""Regression: Activity-Log source_id must survive pfbid session rotation.

Facebook regenerates the `pfbid…` permalink token on every new browsing
session, so the same post exported twice (e.g. running the Chrome extension
on two different days) previously produced two different source_ids and two
duplicate records in the blog feed.

_source_id_for_post now hashes timestamp + cleaned text for pfbid permalinks,
so the same post's source_id is stable across session-rotated pfbids. Numeric
post IDs (e.g. /posts/12345 or fbid=12345) still pass through as-is because
they are the underlying object id, not a permalink token.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from extractors.activity_log import _source_id_for_post, extract
from extractors.posts_io import read_records


def _make_zip(posts_list: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('posts.json', json.dumps({
            "collectedAt": "2026-04-18T00:00:00.000Z",
            "postsWithText": posts_list,
        }))
    return buf.getvalue()


def _extract(tmp_path: Path, posts_list: list[dict]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(_make_zip(posts_list))
    out_dir = tmp_path / "out"
    extract(zip_path, out_dir, dry_run=False)
    return list(read_records(out_dir / "posts.binpb"))


class TestSourceIdUnitLevel:
    """Direct unit tests on _source_id_for_post — no extract() pipeline."""

    def test_numeric_id_in_url_is_returned_as_is(self):
        record = {
            "url": "https://www.facebook.com/vyakunin/posts/14123456789",
            "timestamp": {"utime": 1700000000},
            "text": "updated his status.Hello.Public",
        }
        assert _source_id_for_post(record, content_text="Hello") == "14123456789"

    def test_fbid_query_param_is_returned_as_is(self):
        record = {
            "url": "https://www.facebook.com/photo/?fbid=98765432101234567&set=a.123",
            "timestamp": {"utime": 1700000000},
            "text": "added a new photo.Photo.Public",
        }
        assert _source_id_for_post(record, content_text="Photo") == "98765432101234567"

    def test_pfbid_url_rotates_but_content_stays_same(self):
        # Same post exported in two sessions → two different pfbids, same
        # timestamp.utime and same cleaned text. source_id must match.
        base = {
            "timestamp": {"utime": 1700000000},
            "text": "updated his status.Hello world.Public",
        }
        record_a = dict(base, url="https://www.facebook.com/vyakunin/posts/pfbid0SESSION_A")
        record_b = dict(base, url="https://www.facebook.com/vyakunin/posts/pfbid0SESSION_B_DIFFERENT")

        sid_a = _source_id_for_post(record_a, content_text="Hello world.")
        sid_b = _source_id_for_post(record_b, content_text="Hello world.")

        assert sid_a == sid_b, "pfbid rotation must not change source_id"
        assert sid_a.startswith("al_"), "pfbid path must produce a hashed source_id"

    def test_pfbid_different_content_gives_different_source_id(self):
        # Same pfbid-style URL, same timestamp, different text → different ids
        # (sanity check that the hash actually reflects the content).
        base_url = "https://www.facebook.com/vyakunin/posts/pfbid0SAMESESSION"
        ts = {"utime": 1700000000}
        r1 = {"url": base_url, "timestamp": ts, "text": "updated his status.Text one.Public"}
        r2 = {"url": base_url, "timestamp": ts, "text": "updated his status.Text two.Public"}

        sid1 = _source_id_for_post(r1, content_text="Text one.")
        sid2 = _source_id_for_post(r2, content_text="Text two.")
        assert sid1 != sid2

    def test_raw_ts_path_stable_without_utime(self):
        # When utime is missing but rawText is present, the seed must still be
        # stable across pfbid rotation.
        base = {
            "timestamp": {"utime": None, "iso": None, "rawText": "June 15, 2024 at 2:30 PM"},
            "text": "shared a post.Hello.Public",
        }
        r_a = dict(base, url="https://www.facebook.com/vyakunin/posts/pfbid0X")
        r_b = dict(base, url="https://www.facebook.com/vyakunin/posts/pfbid0Y")
        sid_a = _source_id_for_post(r_a, content_text="Hello.")
        sid_b = _source_id_for_post(r_b, content_text="Hello.")
        assert sid_a == sid_b
        assert sid_a.startswith("al_")


class TestPipelineDedupAcrossSessions:
    """End-to-end: run extract() on two synthetic ZIPs with rotated pfbids;
    the output source_ids must match, so a downstream (source, source_id)
    importer would hit the same row twice instead of creating duplicates.
    """

    def _post(self, pfbid: str, text: str, utime: int) -> dict:
        return {
            "postKey": f"https://www.facebook.com/vyakunin/posts/{pfbid}",
            "fbId": pfbid,
            "url": f"https://www.facebook.com/vyakunin/posts/{pfbid}",
            "timestamp": {"utime": utime, "iso": None, "rawText": None},
            "text": text,
        }

    def test_pfbid_rotation_produces_same_source_id(self, tmp_path):
        text = "updated his status.Content that does not change across sessions.Public1:00\u202fAMView"
        utime = 1700000000

        records_a = _extract(
            tmp_path / "session_a",
            [self._post("pfbid0SESSION_ONE", text, utime)],
        )
        records_b = _extract(
            tmp_path / "session_b",
            [self._post("pfbid0SESSION_TWO_DIFFERENT", text, utime)],
        )
        assert len(records_a) == 1 and len(records_b) == 1
        assert records_a[0].source_id == records_b[0].source_id
        assert records_a[0].source_id.startswith("al_")

    def test_numeric_id_path_still_matches_literal(self, tmp_path):
        # A record whose URL carries a numeric post id must still use that id
        # directly (unchanged behaviour) — this is the common modern case for
        # /posts/<digits> permalinks and fbid=<digits> photo URLs.
        records = _extract(
            tmp_path,
            [self._post("14777777777", "added a new photo.Numeric.Public", 1700000000)],
        )
        assert len(records) == 1
        assert records[0].source_id == "14777777777"

    def test_mixed_batch_dedup(self, tmp_path):
        """Two pfbid entries (same content/utime) in the same batch collapse
        to a single record via in-memory dedup."""
        text = "updated his status.Same content.Public2:00\u202fAMView"
        utime = 1700000500
        records = _extract(
            tmp_path,
            [
                self._post("pfbid0DUP_A", text, utime),
                self._post("pfbid0DUP_B", text, utime),
            ],
        )
        assert len(records) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
