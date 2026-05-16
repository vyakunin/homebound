"""Tests for Wayback Machine Twitter/X extractor.

Covers: handle normalization, tweet ID extraction, CDX timestamp parsing,
old/modern HTML parsing, thread disambiguation, media extraction, and
quote tweet detection.
"""
from datetime import datetime, timezone

import pytest

from extractors.wayback_twitter_log import (
    _extract_handle_from_status_url,
    _extract_tweet_id,
    _normalize_handle,
    _parse_archived_tweet,
    _parse_cdx_timestamp,
    _best_snapshot_per_tweet,
)


class TestNormalizeHandle:
    def test_with_at_prefix(self):
        assert _normalize_handle('@vyakunin') == 'vyakunin'

    def test_without_at_prefix(self):
        assert _normalize_handle('vyakunin') == 'vyakunin'

    def test_mixed_case(self):
        assert _normalize_handle('@VyakunIn') == 'vyakunin'

    def test_with_spaces(self):
        assert _normalize_handle('  @vyakunin  ') == 'vyakunin'


class TestExtractTweetId:
    def test_from_x_com(self):
        assert _extract_tweet_id('https://x.com/vyakunin/status/1234567890') == '1234567890'

    def test_from_twitter_com(self):
        assert _extract_tweet_id('https://twitter.com/vyakunin/status/9876543210') == '9876543210'

    def test_statuses_plural(self):
        assert _extract_tweet_id('https://twitter.com/vyakunin/statuses/1111111111') == '1111111111'

    def test_no_id(self):
        assert _extract_tweet_id('https://twitter.com/vyakunin/likes') is None

    def test_empty(self):
        assert _extract_tweet_id('') is None

    def test_none(self):
        assert _extract_tweet_id(None) is None


class TestExtractHandleFromStatusUrl:
    def test_twitter_com(self):
        assert _extract_handle_from_status_url(
            'https://twitter.com/apmassaro3/status/1539580912505077767'
        ) == 'apmassaro3'

    def test_x_com(self):
        assert _extract_handle_from_status_url(
            'https://x.com/ElonMusk/status/1234567890'
        ) == 'ElonMusk'

    def test_statuses_plural(self):
        assert _extract_handle_from_status_url(
            'https://twitter.com/jack/statuses/20'
        ) == 'jack'

    def test_empty(self):
        assert _extract_handle_from_status_url('') == ''

    def test_no_status_path(self):
        assert _extract_handle_from_status_url('https://twitter.com/jack') == ''


class TestParseCdxTimestamp:
    def test_valid(self):
        epoch = _parse_cdx_timestamp('20220512143022')
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        assert dt == datetime(2022, 5, 12, 14, 30, 22, tzinfo=timezone.utc)

    def test_invalid(self):
        assert _parse_cdx_timestamp('invalid') is None

    def test_none(self):
        assert _parse_cdx_timestamp(None) is None


class TestBestSnapshotPerTweet:
    def test_picks_newest_snapshot(self):
        snapshots = [
            {'timestamp': '20190910185700', 'url': 'https://twitter.com/vyakunin/status/111'},
            {'timestamp': '20190913135731', 'url': 'https://twitter.com/vyakunin/status/111'},
            {'timestamp': '20190912143252', 'url': 'https://twitter.com/vyakunin/status/222'},
        ]
        best = _best_snapshot_per_tweet(snapshots)
        assert len(best) == 2
        assert best['111']['timestamp'] == '20190913135731'

    def test_handles_original_key(self):
        """CDX cache may use 'original' instead of 'url'."""
        snapshots = [
            {'timestamp': '20190910185700', 'original': 'https://twitter.com/vyakunin/status/111'},
        ]
        best = _best_snapshot_per_tweet(snapshots)
        assert '111' in best


# ---------------------------------------------------------------------------
# Old Twitter HTML parsing (pre-2020: div.tweet with data-tweet-id)
# ---------------------------------------------------------------------------

