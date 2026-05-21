"""Voyage AI embeddings client for the blog's semantic search.

Used at index time by the ``generate_embeddings`` management command (one
vector per Post, ``input_type=document``) and at request time by the
``/api/search/semantic/`` view (one vector per user query,
``input_type=query``). Voyage uses asymmetric heads internally; passing
the right ``input_type`` on each side noticeably improves recall.

Auth: bearer token from ``${VOYAGE_API_KEY}`` env var (priority) or the
file at ``~/tokens/homebound_voyage_key`` (production path, mode 600).
The Docker image mounts that file as a secret at
``/run/secrets/voyage_api_key`` and exports its path via
``VOYAGE_API_KEY_FILE`` — see ``docker-compose.yml``.

When no key is configured every call raises
``EmbeddingsUnavailableError`` and the caller is expected to degrade
gracefully (keyword search still works without embeddings).

Model: ``voyage-3.5`` at 1024 dimensions — kept in sync with the
migrations 0006 (vector(1024)) + 0011 (model rebump). voyage-3.5 has
materially better cross-lingual recall than voyage-3-lite at the cost
of a 2x dim (irrelevant at this scale: ~25 MB more on disk) and ~3x
per-token price ($0.06 vs $0.02 per 1M). Backfill cost: ~$0.50
for the full corpus + chunks.

Spike data (10 EN queries → 8 RU seed posts + 50 distractors, May 2026):
- voyage-3-lite:   mean rank 1.3, hit@1 80%, hit@3 100%
- voyage-3.5-lite: mean rank 1.2, hit@1 80%, hit@3 100%
- voyage-3.5:      mean rank 1.1, hit@1 90%, hit@3 100%
The 90% hit@1 catches the Venezuela query that voyage-3-lite ranked at
position 2 — exact bug observed in the bot transcripts.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_MODEL = "voyage-3.5"
DEFAULT_DIM = 1024
DEFAULT_TIMEOUT_S = 30.0
# Voyage's per-request batch limit. Backfill hits this often; query-time
# never gets close (single string per request).
MAX_BATCH = 128

InputType = Literal["query", "document"]


class EmbeddingsUnavailableError(RuntimeError):
    """Raised when the embeddings provider can't be reached — missing
    API key, network failure, auth rejection, or malformed response.
    The semantic-search view treats this as soft-fail (falls back to
    keyword search); the backfill command treats it as hard-fail."""


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """One vector + the model bookkeeping we persist on the Post row."""

    vector: list[float]
    model: str
    dim: int


def content_hash(text: str) -> str:
    """SHA-256 hex of the canonical embed-input string. Used by the
    backfill to skip rows whose text hasn't changed since the last run.
    Kept as a free function (not bound to Post) so tests can hash
    arbitrary inputs without ORM ceremony."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed_input_for(title: str, content_text: str) -> str:
    """Canonical text fed into the embedding model for one Post. Title
    and body are joined by a blank line so the model sees them as one
    coherent document but with a clear structural break. Whitespace is
    collapsed so that hash matching survives trivial re-imports that
    only change line endings."""
    title = (title or "").strip()
    body = (content_text or "").strip()
    if title and body:
        joined = f"{title}\n\n{body}"
    else:
        joined = title or body
    return " ".join(joined.split())


# Chunking knobs for chunk-level embeddings (Phase 2 anti-long-doc-bias).
# Target chunk size is ~800 chars (~150-250 tokens for mixed RU/EN), small
# enough that one chunk maps to one "thought" yet big enough that a single
# concept usually fits inside one chunk without fragmentation. Overlap
# carries 100 chars into the next chunk so a phrase straddling a boundary
# is still findable on both sides.
CHUNK_TARGET_CHARS = 800
CHUNK_OVERLAP_CHARS = 100


def chunk_text(text: str, *, target: int = CHUNK_TARGET_CHARS, overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Split a body of text into overlapping chunks.

    Prefers semantic break points in order: paragraph (\\n\\n), sentence
    boundary (.?!), then hard char split. Short inputs (≤ target) return
    a single chunk. Empty input returns [].
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + target, n)
        if end < n:
            # Look for a clean break point in the back half of the window
            # so chunks don't end mid-sentence when avoidable.
            search_from = start + target // 2
            para_break = text.rfind("\n\n", search_from, end)
            if para_break != -1:
                end = para_break
            else:
                sent_break = max(
                    text.rfind(". ", search_from, end),
                    text.rfind("! ", search_from, end),
                    text.rfind("? ", search_from, end),
                    text.rfind(".\n", search_from, end),
                )
                if sent_break != -1:
                    end = sent_break + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= n:
            break
        # Step forward with overlap; never step backwards.
        start = max(start + 1, end - overlap)
    return chunks


