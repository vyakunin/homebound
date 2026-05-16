"""Tests for the Twitter/X timeline extractor (no Django dependency)."""
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from extractors.twitter_log import (
    _extract_tags,
    _extract_tweet_id,
    _parse_timestamp,
    _source_id_for_tweet,
    extract,
)
from extractors.posts_io import read_records
from proto.post_record import Source, Visibility


# ---------------------------------------------------------------------------
# Helper: build a minimal ZIP from dicts
# ---------------------------------------------------------------------------

def make_zip(
    posts: dict | None = None,
    comments: dict | None = None,
    media_manifest: list | None = None,
    reaction_counts: dict | None = None,
    link_attachments: dict | None = None,
    profile_links: dict | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        if posts is not None:
            zf.writestr('posts.json', json.dumps(posts))
        if comments is not None:
            zf.writestr('comments.json', json.dumps(comments))
        if media_manifest is not None:
            zf.writestr('media_manifest.json', json.dumps(media_manifest))
        if reaction_counts is not None:
            zf.writestr('reaction_counts.json', json.dumps(reaction_counts))
        if link_attachments is not None:
            zf.writestr('link_attachments.json', json.dumps(link_attachments))
        if profile_links is not None:
            zf.writestr('profile_links.json', json.dumps(profile_links))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. _extract_tweet_id
# ---------------------------------------------------------------------------

class TestExtractTweetId:
    def test_standard_url(self):
        assert _extract_tweet_id("https://x.com/user/status/123456789") == "123456789"

    def test_twitter_url(self):
        assert _extract_tweet_id("https://twitter.com/user/status/987654321") == "987654321"

    def test_with_query_params(self):
        assert _extract_tweet_id("https://x.com/user/status/111?s=20") == "111"

    def test_no_status(self):
        assert _extract_tweet_id("https://x.com/user") is None

    def test_none(self):
        assert _extract_tweet_id(None) is None

    def test_empty(self):
        assert _extract_tweet_id("") is None


# ---------------------------------------------------------------------------
# 2. _source_id_for_tweet
# ---------------------------------------------------------------------------

class TestSourceIdForTweet:
    def test_tweet_id_field(self):
        record = {"tweetId": "12345", "url": "https://x.com/user/status/12345"}
        assert _source_id_for_tweet(record) == "12345"

    def test_fb_id_compatibility(self):
        record = {"fbId": "67890", "url": "https://x.com/user/status/67890"}
        assert _source_id_for_tweet(record) == "67890"

    def test_fallback_to_url(self):
        record = {"url": "https://x.com/user/status/99999"}
        assert _source_id_for_tweet(record) == "99999"

    def test_hash_fallback(self):
        record = {"url": "https://x.com/some/weird/path"}
        sid = _source_id_for_tweet(record)
        assert sid.startswith("tw_")
        assert len(sid) == 19  # "tw_" + 16 hex chars


# ---------------------------------------------------------------------------
# 3. _parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_iso_format(self):
        ts = {"iso": "2024-01-15T10:30:00.000Z", "utime": None, "rawText": None}
        epoch = _parse_timestamp(ts, None)
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.minute == 30

    def test_iso_with_timezone(self):
        ts = {"iso": "2024-06-01T14:00:00+05:00", "utime": None, "rawText": None}
        epoch = _parse_timestamp(ts, None)
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        assert dt.hour == 9  # 14:00 +5:00 = 09:00 UTC

    def test_none_timestamp(self):
        assert _parse_timestamp(None, None) is None

    def test_empty_timestamp(self):
        assert _parse_timestamp({}, None) is None


# ---------------------------------------------------------------------------
# 4. _extract_tags
# ---------------------------------------------------------------------------

class TestExtractTags:
    def test_single_hashtag(self):
        assert _extract_tags("Hello #world") == ["world"]

    def test_multiple_hashtags(self):
        assert _extract_tags("#foo bar #baz") == ["foo", "baz"]

    def test_no_hashtags(self):
        assert _extract_tags("no hashtags here") == []

    def test_cyrillic_hashtags(self):
        assert _extract_tags("#Привет мир") == ["привет"]


# ---------------------------------------------------------------------------
# 5. Full extraction (ZIP -> records)
# ---------------------------------------------------------------------------

class TestExtract:
    def test_basic_tweet(self, tmp_path):
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "postKey": "https://x.com/testuser/status/111111",
                    "tweetId": "111111",
                    "fbId": "111111",
                    "url": "https://x.com/testuser/status/111111",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Hello from #Twitter!",
                    "author": "Test User",
                    "authorHandle": "testuser",
                    "likeCount": 5,
                    "retweetCount": 2,
                    "replyCount": 1,
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 1
        assert result["skipped_retweet"] == 0

        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        assert len(records) == 1

        r = records[0]
        assert r.source == Source.SOURCE_TWITTER
        assert r.source_id == "111111"
        assert r.content_text == "Hello from #Twitter!"
        assert r.visibility == Visibility.VISIBILITY_PUBLIC
        assert "twitter" in r.tags
        assert r.extra.get("tw_like_count") == "5"
        assert r.extra.get("tw_retweet_count") == "2"

    def test_retweet_skipped(self, tmp_path):
        """Pure retweets (no quote) should be skipped."""
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "postKey": "https://x.com/otheruser/status/222222",
                    "tweetId": "222222",
                    "fbId": "222222",
                    "url": "https://x.com/otheruser/status/222222",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Original tweet text",
                    "resharedFrom": {
                        "author": "Other User",
                        "authorHandle": "otheruser",
                        "url": "https://x.com/otheruser/status/222222",
                    },
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 0
        assert result["skipped_retweet"] == 1

    def test_quote_tweet(self, tmp_path):
        """Quote tweets should be imported with reshared_from pointing to the quoted tweet."""
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "postKey": "https://x.com/testuser/status/333333",
                    "tweetId": "333333",
                    "fbId": "333333",
                    "url": "https://x.com/testuser/status/333333",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "My commentary on this",
                    "quotedTweet": {
                        "url": "https://x.com/otheruser/status/444444",
                        "author": "Other User",
                        "authorHandle": "otheruser",
                        "text": "The original quoted content",
                    },
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 1

        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        r = records[0]
        assert r.content_text == "My commentary on this"
        assert r.reshared_from.author == "Other User"
        assert r.reshared_from.url == "https://x.com/otheruser/status/444444"
        assert r.reshared_from.content_text == "The original quoted content"

    def test_dedup_by_source_id(self, tmp_path):
        """When the same tweet appears in tweets and replies, keep the one with longer text."""
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "555555",
                    "fbId": "555555",
                    "url": "https://x.com/testuser/status/555555",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Short",
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        replies = {
            "phase": "replies",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "555555",
                    "fbId": "555555",
                    "url": "https://x.com/testuser/status/555555",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Much longer version of the same tweet text",
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts, comments=replies)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 1

        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        assert records[0].content_text == "Much longer version of the same tweet text"

    def test_media_manifest_attachment(self, tmp_path):
        """Media from manifest should be attached to the correct tweet."""
        tweet_url = "https://x.com/testuser/status/666666"
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "666666",
                    "fbId": "666666",
                    "url": tweet_url,
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Tweet with image",
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        manifest = [
            {
                "filename": "test_00000.jpg",
                "sourcePermalink": tweet_url,
                "originalUrl": "https://pbs.twimg.com/media/abc123.jpg?format=jpg&name=orig",
                "context": "tweet_photo",
            },
        ]
        # Create ZIP with a dummy media file
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('posts.json', json.dumps(posts))
            zf.writestr('media_manifest.json', json.dumps(manifest))
            zf.writestr('media/test_00000.jpg', b'\xff\xd8\xff\xe0' + b'\x00' * 100)  # fake JPEG
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(buf.getvalue())

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 1
        assert result["media_attached"] == 1

        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        r = records[0]
        assert len(r.media) == 1
        assert "pbs.twimg.com" in r.media[0].original_url

    def test_link_attachment(self, tmp_path):
        """Link attachments from link_attachments.json become LINK_EMBED media items."""
        tweet_url = "https://x.com/testuser/status/777777"
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "777777",
                    "fbId": "777777",
                    "url": tweet_url,
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Check out this article",
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        la = {
            tweet_url: [{"url": "https://example.com/article", "title": "Great Article", "image": ""}],
        }
        zip_bytes = make_zip(posts=posts, link_attachments=la)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        r = records[0]
        assert len(r.media) == 1
        assert r.media[0].caption == "Great Article"

    def test_dry_run(self, tmp_path):
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "888888",
                    "fbId": "888888",
                    "url": "https://x.com/testuser/status/888888",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Dry run tweet",
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out", dry_run=True)
        assert result["records"] == 1
        assert not (tmp_path / "out" / "posts.binpb").exists()

    def test_empty_zip(self, tmp_path):
        zip_bytes = make_zip(posts={"postsWithText": [], "collectedAt": "2024-06-01T12:00:00Z"})
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        assert result["records"] == 0

    def test_reaction_counts_override(self, tmp_path):
        """reaction_counts.json should override inline likeCount when higher."""
        tweet_url = "https://x.com/testuser/status/999999"
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "999999",
                    "fbId": "999999",
                    "url": tweet_url,
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Popular tweet",
                    "likeCount": 10,
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        rc = {tweet_url: 42}
        zip_bytes = make_zip(posts=posts, reaction_counts=rc)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        records = list(read_records(tmp_path / "out" / "posts.binpb"))
        assert records[0].extra.get("tw_like_count") == "42"


class TestOwnerHandleFilter:
    """Foreign-authored tweets pulled in from reply threads should be filtered out."""

    def _make_posts(self, entries: list[dict], phase: str = 'tweets', owner: str | None = None) -> dict:
        out: dict = {
            'phase': phase,
            'collectedAt': '2024-06-01T12:00:00Z',
            'postsWithText': entries,
            'postsWithTextCount': len(entries),
            'mediaCandidates': [],
        }
        if owner is not None:
            out['ownerHandle'] = owner
        return out

    def _entry(self, tweet_id: str, handle: str, text: str, **extra) -> dict:
        e = {
            'tweetId': tweet_id,
            'fbId': tweet_id,
            'url': f'https://x.com/{handle}/status/{tweet_id}',
            'timestamp': {'iso': '2024-05-20T10:00:00Z', 'utime': None, 'rawText': None},
            'text': text,
            'authorHandle': handle,
        }
        e.update(extra)
        return e

    def test_foreign_reply_thread_tweets_are_skipped(self, tmp_path):
        """Parent/sibling tweets in a reply thread (different authorHandle) must be dropped."""
        tweets = self._make_posts([
            self._entry('100', 'me', 'my original tweet'),
        ])
        replies = self._make_posts(
            [
                self._entry('200', 'me', 'my reply'),
                # Parent tweet in the thread — should be filtered out
                self._entry('201', 'otheruser', 'someone else\'s tweet we replied to'),
                # Sibling reply from a different user
                self._entry('202', 'thirdparty', 'another person in the thread'),
            ],
            phase='replies',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out', owner_handle='me')
        assert result['skipped_foreign'] == 2
        assert result['records'] == 2
        assert result['owner_handle'] == 'me'

        records = list(read_records(tmp_path / 'out' / 'posts.binpb'))
        kept_ids = {r.source_id for r in records}
        assert kept_ids == {'100', '200'}

    def test_owner_inferred_from_majority_when_not_provided(self, tmp_path):
        """With no explicit handle, the majority authorHandle in posts.json is used."""
        tweets = self._make_posts([
            self._entry('1', 'majority', 'one'),
            self._entry('2', 'majority', 'two'),
            self._entry('3', 'majority', 'three'),
        ])
        replies = self._make_posts(
            [
                self._entry('4', 'majority', 'my reply'),
                self._entry('5', 'otheruser', 'parent tweet'),
            ],
            phase='replies',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out')
        assert result['owner_handle'] == 'majority'
        assert result['skipped_foreign'] == 1
        assert result['records'] == 4

    def test_owner_handle_from_export_metadata(self, tmp_path):
        """ownerHandle at the top level of posts.json takes precedence over inference."""
        tweets = self._make_posts(
            [
                # Intentional: someone else has more posts, but ownerHandle is explicit.
                self._entry('1', 'notme', 'retweet-like thing', resharedFrom={'author': 'X', 'url': 'https://x.com/notme/status/1'}),
                self._entry('2', 'notme', 'another retweet', resharedFrom={'author': 'Y', 'url': 'https://x.com/notme/status/2'}),
                self._entry('3', 'me', 'my own post'),
            ],
            owner='me',
        )
        replies = self._make_posts(
            [
                self._entry('4', 'me', 'my reply'),
                self._entry('5', 'stranger', 'parent tweet in thread'),
            ],
            phase='replies',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out')
        assert result['owner_handle'] == 'me'
        assert result['skipped_foreign'] == 1

    def test_explicit_handle_overrides_metadata_and_inference(self, tmp_path):
        """An explicit owner_handle argument beats both export metadata and inference."""
        tweets = self._make_posts(
            [
                self._entry('1', 'metadata_owner', 'post one'),
                self._entry('2', 'metadata_owner', 'post two'),
                self._entry('3', 'real_owner', 'my own post'),
            ],
            owner='metadata_owner',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets))

        result = extract(zip_path, tmp_path / 'out', owner_handle='real_owner')
        assert result['owner_handle'] == 'real_owner'
        # 2 metadata_owner entries should be skipped as foreign
        assert result['skipped_foreign'] == 2

    def test_handle_at_sign_and_case_insensitive(self, tmp_path):
        """Leading @ and mixed case are normalised when comparing handles."""
        tweets = self._make_posts([
            self._entry('1', 'Me', 'mine'),
            self._entry('2', 'ME', 'mine too'),
            self._entry('3', 'stranger', 'not mine'),
        ])
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets))

        result = extract(zip_path, tmp_path / 'out', owner_handle='@me')
        assert result['owner_handle'] == 'me'
        assert result['skipped_foreign'] == 1
        assert result['records'] == 2


class TestReplyToSelfFilter:
    """Replies into other users' threads should be dropped; own-thread replies kept."""

    def _make_posts(self, entries: list[dict], phase: str = 'replies', owner: str | None = 'me') -> dict:
        out: dict = {
            'phase': phase,
            'collectedAt': '2024-06-01T12:00:00Z',
            'postsWithText': entries,
            'postsWithTextCount': len(entries),
            'mediaCandidates': [],
        }
        if owner is not None:
            out['ownerHandle'] = owner
        return out

    def _entry(self, tweet_id: str, text: str, handle: str = 'me', **extra) -> dict:
        e = {
            'tweetId': tweet_id,
            'fbId': tweet_id,
            'url': f'https://x.com/{handle}/status/{tweet_id}',
            'timestamp': {'iso': '2024-05-20T10:00:00Z', 'utime': None, 'rawText': None},
            'text': text,
            'authorHandle': handle,
        }
        e.update(extra)
        return e

    def test_reply_to_other_user_skipped_via_graphql(self, tmp_path):
        """A reply whose inReplyTo.screenName is someone else is dropped."""
        tweets = {
            'phase': 'tweets',
            'collectedAt': '2024-06-01T12:00:00Z',
            'postsWithText': [self._entry('100', 'own original')],
            'ownerHandle': 'me',
        }
        replies = self._make_posts([
            self._entry(
                '200', 'УМРЕШЬ',
                inReplyTo={
                    'statusId': '999',
                    'screenName': 'stranger',
                    'userId': '42',
                    'url': 'https://x.com/stranger/status/999',
                },
                isReply=True,
            ),
        ])
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out')
        assert result['skipped_non_self_reply'] == 1
        assert result['records'] == 1  # only the own tweet
        records = list(read_records(tmp_path / 'out' / 'posts.binpb'))
        assert {r.source_id for r in records} == {'100'}

    def test_reply_to_self_kept(self, tmp_path):
        """A reply to one of the owner's own tweets is kept (self-thread continuation)."""
        tweets = {
            'phase': 'tweets',
            'collectedAt': '2024-06-01T12:00:00Z',
            'postsWithText': [self._entry('100', 'start of thread')],
            'ownerHandle': 'me',
        }
        replies = self._make_posts([
            self._entry(
                '200', 'continuation',
                inReplyTo={
                    'statusId': '100',
                    'screenName': 'me',
                    'userId': '7',
                    'url': 'https://x.com/me/status/100',
                },
                isReply=True,
            ),
        ])
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out')
        assert result['skipped_non_self_reply'] == 0
        assert result['records'] == 2

    def test_reply_to_other_skipped_via_dom_fallback(self, tmp_path):
        """Pre-v1.4.2 exports have no inReplyTo; DOM replyToHandle still triggers the filter."""
        tweets = {
            'phase': 'tweets',
            'collectedAt': '2024-06-01T12:00:00Z',
            'postsWithText': [self._entry('100', 'own tweet')],
            'ownerHandle': 'me',
        }
        replies = self._make_posts([
            self._entry('200', 'grand merci', isReply=True, replyToHandle='stranger'),
        ])
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets, comments=replies))

        result = extract(zip_path, tmp_path / 'out')
        assert result['skipped_non_self_reply'] == 1
        assert result['records'] == 1

    def test_no_reply_context_is_kept(self, tmp_path):
        """Regular tweets with no reply data at all go through untouched."""
        tweets = self._make_posts(
            [self._entry('1', 'one'), self._entry('2', 'two')],
            phase='tweets',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets))

        result = extract(zip_path, tmp_path / 'out')
        assert result['skipped_non_self_reply'] == 0
        assert result['records'] == 2

    def test_quote_tweet_reply_to_other_kept(self, tmp_path):
        """Reply-that-also-quotes is kept — the quoted context is already wired into reshared_from."""
        tweets = self._make_posts(
            [
                self._entry(
                    '100', 'my take',
                    inReplyTo={
                        'statusId': '999',
                        'screenName': 'stranger',
                        'userId': '42',
                        'url': 'https://x.com/stranger/status/999',
                    },
                    isReply=True,
                    quotedTweet={
                        'url': 'https://x.com/stranger/status/999',
                        'author': 'Stranger',
                        'authorHandle': 'stranger',
                        'text': 'their post',
                    },
                ),
            ],
            phase='tweets',
        )
        zip_path = tmp_path / 'test.zip'
        zip_path.write_bytes(make_zip(posts=tweets))

        result = extract(zip_path, tmp_path / 'out')
        assert result['skipped_non_self_reply'] == 0
        assert result['records'] == 1


