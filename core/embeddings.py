"""Offer embeddings for the AI matching pipeline.

Eng review locked decisions:
  - Re-embed only when name or description changes (1G + cost guard)
  - Sync call inside the import / scrape persistence path is fine at this
    scale (PLAN H11; <1s for the 22K-offer batch via the bulk endpoint)
  - 3 retries with exponential backoff on transient failures (TODO #2);
    on exhaustion leave embedding=NULL — the H17 backfill cron picks it up
    next time. No separate retry queue table needed (simpler, fewer
    moving parts than originally proposed).

Hash dedup uses sha256 of name + '\\n' + description, stored in
Offer.embedding_input_hash. embed_offer() compares hashes before any API
call so a no-op re-embed is free.

Disabled when OPENAI_API_KEY is empty (dev / tests).
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable

from django.conf import settings

from .models import Offer

logger = logging.getLogger(__name__)


def _input_text(name: str, description: str) -> str:
    return f'{name}\n{description}'.strip()


def compute_embedding_hash(name: str, description: str) -> str:
    text = _input_text(name, description)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _client():
    """Lazy OpenAI client. Raises if no API key configured."""
    from openai import OpenAI
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is empty — embedding pipeline disabled.')
    return OpenAI(api_key=api_key)


def embed_texts(texts: list[str], *, max_attempts: int = 3,
                base_backoff: float = 1.0) -> list[list[float]]:
    """Call OpenAI embeddings API for a batch. Retries transient errors.

    OpenAI's embeddings endpoint accepts up to ~2048 inputs per call. Caller
    is responsible for chunking very large batches.
    """
    if not texts:
        return []

    from openai import APIError, RateLimitError

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = _client().embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except (APIError, RateLimitError, OSError) as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            backoff = base_backoff * (4 ** (attempt - 1))
            logger.warning(
                'embed_texts attempt %d/%d failed (%s). Sleeping %.1fs.',
                attempt, max_attempts, exc, backoff,
            )
            time.sleep(backoff)

    logger.error('embed_texts giving up after %d attempts: %s', max_attempts, last_exc)
    raise last_exc  # caller decides whether to swallow or propagate


def is_enabled() -> bool:
    return bool(settings.OPENAI_API_KEY)


def embed_offer(offer: Offer) -> bool:
    """Re-embed `offer` if name/description changed since last embed.

    Returns True if the embedding is current (whether updated or already
    matching). Returns False if the API call failed OR the pipeline is
    disabled (no API key — dev/test). On API failure the offer's embedding
    stays at its previous value (possibly NULL) and the H17 backfill cron
    will retry.
    """
    if not is_enabled():
        return False

    new_hash = compute_embedding_hash(offer.name, offer.description)
    if offer.embedding_input_hash == new_hash and offer.embedding is not None:
        return True  # already current

    try:
        vectors = embed_texts([_input_text(offer.name, offer.description)])
    except Exception:
        logger.exception('Failed to embed offer %s after retries', offer.pk)
        return False

    offer.embedding = vectors[0]
    offer.embedding_input_hash = new_hash
    offer.save(update_fields=['embedding', 'embedding_input_hash', 'updated_at'])
    return True


def embed_offers_bulk(offers: Iterable[Offer], *, chunk_size: int = 500) -> dict:
    """Backfill embeddings for many offers using the bulk endpoint.

    Skips offers whose stored hash already matches their current text. Returns
    a counters dict for the management command.
    """
    counters = {'embedded': 0, 'skipped_unchanged': 0, 'failed': 0}
    chunk: list[Offer] = []
    chunk_hashes: list[str] = []

    def flush():
        if not chunk:
            return
        texts = [_input_text(o.name, o.description) for o in chunk]
        try:
            vectors = embed_texts(texts)
        except Exception:
            logger.exception('Bulk embed failed for chunk of %d', len(chunk))
            counters['failed'] += len(chunk)
            chunk.clear()
            chunk_hashes.clear()
            return
        for offer, vector, h in zip(chunk, vectors, chunk_hashes):
            offer.embedding = vector
            offer.embedding_input_hash = h
            offer.save(update_fields=['embedding', 'embedding_input_hash', 'updated_at'])
            counters['embedded'] += 1
        chunk.clear()
        chunk_hashes.clear()

    for offer in offers:
        new_hash = compute_embedding_hash(offer.name, offer.description)
        if offer.embedding_input_hash == new_hash and offer.embedding is not None:
            counters['skipped_unchanged'] += 1
            continue
        chunk.append(offer)
        chunk_hashes.append(new_hash)
        if len(chunk) >= chunk_size:
            flush()
    flush()
    return counters