def chunk_input_for(title: str, content_text: str, reshared_text: str = "") -> list[str]:
    """Chunk a Post into embed inputs.

    Title is prepended to the FIRST chunk only — repeating the title in
    every chunk would bias retrieval toward title-heavy posts and waste
    embedding tokens. Body and reshared content are concatenated (with
    a blank line between) because at retrieval time we want either to
    surface the post for either side.
    """
    title = (title or "").strip()
    body = (content_text or "").strip()
    reshared = (reshared_text or "").strip()
    parts: list[str] = []
    if body:
        parts.append(body)
    if reshared:
        parts.append(reshared)
    full = "\n\n".join(parts)
    chunks = chunk_text(full)
    if title and chunks:
        chunks[0] = f"{title}\n\n{chunks[0]}"
    elif title and not chunks:
        chunks = [title]
    return chunks


def _api_key() -> str | None:
    """Resolve the Voyage API key. Env var wins (lets tests + ad-hoc
    runs override without touching disk); file paths cover production
    (Docker secret) and local-dev (``~/tokens/``). Returns ``None`` when
    nothing is configured so callers can short-circuit cleanly."""
    env = os.environ.get("VOYAGE_API_KEY", "").strip()
    if env:
        return env
    candidates = [
        os.environ.get("VOYAGE_API_KEY_FILE"),
        str(Path.home() / "tokens" / "homebound_voyage_key"),
    ]
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip() or None
            except OSError:
                continue
    return None


def is_available() -> bool:
    """Cheap key-presence check. Use from request handlers to return a
    503-with-explanation instead of attempting a doomed Voyage call."""
    return _api_key() is not None


def embed_query(text: str, *, model: str = DEFAULT_MODEL) -> EmbeddingResult:
    """Embed ONE user query string with ``input_type=query``."""
    results = embed_batch([text], input_type="query", model=model)
    return results[0]


def embed_batch(
    texts: list[str],
    *,
    input_type: InputType,
    model: str = DEFAULT_MODEL,
) -> list[EmbeddingResult]:
    """Embed up to ``MAX_BATCH`` texts in one Voyage call. ``input_type``
    is the only knob callers think about: ``query`` for runtime
    lookups, ``document`` for index-time embedding of Post bodies.

    Empty input returns ``[]`` without making the call — Voyage charges
    per token, so accidental zero-input hits should be free.
    """
    if not texts:
        return []
    key = _api_key()
    if not key:
        raise EmbeddingsUnavailableError(
            "Voyage API key missing — set VOYAGE_API_KEY, or point "
            "VOYAGE_API_KEY_FILE / write ~/tokens/homebound_voyage_key (mode 600)."
        )
    out: list[EmbeddingResult] = []
    for chunk_start in range(0, len(texts), MAX_BATCH):
        chunk = texts[chunk_start:chunk_start + MAX_BATCH]
        try:
            resp = _call_voyage(key, chunk, input_type=input_type, model=model)
        except (httpx.HTTPError, ValueError) as e:
            raise EmbeddingsUnavailableError(f"Voyage call failed: {e}") from e
        for vector in resp:
            out.append(EmbeddingResult(vector=vector, model=model, dim=len(vector)))
    return out


def _call_voyage(
    api_key: str,
    inputs: list[str],
    *,
    input_type: InputType,
    model: str,
) -> list[list[float]]:
    """Single Voyage Embeddings API call. Returns the raw vectors in
    input order. Errors raised here get translated to
    ``EmbeddingsUnavailableError`` upstream so callers can fall back
    cleanly."""
    body = {
        "input": inputs,
        "model": model,
        "input_type": input_type,
        "output_dimension": DEFAULT_DIM,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=DEFAULT_TIMEOUT_S) as client:
        resp = client.post(VOYAGE_API_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Voyage HTTP {resp.status_code}: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )
    data = resp.json()
    rows = data.get("data") or []
    rows.sort(key=lambda r: int(r.get("index", 0)))
    out: list[list[float]] = []
    for r in rows:
        v = r.get("embedding")
        if not isinstance(v, list) or not v:
            raise ValueError(f"Voyage row had no embedding: {r}")
        out.append([float(x) for x in v])
    if len(out) != len(inputs):
        raise ValueError(
            f"Voyage returned {len(out)} vectors for {len(inputs)} inputs"
        )
    return out
