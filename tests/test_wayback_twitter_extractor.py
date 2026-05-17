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

_OLD_TWITTER_REPLY_WITH_ANCESTOR = '''
<html><body>
<div class="tweet ancestor permalink-ancestor-tweet"
     data-tweet-id="700"
     data-screen-name="otheruser"
     data-conversation-id="700">
  <p class="tweet-text">parent post</p>
</div>
<div class="tweet permalink-tweet"
     data-tweet-id="701"
     data-screen-name="vyakunin"
     data-conversation-id="700"
     data-is-reply-to="true">
  <p class="tweet-text">добро пожаловать в клуб</p>
  <a class="tweet-timestamp"><span data-time="1567530900"></span></a>
</div>
</body></html>
'''

_OLD_TWITTER_REPLY_DEEP_THREAD = '''
<html><body>
<div class="tweet ancestor permalink-ancestor-tweet"
     data-tweet-id="710" data-screen-name="rootuser" data-conversation-id="710">
  <p class="tweet-text">thread root</p>
</div>
<div class="tweet ancestor"
     data-tweet-id="711" data-screen-name="middleuser" data-conversation-id="710">
  <p class="tweet-text">middle reply</p>
</div>
<div class="tweet permalink-tweet"
     data-tweet-id="712" data-screen-name="vyakunin"
     data-conversation-id="710" data-is-reply-to="true">
  <p class="tweet-text">если дискретная — тоже</p>
  <a class="tweet-timestamp"><span data-time="1567530950"></span></a>
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

    def test_reply_context_from_ancestor_div(self):
        """A reply tweet's parent is rendered as a div.tweet.ancestor.
        Parser uses data-is-reply-to=true + the closest ancestor in DOM."""
        parsed = _parse_archived_tweet(
            _OLD_TWITTER_REPLY_WITH_ANCESTOR, '20190903000000',
            expected_tweet_id='701',
        )
        assert parsed is not None
        assert parsed['reply_to_url'] == 'https://twitter.com/otheruser/status/700'
        assert parsed['reply_to_author'] == 'otheruser'
        assert parsed['quote_tweet_url'] == ''

    def test_reply_context_picks_direct_parent_in_deep_thread(self):
        """In a deep thread the parser must pick the immediate parent
        (middleuser/711), not the conversation root (rootuser/710)."""
        parsed = _parse_archived_tweet(
            _OLD_TWITTER_REPLY_DEEP_THREAD, '20190903000000',
            expected_tweet_id='712',
        )
        assert parsed is not None
        assert parsed['reply_to_url'] == 'https://twitter.com/middleuser/status/711'
        assert parsed['reply_to_author'] == 'middleuser'

    def test_non_reply_has_empty_reply_context(self):
        parsed = _parse_archived_tweet(_OLD_TWITTER_SINGLE, '20190903000000')
        assert parsed['reply_to_url'] == ''
        assert parsed['reply_to_author'] == ''

    def test_real_archived_reply_2019(self):
        """Regression test against an actual archived 2019 reply page.
        Fixture: tests/fixtures/wayback/old_reply_1169669387906764801.html
        — vyakunin's "ага, за касамару например?" replying to @mich261213."""
        import pathlib
        fixture = pathlib.Path(__file__).parent / 'fixtures' / 'wayback' / 'old_reply_1169669387906764801.html'
        if not fixture.exists():
            pytest.skip(f'fixture missing: {fixture}')
        html_str = fixture.read_text()
        parsed = _parse_archived_tweet(
            html_str, '20190920065521',
            expected_tweet_id='1169669387906764801',
        )
        assert parsed is not None
        assert parsed['tweetId'] == '1169669387906764801'
        assert 'касамару' in parsed['text']
        assert parsed['reply_to_author'] == 'mich261213'
        assert parsed['reply_to_url'] == 'https://twitter.com/mich261213/status/1169657491640266753'


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

_MODERN_REPLY_NO_PARENT_ARTICLE = '''
<html><body>
<article data-testid="tweet">
  <div>Replying to <a href="/otheruser">@otheruser</a></div>
  <div data-testid="tweetText">standalone reply, parent not in snapshot</div>
  <a href="/vyakunin/status/910">Link</a>
  <time datetime="2022-06-01T10:30:00Z"></time>
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

    def test_reply_context_from_prior_article(self):
        """In a conversation snapshot, the article preceding target is the parent."""
        parsed = _parse_archived_tweet(
            _MODERN_THREAD, '20220515103000', expected_tweet_id='901',
        )
        assert parsed is not None
        assert parsed['reply_to_url'] == 'https://twitter.com/otheruser/status/900'
        assert parsed['reply_to_author'] == 'otheruser'

    def test_reply_context_replying_to_hint(self):
        """When the parent article isn't in the snapshot, fall back to "Replying to" hint."""
        parsed = _parse_archived_tweet(
            _MODERN_REPLY_NO_PARENT_ARTICLE, '20220601103000',
            expected_tweet_id='910',
        )
        assert parsed is not None
        # No parent status ID is recoverable — author-only URL is returned.
        assert parsed['reply_to_url'] == 'https://twitter.com/otheruser'
        assert parsed['reply_to_author'] == 'otheruser'

    def test_first_article_no_reply_context(self):
        """Target is the first article — no preceding parent, no Replying hint."""
        parsed = _parse_archived_tweet(_MODERN_SINGLE, '20220515103000')
        assert parsed['reply_to_url'] == ''
        assert parsed['reply_to_author'] == ''

    def test_real_archived_modern_reply_2022(self):
        """Regression test against a real archived 2022 modern reply page.
        Fixture: tests/fixtures/wayback/modern_reply_1567792120173580288.html
        — vyakunin's "why?" replying to @apmassaro3."""
        import pathlib
        fixture = pathlib.Path(__file__).parent / 'fixtures' / 'wayback' / 'modern_reply_1567792120173580288.html'
        if not fixture.exists():
            pytest.skip(f'fixture missing: {fixture}')
        html_str = fixture.read_text()
        parsed = _parse_archived_tweet(
            html_str, '20220908083120',
            expected_tweet_id='1567792120173580288',
        )
        assert parsed is not None
        assert parsed['tweetId'] == '1567792120173580288'
        assert parsed['text'] == 'why?'
        assert parsed['reply_to_author'] == 'apmassaro3'
        assert parsed['reply_to_url'] == 'https://twitter.com/apmassaro3/status/1567763015675658241'