class TestQuoteTweetWithRetweet:
    """A retweet that also has a quotedTweet should NOT be skipped."""

    def test_retweet_with_quote_is_imported(self, tmp_path):
        posts = {
            "phase": "tweets",
            "collectedAt": "2024-06-01T12:00:00Z",
            "postsWithText": [
                {
                    "tweetId": "1010101",
                    "fbId": "1010101",
                    "url": "https://x.com/testuser/status/1010101",
                    "timestamp": {"iso": "2024-05-20T10:00:00Z", "utime": None, "rawText": None},
                    "text": "Retweet with my own take",
                    "resharedFrom": {
                        "author": "Other",
                        "url": "https://x.com/other/status/1010101",
                    },
                    "quotedTweet": {
                        "url": "https://x.com/another/status/2020202",
                        "author": "Another User",
                        "text": "Quoted content",
                    },
                },
            ],
            "postsWithTextCount": 1,
            "mediaCandidates": [],
        }
        zip_bytes = make_zip(posts=posts)
        zip_path = tmp_path / "test.zip"
        zip_path.write_bytes(zip_bytes)

        result = extract(zip_path, tmp_path / "out")
        # Quote tweet presence means this is not a pure retweet
        assert result["records"] == 1
        assert result["skipped_retweet"] == 0
