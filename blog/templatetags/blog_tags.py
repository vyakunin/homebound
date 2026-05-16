import re

from django import template
from django.templatetags.static import static
from django.utils.html import escape
from django.utils.safestring import mark_safe
from urllib.parse import quote, urlparse, parse_qs, unquote

from blog.models import PostSource, MediaType
from extractors.facebook_api import _fb_urls_equal_for_embed, is_unavailable_reshare_notice

register = template.Library()

# Official Google+ icon SVG path (Simple Icons / brand kit)
_GPLUS_SVG = (
    '<svg class="source-icon source-icon--gplus" viewBox="0 0 24 24" '
    'xmlns="http://www.w3.org/2000/svg" aria-label="Google+" role="img">'
    '<path fill="#dd4b39" d="M7.635 10.909v2.619h4.335c-.173 1.125-1.31 3.295'
    '-4.331 3.295-2.604 0-4.731-2.16-4.731-4.823 0-2.662 2.122-4.822 4.728-4.822'
    ' 1.485 0 2.479.633 3.045 1.178l2.073-1.994c-1.33-1.245-3.056-2-5.115-2C3.412'
    ' 4.362 0 7.803 0 12c0 4.198 3.412 7.638 7.635 7.638 4.408 0 7.33-3.101'
    ' 7.33-7.488 0-.502-.054-.884-.12-1.266H7.635zm16.365 0h-2.183V8.726h-2.183'
    'v2.183h-2.182v2.181h2.182v2.184h2.183v-2.184H24V10.91z"/>'
    '</svg>'
)

# Official Facebook "f" logo (Simple Icons / brand kit)
_FB_SVG = (
    '<svg class="source-icon source-icon--facebook" viewBox="0 0 24 24" '
    'xmlns="http://www.w3.org/2000/svg" aria-label="Facebook" role="img">'
    '<path fill="#1877F2" d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12'
    'c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43'
    'c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83'
    'c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385'
    'C19.612 23.027 24 18.062 24 12.073z"/>'
    '</svg>'
)


@register.filter
def source_icon(source_value):
    """Return an HTML icon/image for a source integer value."""
    if source_value == PostSource.GOOGLE_PLUS:
        return mark_safe(_GPLUS_SVG)
    if source_value == PostSource.FACEBOOK:
        return mark_safe(_FB_SVG)
    if source_value == PostSource.BLOG:
        favicon_url = static('img/thumbnail.png')
        return mark_safe(
            f'<img class="source-icon source-icon--blog"'
            f' src="{favicon_url}" alt="Blog">'
        )
    if source_value == PostSource.TWITTER:
        return mark_safe(
            '<svg class="source-icon source-icon--twitter" viewBox="0 0 24 24" '
            'xmlns="http://www.w3.org/2000/svg" aria-label="X/Twitter" role="img">'
            '<path fill="#000" d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406'
            'l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932'
            'ZM17.61 20.644h2.039L6.486 3.24H4.298Z"/>'
            '</svg>'
        )
    return ''


@register.filter
def source_label(source_value):
    """Return the human-readable label for a source integer value."""
    labels = {
        PostSource.BLOG: 'Blog',
        PostSource.GOOGLE_PLUS: 'Google+',
        PostSource.FACEBOOK: 'Facebook',
        PostSource.TWITTER: 'Twitter',
    }
    return labels.get(source_value, 'Unknown')


@register.filter
def source_slug(source_value):
    """Return the URL slug for a source integer value (used in /source/<slug>/ URLs)."""
    slugs = {
        PostSource.BLOG: 'blog',
        PostSource.GOOGLE_PLUS: 'google_plus',
        PostSource.FACEBOOK: 'facebook',
        PostSource.TWITTER: 'twitter',
    }
    return slugs.get(source_value, '')


@register.filter
def urlparse_domain(url: str) -> str:
    """Extract the bare hostname from a URL (strips www. prefix)."""
    try:
        host = urlparse(url).netloc or url
        return host.removeprefix('www.')
    except Exception:
        return url


@register.filter
def youtube_video_id(url: str) -> str:
    """Extract a YouTube video ID from any YouTube URL format.

    Handles:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/attribution_link?...&u=/watch?v%3DVIDEO_ID...
    Returns empty string if the URL is not a recognizable YouTube video URL.
    """
    if not url:
        return ''
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix('www.')

        if host == 'youtube.com' and parsed.path == '/attribution_link':
            qs = parse_qs(parsed.query)
            u_values = qs.get('u', [])
            if u_values:
                inner = unquote(u_values[0])
                if not inner.startswith('http'):
                    inner = f'https://youtube.com{inner}'
                parsed = urlparse(inner)
                host = parsed.netloc.lower().removeprefix('www.')

        if host in ('youtube.com', 'youtube-nocookie.com'):
            vid_id = parse_qs(parsed.query).get('v', [''])[0]
            return vid_id

        if host == 'youtu.be':
            return parsed.path.lstrip('/')

        return ''
    except Exception:
        return ''