_OLD_TWITTER_SINGLE = '''
<html><body>
<div class="tweet permalink-tweet" data-tweet-id="100" data-screen-name="vyakunin">
  <p class="tweet-text">Hello world</p>
  <a class="tweet-timestamp"><span data-time="1567530856"></span></a>
</div>
</body></html>
'''

_OLD_TWITTER_WITH_MEDIA = '''
<html><body>
<div class="tweet permalink-tweet" data-tweet-id="200" data-screen-name="vyakunin">
  <p class="tweet-text">Check this photo</p>
  <a class="tweet-timestamp"><span data-time="1567530856"></span></a>
  <img src="https://pbs.twimg.com/media/EDjfSjRXkAAGMcQ.jpg">
</div>
</body></html>
'''

_OLD_TWITTER_WITH_QT = '''
<html><body>
<div class="tweet permalink-tweet" data-tweet-id="300" data-screen-name="vyakunin">
  <p class="tweet-text">Look at this</p>
  <a class="tweet-timestamp"><span data-time="1567530856"></span></a>
  <div class="QuoteTweet">
    <a class="QuoteTweet-link" href="/otheruser/status/999"></a>
  </div>
</div>
</body></html>
'''

_OLD_TWITTER_THREAD = '''
<html><body>
<div class="tweet" data-tweet-id="500" data-screen-name="vyakunin">
  <p class="tweet-text">Parent tweet in thread</p>
  <a class="tweet-timestamp"><span data-time="1567530000"></span></a>
</div>
<div class="tweet" data-tweet-id="400" data-screen-name="otheruser">
  <p class="tweet-text">Original post being replied to</p>
  <a class="tweet-timestamp"><span data-time="1567520000"></span></a>
</div>
<div class="tweet" data-tweet-id="401" data-screen-name="vyakunin">
  <p class="tweet-text">My reply in the thread</p>
  <a class="tweet-timestamp"><span data-time="1567530100"></span></a>
</div>
</body></html>
'''

_OLD_TWITTER_REPLY_WITH_PARENT_IMAGE = '''
<html><body>
<div class="tweet" data-tweet-id="600" data-screen-name="otheruser">
  <p class="tweet-text">Parent with image</p>
  <img src="https://pbs.twimg.com/media/PARENT_IMAGE.jpg">
</div>
<div class="tweet permalink-tweet" data-tweet-id="601" data-screen-name="vyakunin">
  <p class="tweet-text">My reply (no image)</p>
  <a class="tweet-timestamp"><span data-time="1567530856"></span></a>
</div>
</body></html>
'''


class TestParseOldTwitterHtml:
    def test_single_tweet(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_SINGLE, '20190903000000')
        assert parsed is not None
        assert parsed['tweetId'] == '100'
        assert parsed['text'] == 'Hello world'
        assert parsed['author'] == 'vyakunin'
        assert parsed['timestamp']['utime'] == 1567530856

    def test_media_extraction(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_WITH_MEDIA, '20190903000000')
        assert parsed is not None
        assert len(parsed['media_urls']) == 1
        assert 'EDjfSjRXkAAGMcQ.jpg' in parsed['media_urls'][0]

    def test_quote_tweet_url(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_WITH_QT, '20190903000000')
        assert parsed is not None
        assert parsed['quote_tweet_url'] == 'https://twitter.com/otheruser/status/999'

    def test_no_media_no_qt(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_SINGLE, '20190903000000')
        assert parsed['media_urls'] == []
        assert parsed['quote_tweet_url'] == ''

    def test_thread_picks_expected_tweet_id(self):
        """Parser must pick the tweet matching expected_tweet_id, not max ID."""
        parsed = _parse_archived_tweet(
            _OLD_TWITTER_THREAD, '20190903000000', expected_tweet_id='401',
        )
        assert parsed is not None
        assert parsed['tweetId'] == '401'
        assert parsed['text'] == 'My reply in the thread'

    def test_thread_without_expected_id_falls_back_to_max(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_THREAD, '20190903000000')
        assert parsed is not None
        # Without expected_tweet_id, falls back to permalink-tweet (none here)
        # then max ID = 500
        assert parsed['tweetId'] == '500'

    def test_reply_does_not_steal_parent_image(self):
        """Image in parent tweet must not be attributed to the reply."""
        parsed = _parse_archived_tweet(
            _OLD_TWITTER_REPLY_WITH_PARENT_IMAGE, '20190903000000',
            expected_tweet_id='601',
        )
        assert parsed is not None
        assert parsed['tweetId'] == '601'
        assert parsed['media_urls'] == []


