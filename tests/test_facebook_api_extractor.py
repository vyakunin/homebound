"""Tests for the Facebook Graph API extractor (no Django dependency).

All tests mock requests.Session so no real network calls are made.
"""
from __future__ import annotations

import json
import struct
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from extractors.facebook_api import (
    _map_privacy,
    _map_reaction_type,
    _parse_comments,
    _parse_reactions,
    _parse_location,
    _parse_attachments,
    _needs_fetch_embed_text,
    _build_reshared_from,
    is_unavailable_reshare_notice,
    map_post,
    validate_token,
    iter_posts,
    extract,
    _get_with_retry,
    _check_app_usage,
    _safe_filename_from_url,
)
from extractors.posts_io import read_records, write_records
from proto.comment import Comment
from proto.media_item import MediaItem, MediaType
from proto.post_record import PostRecord, Source, Visibility
from proto.reaction import Reaction, ReactionType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(json_body: dict, status_code: int = 200, headers: dict | None = None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


def _make_session(responses: list) -> MagicMock:
    """Return a mock Session whose .get() returns responses in sequence."""
    session = MagicMock()
    session.get.side_effect = responses
    session.params = {}
    return session


# ---------------------------------------------------------------------------
# Privacy -> Visibility mapping
# ---------------------------------------------------------------------------

class TestMapPrivacy:
    def test_everyone_maps_to_public(self):
        assert _map_privacy({'value': 'EVERYONE'}) == Visibility.VISIBILITY_PUBLIC

    def test_all_friends_maps_to_friends(self):
        assert _map_privacy({'value': 'ALL_FRIENDS'}) == Visibility.VISIBILITY_FRIENDS

    def test_friends_of_friends_maps_to_friends(self):
        assert _map_privacy({'value': 'FRIENDS_OF_FRIENDS'}) == Visibility.VISIBILITY_FRIENDS

    def test_self_maps_to_private(self):
        assert _map_privacy({'value': 'SELF'}) == Visibility.VISIBILITY_PRIVATE

    def test_custom_maps_to_private(self):
        assert _map_privacy({'value': 'CUSTOM'}) == Visibility.VISIBILITY_PRIVATE

    def test_none_defaults_to_friends(self):
        assert _map_privacy(None) == Visibility.VISIBILITY_FRIENDS

    def test_unknown_value_defaults_to_friends(self):
        assert _map_privacy({'value': 'UNKNOWN_NEW_TYPE'}) == Visibility.VISIBILITY_FRIENDS


# ---------------------------------------------------------------------------
# Reaction type mapping
# ---------------------------------------------------------------------------

class TestMapReactionType:
    @pytest.mark.parametrize('fb_type, expected', [
        ('LIKE',  ReactionType.REACTION_TYPE_LIKE),
        ('LOVE',  ReactionType.REACTION_TYPE_LOVE),
        ('HAHA',  ReactionType.REACTION_TYPE_HAHA),
        ('WOW',   ReactionType.REACTION_TYPE_WOW),
        ('SAD',   ReactionType.REACTION_TYPE_SAD),
        ('ANGRY', ReactionType.REACTION_TYPE_ANGRY),
        ('CARE',  ReactionType.REACTION_TYPE_CARE),
    ])
    def test_all_fb_types(self, fb_type, expected):
        assert _map_reaction_type(fb_type) == expected

    def test_lowercase_accepted(self):
        assert _map_reaction_type('like') == ReactionType.REACTION_TYPE_LIKE

    def test_unknown_type_maps_to_other(self):
        assert _map_reaction_type('THUMBS_DOWN') == ReactionType.REACTION_TYPE_OTHER


# ---------------------------------------------------------------------------
# Comment threading
# ---------------------------------------------------------------------------

class TestParseComments:
    def test_top_level_comment_has_empty_parent_id(self):
        data = {'data': [
            {'id': 'c1', 'message': 'Hello', 'from': {'name': 'Alice'}, 'created_time': '2023-01-01T10:00:00+0000'},
        ]}
        comments = _parse_comments(data)
        assert len(comments) == 1
        assert comments[0].source_id == 'c1'
        assert comments[0].author == 'Alice'
        assert comments[0].text == 'Hello'
        assert comments[0].parent_comment_id == ''

    def test_reply_inherits_parent_comment_id(self):
        data = {'data': [
            {'id': 'c1', 'message': 'Top comment', 'from': {'name': 'Alice'}, 'created_time': '2023-01-01T10:00:00+0000'},
            {'id': 'c2', 'message': 'Reply', 'from': {'name': 'Bob'}, 'created_time': '2023-01-01T10:05:00+0000',
             'parent': {'id': 'c1'}},
        ]}
        comments = _parse_comments(data)
        assert len(comments) == 2
        assert comments[0].parent_comment_id == ''
        assert comments[1].parent_comment_id == 'c1'

    def test_empty_data_returns_empty_list(self):
        assert _parse_comments(None) == []
        assert _parse_comments({}) == []
        assert _parse_comments({'data': []}) == []

    def test_comment_timestamp_parsed(self):
        data = {'data': [
            {'id': 'c1', 'message': 'Hi', 'from': {'name': 'X'}, 'created_time': '2023-06-15T08:30:00+0000'},
        ]}
        comments = _parse_comments(data)
        assert comments[0].date == datetime(2023, 6, 15, 8, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------

class TestParseReactions:
    def test_parses_all_reaction_types(self):
        data = {'data': [
            {'type': 'LIKE', 'name': 'Alice'},
            {'type': 'LOVE', 'name': 'Bob'},
            {'type': 'HAHA', 'name': 'Carol'},
        ]}
        reactions = _parse_reactions(data)
        assert len(reactions) == 3
        assert reactions[0].type == ReactionType.REACTION_TYPE_LIKE
        assert reactions[0].user == 'Alice'
        assert reactions[1].type == ReactionType.REACTION_TYPE_LOVE
        assert reactions[2].type == ReactionType.REACTION_TYPE_HAHA

    def test_empty_returns_empty(self):
        assert _parse_reactions(None) == []
        assert _parse_reactions({'data': []}) == []


# ---------------------------------------------------------------------------
# Location parsing
# ---------------------------------------------------------------------------

class TestParseLocation:
    def test_place_with_coordinates(self):
        place = {
            'name': 'Central Park',
            'location': {'latitude': 40.785091, 'longitude': -73.968285},
        }
        loc = _parse_location(place)
        assert loc is not None
        assert loc.name == 'Central Park'
        assert abs(loc.lat - 40.785091) < 1e-6
        assert abs(loc.lng - (-73.968285)) < 1e-6

    def test_none_returns_none(self):
        assert _parse_location(None) is None

    def test_place_without_location_sub_object(self):
        loc = _parse_location({'name': 'Somewhere', 'location': {}})
        assert loc is not None
        assert loc.name == 'Somewhere'
        assert loc.lat == 0.0
        assert loc.lng == 0.0


# ---------------------------------------------------------------------------
# Media attachments
# ---------------------------------------------------------------------------

class TestParseAttachments:
    def test_photo_attachment_downloaded(self, tmp_path):
        session = MagicMock()
        img_bytes = b'FAKEIMAGE'
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [img_bytes]

        data = {'data': [
            {'type': 'photo', 'media': {'image': {'src': 'https://cdn.fb.com/photo.jpg'}}}
        ]}
        created = datetime(2023, 3, 15, tzinfo=timezone.utc)
        with patch('extractors.facebook_api.requests.get', return_value=mock_resp) as raw_get:
            items = _parse_attachments(data, tmp_path / 'media', created, session)

        assert len(items) == 1
        assert items[0].type == MediaType.MEDIA_TYPE_IMAGE
        assert items[0].original_url == 'https://cdn.fb.com/photo.jpg'
        assert items[0].local_path != ''
        raw_get.assert_called_once()

    def test_link_attachment_stored_without_download(self, tmp_path):
        session = MagicMock()
        data = {'data': [
            {'type': 'share', 'url': 'https://example.com/article', 'title': 'An article'}
        ]}
        created = datetime(2023, 1, 1, tzinfo=timezone.utc)
        items = _parse_attachments(data, tmp_path / 'media', created, session)

        assert len(items) == 1
        assert items[0].type == MediaType.MEDIA_TYPE_LINK_EMBED
        assert items[0].original_url == 'https://example.com/article'
        session.get.assert_not_called()

    def test_video_attachment_stored_as_url(self, tmp_path):
        session = MagicMock()
        data = {'data': [
            {'type': 'video_inline', 'media': {'video': {'url': 'https://video.fb.com/v.mp4'}}}
        ]}
        with patch('extractors.facebook_api.requests.get'):
            items = _parse_attachments(data, tmp_path / 'media', None, session)

        assert len(items) == 1
        assert items[0].type == MediaType.MEDIA_TYPE_VIDEO
        assert items[0].local_path == ''

    def test_empty_attachments_returns_empty(self, tmp_path):
        assert _parse_attachments(None, tmp_path / 'media', None, MagicMock()) == []
        assert _parse_attachments({'data': []}, tmp_path / 'media', None, MagicMock()) == []

    def test_album_subattachments_expanded(self, tmp_path):
        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content.return_value = [b'DATA']

        data = {'data': [
            {'type': 'album', 'subattachments': {'data': [
                {'type': 'photo', 'media': {'image': {'src': 'https://cdn.fb.com/1.jpg'}}},
                {'type': 'photo', 'media': {'image': {'src': 'https://cdn.fb.com/2.jpg'}}},
            ]}}
        ]}
        media_dir = tmp_path / 'media'
        media_dir.mkdir()
        created = datetime(2023, 5, 1, tzinfo=timezone.utc)
        with patch('extractors.facebook_api.requests.get', return_value=mock_resp):
            items = _parse_attachments(data, media_dir, created, session)

        assert len(items) == 2

    def test_reel_link_downloads_mp4_when_source_returned(self, tmp_path):
        """Share URL with /reel/ triggers Video node source fetch + CDN download."""
        from extractors.facebook_api import _extract_facebook_video_id

        assert _extract_facebook_video_id('https://www.facebook.com/reel/1606389767181143/') == (
            '1606389767181143'
        )

        session = MagicMock()
        cdn_resp = MagicMock()
        cdn_resp.raise_for_status = MagicMock()
        cdn_resp.iter_content.return_value = [b'MP4DATA']

        graph_src = {
            'source': 'https://video.xx.fbcdn.net/fake.mp4?token=1',
        }

        def session_get_side_effect(url, **kwargs):
            if isinstance(url, str) and '/1606389767181143' in url and 'graph.facebook.com' in url:
                return _make_response(graph_src)
            return cdn_resp

        session.get.side_effect = session_get_side_effect

        data = {'data': [
            {'type': 'share', 'url': 'https://www.facebook.com/reel/1606389767181143/', 'title': 'R'},
        ]}
        media_dir = tmp_path / 'media'
        media_dir.mkdir()
        created = datetime(2023, 6, 1, tzinfo=timezone.utc)
        with patch('extractors.facebook_api.requests.get', return_value=cdn_resp) as raw_get:
            items = _parse_attachments(data, media_dir, created, session)

        assert len(items) == 1
        assert items[0].type == MediaType.MEDIA_TYPE_VIDEO
        assert items[0].local_path != ''
        assert raw_get.call_count >= 1


# ---------------------------------------------------------------------------
# map_post — full field mapping
# ---------------------------------------------------------------------------

class TestMapPost:
    def _make_full_fb_post(self) -> dict:
        return {
            'id': '123456789',
            'message': 'Hello #world from the API!',
            'created_time': '2023-03-15T12:00:00+0000',
            'updated_time': '2023-03-15T13:00:00+0000',
            'permalink_url': 'https://www.facebook.com/post/123456789',
            'privacy': {'value': 'EVERYONE'},
            'place': {'name': 'New York', 'location': {'latitude': 40.7128, 'longitude': -74.0060}},
            'attachments': None,
            'comments': {'data': [
                {'id': 'c1', 'message': 'Nice!', 'from': {'name': 'Friend'}, 'created_time': '2023-03-15T12:30:00+0000'},
            ]},
            'reactions': {'data': [
                {'type': 'LOVE', 'name': 'Fan'},
            ]},
        }

    def test_all_fields_mapped_correctly(self, tmp_path):
        session = MagicMock()
        record = map_post(self._make_full_fb_post(), tmp_path / 'media', session)

        assert record is not None
        assert record.source == Source.SOURCE_FACEBOOK
        assert record.source_id == '123456789'
        assert record.source_url == 'https://www.facebook.com/post/123456789'
        assert record.content_text == 'Hello #world from the API!'
        assert record.visibility == Visibility.VISIBILITY_PUBLIC
        assert record.created_at == datetime(2023, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert record.updated_at == datetime(2023, 3, 15, 13, 0, 0, tzinfo=timezone.utc)
        assert record.location is not None
        assert record.location.name == 'New York'
        assert len(record.comments) == 1
        assert record.comments[0].text == 'Nice!'
        assert len(record.reactions) == 1
        assert record.reactions[0].type == ReactionType.REACTION_TYPE_LOVE
        assert 'world' in record.tags

    def test_reaction_summary_extra_when_graph_omits_reaction_rows(self, tmp_path):
        """Graph summary.total_count can be >0 while reaction rows are omitted."""
        fb_post = {
            'id': 'p_rxsum',
            'message': 'x',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'reactions': {'summary': {'total_count': 6}, 'data': []},
        }
        session = MagicMock()
        session.get.return_value = _make_response({'data': []})
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.extra.get('fb_reaction_total_count') == '6'
        assert len(record.reactions) == 0

    def test_comment_summary_extra_when_graph_omits_comment_rows(self, tmp_path):
        """Graph summary.total_count can be >0 while comment objects are omitted (privacy)."""
        fb_post = {
            'id': 'p_csum',
            'message': 'x',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'summary': {'total_count': 4}, 'data': []},
        }
        with patch('extractors.facebook_api._load_all_comment_items', return_value=[]):
            record = map_post(fb_post, tmp_path / 'media', MagicMock(), {})
        assert record is not None
        assert record.extra.get('fb_comment_total_count') == '4'
        assert len(record.comments) == 0

    def test_hashtags_extracted_from_message(self, tmp_path):
        fb_post = {'id': '1', 'message': 'Check #python and #coding!',
                   'created_time': '2023-01-01T00:00:00+0000', 'privacy': {'value': 'EVERYONE'}}
        session = MagicMock()
        session.get.return_value = _make_response({'data': []})
        record = map_post(fb_post, tmp_path / 'media', session)
        assert 'python' in record.tags
        assert 'coding' in record.tags

    def test_returns_none_when_no_content(self, tmp_path):
        fb_post = {'id': '1', 'message': '', 'created_time': '2023-01-01T00:00:00+0000',
                   'privacy': {'value': 'EVERYONE'}, 'story': ''}
        session = MagicMock()
        session.get.return_value = _make_response({'data': []})
        result = map_post(fb_post, tmp_path / 'media', session)
        assert result is None

    def test_story_used_when_no_message(self, tmp_path):
        fb_post = {'id': '1', 'story': 'Alice updated her profile picture.',
                   'created_time': '2023-01-01T00:00:00+0000', 'privacy': {'value': 'EVERYONE'}}
        session = MagicMock()
        session.get.return_value = _make_response({'data': []})
        record = map_post(fb_post, tmp_path / 'media', session)
        assert record is not None
        assert record.content_text == 'Alice updated her profile picture.'

    def test_comments_pagination_merges_second_page(self, tmp_path):
        """paging.next on comments is followed via requests.get (token in URL)."""
        fb_post = {
            'id': 'p1',
            'message': 'hi',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {
                'data': [
                    {'id': 'c1', 'message': 'a', 'from': {'name': 'A'},
                     'created_time': '2023-01-01T00:00:00+0000'},
                ],
                'paging': {'next': 'https://graph.facebook.com/v19.0/cnext?access_token=xx'},
            },
        }
        page2 = {
            'data': [
                {'id': 'c2', 'message': 'b', 'from': {'name': 'B'},
                 'created_time': '2023-01-01T00:00:01+0000'},
            ],
        }
        session = MagicMock()
        with patch('extractors.facebook_api.requests.get') as raw_get:
            raw_get.return_value = _make_response(page2)
            record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert len(record.comments) == 2
        assert record.comments[0].text == 'a'
        assert record.comments[1].text == 'b'
        raw_get.assert_called_once()

    def test_comments_fallback_to_post_edge_when_nested_empty(self, tmp_path):
        """When nested comments are empty, GET /{post-id}/comments is used."""
        fb_post = {
            'id': 'post_edge_1',
            'message': 'x',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': []},
        }
        session = MagicMock()
        session.get.return_value = _make_response({
            'data': [
                {'id': 'e1', 'message': 'edge', 'from': {'name': 'E'},
                 'created_time': '2023-01-01T00:00:00+0000'},
            ],
        })
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert len(record.comments) == 1
        assert record.comments[0].text == 'edge'

    def test_reshared_embed_from_attachment_description(self, tmp_path):
        """Inline attachment description is enough when not truncated — no extra Graph GET."""
        fb_post = {
            'id': 'p1',
            'message': 'My take on this',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'attachments': {'data': [{
                'type': 'photo',
                'url': 'https://www.facebook.com/photo.php?fbid=99',
                'description': 'Original post body from the embed.',
                'target': {'id': '99'},
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.content_text == 'Original post body from the embed.'
        assert record.reshared_from.url == 'https://www.facebook.com/photo.php?fbid=99'
        session.get.assert_not_called()

    def test_reshared_embed_fetches_when_snippet_truncated(self, tmp_path):
        """Ellipsis-ending preview triggers one GET /{target-id} for full text + author."""
        fb_post = {
            'id': 'p2',
            'message': 'Sharing',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'attachments': {'data': [{
                'type': 'share',
                'url': 'https://www.facebook.com/story.php?story_fbid=88',
                'description': 'Short preview that trails off…',
                'target': {'id': '88'},
            }]},
        }

        def _ok_json(payload: dict):
            r = MagicMock()
            r.status_code = 200
            r.headers = {}
            r.json.return_value = payload
            r.raise_for_status = MagicMock()
            return r

        session = MagicMock()
        session.get.side_effect = [
            _ok_json({
                'id': '88',
                'permalink_url': 'https://www.facebook.com/story.php?story_fbid=88',
                'name': 'Full embedded text from API',
                'from': {'name': 'Igor P.', 'id': '1'},
            }),
        ]
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.content_text == 'Full embedded text from API'
        assert record.reshared_from.author == 'Igor P.'
        assert record.reshared_from.url == 'https://www.facebook.com/story.php?story_fbid=88'
        assert session.get.call_count == 1

    def test_reshared_link_share_without_target(self, tmp_path):
        """External URL share: title + description into content_text, no target GET."""
        fb_post = {
            'id': 'p3',
            'message': 'Read this',
            'created_time': '2023-01-01T00:00:00+0000',
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'privacy': {'value': 'EVERYONE'},
            'attachments': {'data': [{
                'type': 'share',
                'url': 'https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.com%2Fa',
                'title': 'Site headline',
                'description': 'OG description line.',
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record.reshared_from is not None
        assert 'Site headline' in record.reshared_from.content_text
        assert 'OG description' in record.reshared_from.content_text
        session.get.assert_not_called()

    def test_reshared_embed_unavailable_native_template(self, tmp_path):
        """Graph returns native_templates with no target when the source post is hidden."""
        fb_post = {
            'id': 'p4',
            'message': 'Commentary mentioning someone',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'permalink_url': 'https://www.facebook.com/story.php?id=p4',
            'attachments': {'data': [{
                'type': 'native_templates',
                'title': "This content isn't available right now",
                'description': (
                    "When this happens, it's usually because the owner only shared it "
                    "with a small group of people, changed who can see it or it's been deleted."
                ),
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.content_text == ''
        assert record.reshared_from.url == 'https://www.facebook.com/story.php?id=p4'
        session.get.assert_not_called()

    def test_unavailable_embed_prefers_nested_attachment_url(self, tmp_path):
        """Use attachment URL for the embedded story when Graph provides it (not parent post)."""
        fb_post = {
            'id': 'p5',
            'message': 'My comment',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'permalink_url': 'https://www.facebook.com/permalink/parent',
            'attachments': {'data': [{
                'type': 'native_templates',
                'url': 'https://www.facebook.com/story.php?story_fbid=child',
                'title': "This content isn't available right now",
                'description': "When this happens, it's usually because the owner only shared it",
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.url == 'https://www.facebook.com/story.php?story_fbid=child'
        assert record.reshared_from.content_text == ''
        session.get.assert_not_called()

    def test_unavailable_embed_notice_only_when_no_embed_url(self, tmp_path):
        """If Graph gives no permalink and no attachment URL, keep the explanatory notice."""
        fb_post = {
            'id': 'p6',
            'message': 'x',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'permalink_url': '',
            'attachments': {'data': [{
                'type': 'native_templates',
                'title': "This content isn't available right now",
                'description': "When this happens, it's usually because the owner only shared it",
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert 'Facebook did not return' in record.reshared_from.content_text
        assert record.reshared_from.url == ''
        assert is_unavailable_reshare_notice(record.reshared_from.content_text)
        session.get.assert_not_called()

    def test_reshared_embed_prefers_subattachment_when_top_matches_parent(self, tmp_path):
        """Nested subattachment URL embeds the story; no Graph GET when description is complete."""
        fb_post = {
            'id': 'p_sub',
            'message': 'Comment',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'permalink_url': 'https://www.facebook.com/story.php?story_fbid=parent',
            'attachments': {'data': [{
                'type': 'share',
                'url': 'https://www.facebook.com/story.php?story_fbid=parent',
                'description': 'Original text',
                'target': {'id': 'x'},
                'subattachments': {'data': [{
                    'url': 'https://www.facebook.com/story.php?story_fbid=child',
                }]},
            }]},
        }
        session = MagicMock()
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.url == 'https://www.facebook.com/story.php?story_fbid=child'
        session.get.assert_not_called()

    def test_reshared_embed_uses_target_permalink_when_attachment_url_is_feed_item(self, tmp_path):
        """When attachment URL is the resharing feed item, GET target ``permalink_url`` for the embed."""
        fb_post = {
            'id': 'parent',
            'message': 'Мой комментарий',
            'created_time': '2023-01-01T00:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'permalink_url': 'https://www.facebook.com/story.php?story_fbid=me',
            'attachments': {'data': [{
                'type': 'share',
                'url': 'https://www.facebook.com/story.php?story_fbid=me',
                'description': 'Игорь Поночевный у меня возникло...',
                'target': {'id': 'original_target'},
            }]},
        }

        def _ok_json(payload: dict):
            r = MagicMock()
            r.status_code = 200
            r.headers = {}
            r.json.return_value = payload
            r.raise_for_status = MagicMock()
            return r

        session = MagicMock()
        session.get.side_effect = [
            _ok_json({
                'id': 'original_target',
                'permalink_url': 'https://www.facebook.com/story.php?story_fbid=original',
                'message': 'Игорь Поночевный у меня возникло...',
                'from': {'name': 'Игорь П.', 'id': '1'},
            }),
        ]
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.reshared_from is not None
        assert record.reshared_from.url == 'https://www.facebook.com/story.php?story_fbid=original'
        assert session.get.call_count == 1

    def test_own_reel_drops_reshare_when_embed_text_equals_message(self, tmp_path):
        """Single reel: Graph attaches target to the same caption as message — show once."""
        body = (
            'Все, что я хочу на Рождество...\n\n'
            'Молитвами Спасителя нашего, Дональда Ухоцарапанного Трампа.'
        )
        fb_post = {
            'id': 'reel_post_1',
            'message': body,
            'permalink_url': 'https://www.facebook.com/reel/999',
            'created_time': '2025-12-24T12:00:00+0000',
            'privacy': {'value': 'EVERYONE'},
            'comments': {'data': [], 'summary': {'total_count': 0}},
            'attachments': {'data': [{
                'type': 'video_inline',
                'url': 'https://www.facebook.com/reel/999',
                'description': body,
                'target': {'id': 'vid_target_1'},
                'media': {'video': {'id': 'vid_target_1', 'url': 'https://www.facebook.com/reel/999'}},
            }]},
        }

        def _ok_json(payload: dict):
            r = MagicMock()
            r.status_code = 200
            r.headers = {}
            r.json.return_value = payload
            r.raise_for_status = MagicMock()
            return r

        session = MagicMock()

        def session_get(url, params=None, timeout=None, **kwargs):
            p = params or {}
            if p.get('fields') == 'source':
                return _ok_json({'id': 'vid_target_1'})
            fld = p.get('fields') or ''
            if isinstance(fld, str) and 'permalink_url' in fld:
                return _ok_json({
                    'id': 'vid_target_1',
                    'message': body,
                    'from': {'name': 'Vladimir Yakunin', 'id': '1'},
                })
            if p.get('fields') == 'from{name}':
                return _ok_json({'from': {'name': 'Vladimir Yakunin', 'id': '1'}, 'id': 'vid_target_1'})
            return _ok_json({})

        session.get.side_effect = session_get
        record = map_post(fb_post, tmp_path / 'media', session, {})
        assert record is not None
        assert record.content_text == body
        assert record.reshared_from is None


# ---------------------------------------------------------------------------
# Embedded share (Graph target)
# ---------------------------------------------------------------------------

class TestNeedsFetchEmbedText:
    def test_empty_needs_fetch(self):
        assert _needs_fetch_embed_text('') is True

    def test_full_text_no_fetch(self):
        assert _needs_fetch_embed_text('Complete sentence here.') is False

    def test_unicode_ellipsis_needs_fetch(self):
        assert _needs_fetch_embed_text('Preview truncated…') is True


class TestBuildResharedFrom:
    def test_cache_returns_same_instance(self):
        session = MagicMock()
        cache: dict = {}
        att = {
            'type': 'photo',
            'url': 'https://www.facebook.com/photo.php?fbid=1',
            'description': 'Body',
            'target': {'id': '1'},
        }
        a = _build_reshared_from(att, session, cache)
        b = _build_reshared_from(att, session, cache)
        assert a is b
        session.get.assert_not_called()


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestIterPosts:
    def test_single_page_no_next(self):
        session = MagicMock()
        page1 = {'data': [{'id': 'p1'}, {'id': 'p2'}], 'paging': {}}
        session.get.return_value = _make_response(page1)

        posts = list(iter_posts(session))
        assert len(posts) == 2
        assert posts[0]['id'] == 'p1'

    def test_multi_page_pagination_follows_next(self):
        """Page 2+ uses url_has_token=True (requests.get, not session.get) because
        Graph API paging.next URLs already embed access_token."""
        session = MagicMock()
        page1 = {
            'data': [{'id': 'p1'}],
            'paging': {'next': 'https://graph.facebook.com/v19.0/me/posts?after=cursor1&access_token=tok'},
        }
        page2 = {
            'data': [{'id': 'p2'}],
            'paging': {},
        }
        session.get.return_value = _make_response(page1)

        with patch('extractors.facebook_api.requests.get') as raw_get:
            raw_get.return_value = _make_response(page2)
            posts = list(iter_posts(session))

        assert len(posts) == 2
        assert [p['id'] for p in posts] == ['p1', 'p2']
        assert session.get.call_count == 1
        raw_get.assert_called_once()

    def test_stops_when_no_next(self):
        session = MagicMock()
        session.get.return_value = _make_response({'data': [{'id': 'only'}], 'paging': {}})

        posts = list(iter_posts(session))
        assert session.get.call_count == 1

    def test_npe_subcode_falls_back_to_feed_edge(self):
        session = MagicMock()
        npe_body = {
            'error': {
                'message': 'Permissions error',
                'type': 'OAuthException',
                'code': 200,
                'error_subcode': 2069030,
            },
        }
        feed_page = {
            'feed': {'data': [{'id': 'f1'}, {'id': 'f2'}], 'paging': {}},
            'id': 'user1',
        }
        session.get.side_effect = [
            _make_response(npe_body, status_code=400),
            _make_response(feed_page),
        ]

        posts = list(iter_posts(session))
        assert [p['id'] for p in posts] == ['f1', 'f2']
        assert session.get.call_count == 2

    def test_feed_pagination_stops_when_next_url_returns_npe(self):
        """``paging.next`` often targets /feed, which returns 2069030 for NPE profiles."""
        session = MagicMock()
        npe_body = {
            'error': {
                'message': 'Permissions error',
                'code': 200,
                'error_subcode': 2069030,
            },
        }
        page1 = {
            'feed': {
                'data': [{'id': 'a'}],
                'paging': {'next': 'https://graph.facebook.com/v19.0/uid/feed?access_token=tok'},
            },
            'id': 'user1',
        }
        session.get.side_effect = [
            _make_response(npe_body, status_code=400),
            _make_response(page1),
        ]

        with patch('extractors.facebook_api.requests.get') as raw_get:
            raw_get.return_value = _make_response(npe_body, status_code=400)
            posts = list(iter_posts(session))

        assert [p['id'] for p in posts] == ['a']
        raw_get.assert_called_once()


# ---------------------------------------------------------------------------
# Rate limit detection
# ---------------------------------------------------------------------------

class TestRateLimitDetection:
    def test_high_usage_triggers_sleep(self):
        resp = _make_response({}, headers={'X-App-Usage': json.dumps(
            {'call_count': 85, 'total_cputime': 10, 'total_time': 10}
        )})
        with patch('extractors.facebook_api.time.sleep') as mock_sleep:
            _check_app_usage(resp)
            mock_sleep.assert_called_once()
            slept = mock_sleep.call_args[0][0]
            assert slept > 0

    def test_low_usage_no_sleep(self):
        resp = _make_response({}, headers={'X-App-Usage': json.dumps(
            {'call_count': 20, 'total_cputime': 15, 'total_time': 10}
        )})
        with patch('extractors.facebook_api.time.sleep') as mock_sleep:
            _check_app_usage(resp)
            mock_sleep.assert_not_called()

    def test_missing_header_no_sleep(self):
        resp = _make_response({}, headers={})
        with patch('extractors.facebook_api.time.sleep') as mock_sleep:
            _check_app_usage(resp)
            mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# HTTP 429 retry
# ---------------------------------------------------------------------------

class TestGetWithRetry:
    def test_429_retries_then_succeeds(self):
        session = MagicMock()
        resp_429 = _make_response({}, status_code=429)
        resp_ok = _make_response({'data': 'ok'})
        session.get.side_effect = [resp_429, resp_ok]

        with patch('extractors.facebook_api.time.sleep'):
            result = _get_with_retry(session, 'https://graph.facebook.com/v19.0/me')

        assert result == {'data': 'ok'}
        assert session.get.call_count == 2

    def test_expired_token_raises_runtime_error(self):
        session = MagicMock()
        error_body = {'error': {'code': 190, 'message': 'Invalid OAuth access token.'}}
        resp = _make_response(error_body, status_code=400)
        session.get.return_value = resp

        with pytest.raises(RuntimeError, match='Access token invalid or expired'):
            _get_with_retry(session, 'https://graph.facebook.com/v19.0/me')

    def test_too_many_429_raises(self):
        session = MagicMock()
        resp_429 = _make_response({}, status_code=429)
        session.get.return_value = resp_429

        with patch('extractors.facebook_api.time.sleep'):
            with pytest.raises(RuntimeError, match='Rate limited'):
                _get_with_retry(session, 'https://graph.facebook.com/v19.0/me')


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

class TestValidateToken:
    def test_valid_token_returns_name(self):
        session = MagicMock()
        session.get.return_value = _make_response({'id': '123', 'name': 'Test User'})
        name = validate_token(session)
        assert name == 'Test User'

    def test_invalid_token_raises(self):
        session = MagicMock()
        error_body = {'error': {'code': 190, 'message': 'Invalid OAuth access token.'}}
        session.get.return_value = _make_response(error_body, status_code=400)
        with pytest.raises(RuntimeError):
            validate_token(session)


# ---------------------------------------------------------------------------
# Media download idempotency
# ---------------------------------------------------------------------------

class TestMediaDownloadIdempotency:
    def test_existing_file_not_re_downloaded(self, tmp_path):
        """A file that already exists in media/ is not re-fetched."""
        from extractors.facebook_api import _download_media

        session = MagicMock()
        media_dir = tmp_path / 'media'
        media_dir.mkdir()
        created = datetime(2023, 4, 10, tzinfo=timezone.utc)

        # Pre-create the destination file
        dest_dir = media_dir / '2023' / '04'
        dest_dir.mkdir(parents=True)
        (dest_dir / 'photo.jpg').write_bytes(b'EXISTING')

        local_path = _download_media(
            'https://cdn.fb.com/photo.jpg', media_dir, created, session
        )

        session.get.assert_not_called()
        assert local_path != ''


# ---------------------------------------------------------------------------
# Proto round-trip
# ---------------------------------------------------------------------------

class TestProtoRoundTrip:
    def test_round_trip_with_new_fields(self, tmp_path):
        """PostRecord with parent_comment_id and FB reaction types serialises and deserialises."""
        record = PostRecord(
            source=Source.SOURCE_FACEBOOK,
            source_id='987654321',
            content_text='Test post',
            comments=[
                Comment(
                    source_id='c1', author='Alice', text='Top comment',
                    parent_comment_id='',
                ),
                Comment(
                    source_id='c2', author='Bob', text='Reply',
                    parent_comment_id='c1',
                ),
            ],
            reactions=[
                Reaction(type=ReactionType.REACTION_TYPE_LOVE, user='Fan'),
                Reaction(type=ReactionType.REACTION_TYPE_HAHA, user='Joker'),
                Reaction(type=ReactionType.REACTION_TYPE_CARE, user='Friend'),
            ],
        )

        binpb = tmp_path / 'posts.binpb'
        write_records([record], binpb)

        [restored] = list(read_records(binpb))
        assert restored.source_id == '987654321'
        assert len(restored.comments) == 2
        assert restored.comments[0].parent_comment_id == ''
        assert restored.comments[1].parent_comment_id == 'c1'
        assert restored.reactions[0].type == ReactionType.REACTION_TYPE_LOVE
        assert restored.reactions[1].type == ReactionType.REACTION_TYPE_HAHA
        assert restored.reactions[2].type == ReactionType.REACTION_TYPE_CARE


# ---------------------------------------------------------------------------
# Safe filename helper
# ---------------------------------------------------------------------------

class TestSafeFilenameFromUrl:
    def test_strips_query_params(self):
        url = 'https://cdn.fb.com/photo.jpg?oh=abc&oe=def'
        assert _safe_filename_from_url(url) == 'photo.jpg'

    def test_replaces_unsafe_chars(self):
        url = 'https://cdn.fb.com/my photo (1).jpg'
        name = _safe_filename_from_url(url)
        assert ' ' not in name
        assert '(' not in name