@register.filter
def facebook_embed_url(url: str) -> str:
    """Facebook video/reel plugin URL for iframe embeds, or empty if not a video URL."""
    if not url:
        return ''
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix('www.')
        if host not in ('facebook.com', 'fb.com', 'fb.watch', 'm.facebook.com'):
            return ''
        path = parsed.path.lower()
        if '/reel/' in path or '/watch' in path or '/videos/' in path or 'video.php' in path:
            return (
                'https://www.facebook.com/plugins/video.php?'
                f'height=314&href={quote(url, safe="")}&show_text=false&width=560'
            )
    except Exception:
        return ''
    return ''


@register.filter
def is_fb_unavailable_reshare_notice(text: str) -> bool:
    """True when reshared_content_text is the Graph unavailable-embed placeholder."""
    return is_unavailable_reshare_notice(text or '')


def _fb_profile_from_url(url: str) -> str:
    """Extract the first path segment (profile slug) from a Facebook URL."""
    try:
        return urlparse(url).path.strip('/').split('/')[0].lower()
    except Exception:
        return ''


@register.filter
def fb_reshare_author_label(url: str) -> str:
    """Return the profile slug from a Facebook post URL, for use as attribution text."""
    return _fb_profile_from_url(url) or 'unknown'


@register.filter
def fb_reshare_embed_iframe_ok(post) -> bool:
    """True when the Embedded Post plugin URL is not the same story as this post.

    Returns False when:
    - reshared_from_url is empty (nothing to embed)
    - reshared_from_url == source_url (Graph fallback for unavailable nested embeds)
    - both URLs share the same profile slug (own-profile reshare — render manually)
    """
    if post.source != PostSource.FACEBOOK:
        return True
    ru = (post.reshared_from_url or '').strip()
    su = (post.source_url or '').strip()
    if not ru:
        return False
    if not su:
        return True
    if _fb_urls_equal_for_embed(ru, su):
        return False
    rp = _fb_profile_from_url(ru)
    sp = _fb_profile_from_url(su)
    if rp and sp and rp == sp:
        return False
    return True


def _profile_name_from_url(url: str) -> str:
    """Return a display name for a reshared post URL.

    Priority: ProfileLink display_name → URL slug (capitalised).
    """
    if not url:
        return ''
    from blog.models import ProfileLink
    try:
        slug = urlparse(url).path.strip('/').split('/')[0]
    except Exception:
        slug = ''
    _non_profile = {'permalink.php', 'photo', 'photo.php', 'watch', 'reel',
                     'stories', 'events', 'groups', 'pages', 'marketplace'}
    if not slug or slug in _non_profile:
        return ''
    # Try exact profile URL prefix match in ProfileLink table
    prefix = f'https://www.facebook.com/{slug}'
    pl = ProfileLink.objects.filter(profile_url__startswith=prefix).first()
    if pl:
        return pl.display_name
    # Fall back to slug with spaces instead of dots
    return slug.replace('.', ' ').title()


@register.simple_tag
def reshare_info(post):
    """Return a dict with reshare attribution for display.

    Keys:
      author     — display name of the original author (str, may be empty)
      author_url — profile URL of the original author (str, may be empty)
      original   — linked Post object from our DB (or None)
    """
    from blog.models import Post as PostModel
    ru = (post.reshared_from_url or '').strip()
    ra = (post.reshared_from_author or '').strip()

    author = ra or _profile_name_from_url(ru)
    author_url = ''
    if ru:
        try:
            parsed_url = urlparse(ru)
            host = parsed_url.hostname or ''
            slug = parsed_url.path.strip('/').split('/')[0]
            if slug:
                if host in ('x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'):
                    author_url = f'https://x.com/{slug}'
                    if not author:
                        author = f'@{slug}'
                else:
                    author_url = f'https://www.facebook.com/{slug}'
        except Exception:
            pass

    original = None
    if ru:
        original = PostModel.objects.filter(source_url=ru).first()
        if original is None:
            # Try matching by source_id extracted from URL (pfbid…)
            try:
                parts = urlparse(ru).path.strip('/').split('/')
                sid = next((p for p in parts if p.startswith('pfbid') or p.isdigit()), None)
                if sid:
                    original = PostModel.objects.filter(source_id=sid).first()
            except Exception:
                pass
        # Don't link a post to itself (can happen when source_id matches reshared_from_url)
        if original is not None and original.pk == post.pk:
            original = None

    return {'author': author, 'author_url': author_url, 'original': original}


_URL_RE = r'https?://\S+'