# ---------------------------------------------------------------------------
# Modern Twitter HTML parsing (article[data-testid="tweet"])
# ---------------------------------------------------------------------------

_MODERN_SINGLE = '''
<html><body>
<article data-testid="tweet">
  <div data-testid="tweetText">Modern tweet text</div>
  <a href="/vyakunin/status/700">Link</a>
  <time datetime="2022-05-15T10:30:00Z"></time>
</article>
</body></html>
'''

_MODERN_WITH_MEDIA = '''
<html><body>
<article data-testid="tweet">
  <div data-testid="tweetText">Tweet with photo</div>
  <a href="/vyakunin/status/800">Link</a>
  <time datetime="2022-05-15T10:30:00Z"></time>
  <img src="https://pbs.twimg.com/media/FZutf8gWYAEnD_T?format=jpg&name=900x900">
</article>
</body></html>
'''

_MODERN_THREAD = '''
<html><body>
<article data-testid="tweet">
  <div data-testid="tweetText">Parent tweet text</div>
  <a href="/otheruser/status/900">Link</a>
</article>
<article data-testid="tweet">
  <div data-testid="tweetText">My reply</div>
  <a href="/vyakunin/status/901">Link</a>
  <a href="/vyakunin/status/901/likes">Likes</a>
</article>
<article data-testid="tweet">
  <div data-testid="tweetText">Another reply</div>
  <a href="/vyakunin/status/902">Link</a>
</article>
</body></html>
'''


class TestParseModernTwitterHtml:
    def test_single_tweet(self):
        parsed = _parse_archived_tweet(_MODERN_SINGLE, '20220515103000')
        assert parsed is not None
        assert parsed['tweetId'] == '700'
        assert parsed['text'] == 'Modern tweet text'
        assert parsed['timestamp']['iso'] == '2022-05-15T10:30:00Z'

    def test_media_extraction(self):
        parsed = _parse_archived_tweet(_MODERN_WITH_MEDIA, '20220515103000')
        assert parsed is not None
        assert len(parsed['media_urls']) == 1
        assert 'FZutf8gWYAEnD_T' in parsed['media_urls'][0]

    def test_thread_picks_expected_tweet(self):
        """Parser must find the article containing the expected tweet's permalink."""
        parsed = _parse_archived_tweet(
            _MODERN_THREAD, '20220515103000', expected_tweet_id='901',
        )
        assert parsed is not None
        assert parsed['tweetId'] == '901'
        assert parsed['text'] == 'My reply'

    def test_thread_without_expected_id_picks_first(self):
        parsed = _parse_archived_tweet(_MODERN_THREAD, '20220515103000')
        assert parsed is not None
        assert parsed['tweetId'] == '900'

    def test_ignores_likes_link(self):
        """Links to /status/ID/likes must not be used for tweet matching."""
        parsed = _parse_archived_tweet(
            _MODERN_THREAD, '20220515103000', expected_tweet_id='901',
        )
        assert parsed is not None
        assert '/likes' not in parsed['url']

    def test_html_entity_decode(self):
        html = '''
        <article data-testid="tweet">
          <div data-testid="tweetText">Tweet with &amp; ampersand &lt;tag&gt;</div>
          <a href="/vyakunin/status/999">Link</a>
        </article>
        '''
        parsed = _parse_archived_tweet(html, '20220515103000')
        assert '& ampersand' in parsed['text']
        assert '<tag>' in parsed['text']

    def test_empty_html(self):
        assert _parse_archived_tweet('', '20220515103000') is None

    def test_none_html(self):
        assert _parse_archived_tweet(None, '20220515103000') is None

    def test_no_article(self):
        assert _parse_archived_tweet('<html><p>No tweet</p></html>', '20220515103000') is None
