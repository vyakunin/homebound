"""IP-hash + per-IP rate limiting for the public bot.

Privacy: raw visitor IPs never enter the DB. We hash them with a salt
derived from ``SECRET_KEY`` (already in the project's env) and store
only the 64-char hex digest. That lets us correlate rapid follow-ups
from the same visitor without exposing the IP itself.

Rate limit: 10 questions per IP per hour, counted via a SELECT on
``BotTranscript``. The hard limit lives at nginx (`pb_heavy` zone plus
a dedicated `pb_bot` zone added at Phase 5 deploy). This is the
belt-and-suspenders defense — survives an nginx-config drift, gives a
clearer JSON error to the visitor, and respects the per-row throttle
even if the visitor cycles past nginx via different CF edges.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

# Per-IP default — generous enough that a curious visitor can have a
# real back-and-forth, tight enough to dissuade casual scraping.
DEFAULT_PER_IP_PER_HOUR = 30
# Site-wide cap — bounds total Anthropic spend regardless of how many
# IPs are asking. At ~$0.003 per Sonnet 4.6 answer this caps the
# hourly bill near $1.50.
DEFAULT_SITE_WIDE_PER_HOUR = 500
HASH_HEX_LEN = 64


def ip_hash_for(ip: str | None) -> str:
    """SHA-256(salt + IP) hex. Never returns the raw IP. Empty string
    when no IP is available (e.g. local dev with `runserver` and no
    upstream proxy) — the throttle then groups all anonymous visitors
    into one bucket, which is the safe fallback."""
    if not ip:
        return ""
    salt = getattr(settings, "BOT_IP_HASH_SALT", None) or (settings.SECRET_KEY or "")[:32]
    return hashlib.sha256(f"{salt}|{ip}".encode("utf-8")).hexdigest()


def extract_client_ip(request) -> str | None:
    """Resolve the client IP from a Django request, preferring nginx's
    ``X-Real-IP`` (set in the prod vhost) and falling back to
    ``X-Forwarded-For`` and ``REMOTE_ADDR``. ``X-Forwarded-For`` may
    contain a comma-separated chain; the leftmost entry is the
    originating client."""
    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def recent_question_count(ip_hash: str, *, window: timedelta) -> int:
    """How many questions this hashed IP asked in the last ``window``.
    Imported lazily to avoid pulling models at module-import time."""
    from blog.models import BotTranscript

    if not ip_hash:
        return 0
    cutoff = timezone.now() - window
    return BotTranscript.objects.filter(
        ip_hash=ip_hash, created_at__gte=cutoff,
    ).count()


def recent_total_count(*, window: timedelta) -> int:
    """Total questions across all visitors in the last ``window``.
    Used for the site-wide cap."""
    from blog.models import BotTranscript

    cutoff = timezone.now() - window
    return BotTranscript.objects.filter(created_at__gte=cutoff).count()


def is_ip_rate_limited(ip_hash: str, *, per_hour: int | None = None) -> bool:
    limit = per_hour if per_hour is not None else getattr(
        settings, "BOT_PER_IP_RATE_LIMIT_PER_HOUR", DEFAULT_PER_IP_PER_HOUR,
    )
    return recent_question_count(ip_hash, window=timedelta(hours=1)) >= limit


def is_site_rate_limited(*, per_hour: int | None = None) -> bool:
    limit = per_hour if per_hour is not None else getattr(
        settings, "BOT_SITE_RATE_LIMIT_PER_HOUR", DEFAULT_SITE_WIDE_PER_HOUR,
    )
    return recent_total_count(window=timedelta(hours=1)) >= limit