@register.filter(is_safe=True)
def linkify_mentions(text: str) -> str:
    """Linkify HTTP/HTTPS URLs and known profile names in plain comment text.

    URL pattern takes priority; profile names use longest-match-first so that
    "Alex Mittelman" beats a shorter "Alex" entry.  Operates on raw (unescaped)
    text so query-string ``&`` characters are handled correctly.
    """
    if not text:
        return escape(text)
    from blog.models import ProfileLink
    links = list(ProfileLink.objects.values('display_name', 'profile_url'))

    name_to_url: dict[str, str] = {}
    name_parts: list[str] = []
    if links:
        links.sort(key=lambda x: -len(x['display_name']))
        for link in links:
            name_to_url[link['display_name']] = link['profile_url']
            name_parts.append(re.escape(link['display_name']))

    if name_parts:
        combined = re.compile(f'({_URL_RE})|({"|".join(name_parts)})')
    else:
        combined = re.compile(f'({_URL_RE})')

    result: list[str] = []
    pos = 0
    for m in combined.finditer(text):
        result.append(escape(text[pos:m.start()]))
        matched = m.group(0)
        if m.group(1) is not None:
            # URL — strip trailing punctuation unlikely to be part of the URL
            url = matched.rstrip('.,;:!?)\'">')
            result.append(
                f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
            )
        else:
            profile_url = name_to_url.get(matched, '')
            if profile_url:
                result.append(
                    f'<a href="{escape(profile_url)}" target="_blank" rel="noopener noreferrer">{escape(matched)}</a>'
                )
            else:
                result.append(escape(matched))
        pos = m.end()
    result.append(escape(text[pos:]))
    return mark_safe(''.join(result))


@register.filter(is_safe=True)
def linkify_tweet(text: str) -> str:
    """Linkify URLs, @mentions, and #hashtags in tweet text.

    Handles newlines within URLs by removing them (common extraction artifact).
    """
    if not text:
        return ''
    # Remove newlines within URLs (extraction artifact where URLs get split across lines)
    text = re.sub(r'(https?://\S*)\n+', r'\1', text)

    parts = []
    # Combined regex: URLs first, then @mentions, then #hashtags
    pattern = re.compile(
        r'(https?://\S+)'           # URLs
        r'|(@[A-Za-z0-9_]+)'        # @mentions
        r'|(#[A-Za-z0-9_\u0400-\u04FF]+)'  # #hashtags
    )
    pos = 0
    for m in pattern.finditer(text):
        parts.append(escape(text[pos:m.start()]))
        if m.group(1):
            url = m.group(1).rstrip('.,;:!?)\'">')
            parts.append(
                f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
            )
        elif m.group(2):
            handle = m.group(2)
            parts.append(
                f'<a href="https://x.com/{escape(handle[1:])}" target="_blank" '
                f'rel="noopener noreferrer">{escape(handle)}</a>'
            )
        elif m.group(3):
            tag = m.group(3)
            parts.append(
                f'<a href="https://x.com/search?q={escape(quote(tag))}" target="_blank" '
                f'rel="noopener noreferrer">{escape(tag)}</a>'
            )
        pos = m.end()
    parts.append(escape(text[pos:]))
    return mark_safe(''.join(parts))


@register.filter
def tweet_status_id(url: str) -> str:
    """Extract the numeric status ID from a tweet URL."""
    if not url:
        return ''
    m = re.search(r'/status(?:es)?/(\d+)', url)
    return m.group(1) if m else ''


@register.filter
def twitter_embed_url(url: str) -> str:
    """Convert a tweet URL to the canonical form for embedding.

    Handles x.com, twitter.com, www variants, and fragments.
    Returns empty string if URL is not a valid tweet status URL.
    """
    if not url:
        return ''
    # Strip fragments and trailing params that aren't meaningful
    url = url.split('#')[0].rstrip('/')
    # Match tweet URLs: https://[www.]x.com/user/status/ID or https://[www.]twitter.com/user/status/ID
    m = re.search(r'https?://(?:www\.)?(?:x\.com|twitter\.com)/([^/\?]+)/status(?:es)?/(\d+)', url)
    if m:
        handle = m.group(1)
        tweet_id = m.group(2)
        # Return canonical twitter.com URL (most compatible with embed widget)
        return f'https://twitter.com/{handle}/status/{tweet_id}'
    # Invalid URL — return empty so template falls back to custom card
    return ''


@register.filter
def facebook_post_plugin_embed_url(url: str) -> str:
    """Embedded Post plugin iframe ``src`` for any public Facebook permalink (post/reel)."""
    if not url:
        return ''
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix('www.')
        if host not in (
            'facebook.com',
            'fb.com',
            'fb.watch',
            'm.facebook.com',
            'l.facebook.com',
        ):
            return ''
        return (
            'https://www.facebook.com/plugins/post.php?'
            f'height=600&href={quote(url, safe="")}&show_text=true&width=500'
        )
    except Exception:
        return ''
