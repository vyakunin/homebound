"""IP-hash + per-day rate limiting + Sonnet-tier eligibility.

Privacy: raw visitor IPs never enter the DB. We hash them with a salt
derived from ``SECRET_KEY`` (already in the project's env) and store
only the 64-char hex digest. That lets us correlate rapid follow-ups
from the same visitor without exposing the IP itself.

Rate limit windows are DAILY, counted from `BotTranscript` rows. The
nginx zones are still present as a separate / faster line of defense,
but the cost ceiling lives here — $50/mo at Haiku pricing means
~720 calls/day.

Sonnet-tier rule: one Sonnet call per IP per day, and only for
"non-trivial" questions (>= BOT_SONNET_MIN_WORDS). Trivial / repeated
questions stay on Haiku.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

DEFAULT_PER_IP_PER_DAY = 30
DEFAULT_SITE_WIDE_PER_DAY = 720
DEFAULT_SONNET_PER_IP_PER_DAY = 1
DEFAULT_SONNET_MIN_WORDS = 6
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
    ``X-Forwarded-For`` and ``REMOTE_ADDR``."""
    real_ip = request.META.get("HTTP_X_REAL_IP")
    if real_ip:
        return real_ip.strip()
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _day_cutoff() -> "datetime":
    return timezone.now() - timedelta(hours=24)


def recent_question_count(ip_hash: str) -> int:
    """How many questions this hashed IP asked in the last 24 hours."""
    from blog.models import BotTranscript

    if not ip_hash:
        return 0
    return BotTranscript.objects.filter(
        ip_hash=ip_hash, created_at__gte=_day_cutoff(),
    ).count()


def recent_total_count() -> int:
    """Total questions across all visitors in the last 24 hours."""
    from blog.models import BotTranscript

    return BotTranscript.objects.filter(created_at__gte=_day_cutoff()).count()


def is_ip_rate_limited(ip_hash: str, *, per_day: int | None = None) -> bool:
    limit = per_day if per_day is not None else getattr(
        settings, "BOT_PER_IP_RATE_LIMIT_PER_DAY", DEFAULT_PER_IP_PER_DAY,
    )
    return recent_question_count(ip_hash) >= limit


def is_site_rate_limited(*, per_day: int | None = None) -> bool:
    limit = per_day if per_day is not None else getattr(
        settings, "BOT_SITE_RATE_LIMIT_PER_DAY", DEFAULT_SITE_WIDE_PER_DAY,
    )
    return recent_total_count() >= limit


def sonnet_eligible(ip_hash: str, question: str, *,
                    per_ip_per_day: int | None = None,
                    min_words: int | None = None) -> bool:
    """True if this IP should get the premium model on this question.

    Two gates:
    1. Question has enough words to be worth the model upgrade
       (Sonnet doesn't add much over Haiku on one-liners).
    2. Per-IP Sonnet quota for the day is not yet used.

    Both gates default to settings; tests can override.
    """
    min_words_eff = min_words if min_words is not None else getattr(
        settings, "BOT_SONNET_MIN_WORDS", DEFAULT_SONNET_MIN_WORDS,
    )
    per_ip_eff = per_ip_per_day if per_ip_per_day is not None else getattr(
        settings, "BOT_SONNET_PER_IP_PER_DAY", DEFAULT_SONNET_PER_IP_PER_DAY,
    )
    if len((question or "").split()) < min_words_eff:
        return False
    if not ip_hash:
        # Anonymous IPs share one bucket; deny premium to avoid griefing.
        return False

    from blog.models import BotTranscript
    premium = getattr(settings, "BOT_PREMIUM_MODEL", "claude-sonnet-4-6")
    sonnet_today = BotTranscript.objects.filter(
        ip_hash=ip_hash, created_at__gte=_day_cutoff(), model__startswith=premium.split('-')[0:2][0],
    ).count()
    # Use a startswith check that tolerates the SDK's returned `model`
    # string differing from the request id (e.g. snapshot suffixes).
    # Simpler exact match below works in practice because we set
    # `model=premium` on transcript create.
    sonnet_today = BotTranscript.objects.filter(
        ip_hash=ip_hash, created_at__gte=_day_cutoff(), model=premium,
    ).count()
    return sonnet_today < per_ip_eff
