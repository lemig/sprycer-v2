"""AI-assisted product matching pipeline (H12 + H13).

Two-stage:
  1. Candidate retrieval: pgvector cosine distance, top-K nearest competitor
     offers for a given Schleiper offer (or vice versa).
  2. LLM judge: gpt-4o-mini decides YES / NO / UNCERTAIN per candidate pair.
     Pydantic schema enforces structured output (eng review 2F).

Eng review locks (Tension B + TODO #4):
  - SKIP if a Matching row already exists for the (offer, competing_offer)
    pair. This is the critical regression test — re-running the pipeline
    must NEVER overwrite human-confirmed or human-rejected matches.
  - Auto-accept is DISABLED at cutover. All AI matches land as
    Matching.Status.SUGGESTED. Threshold flip happens after an eval set
    exists (post-cutover, Tension B).
  - LLM=NO is recorded as Matching.Status.REJECTED (not dropped) so the
    pipeline doesn't keep re-asking about the same negative pair.
  - LLM JSON parse failure -> retry once -> mark as Matching.Status.ERRORED.
"""
from __future__ import annotations

import logging
from typing import Iterable, Literal

from django.conf import settings
from django.db.models import Q
from pgvector.django import CosineDistance
from pydantic import BaseModel, Field

from .models import Matching, Offer

logger = logging.getLogger(__name__)


# ---- Pydantic structured output ------------------------------------------


class MatchDecision(BaseModel):
    """Structured response from gpt-4o-mini.

    confidence is the LLM's subjective 0.0-1.0 certainty. The threshold-flip
    decision will eventually compare this against a per-retailer threshold,
    once an eval set exists. At cutover all decisions land as suggested.
    """
    decision: Literal['YES', 'NO', 'UNCERTAIN']
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


# ---- Stage 1: candidate retrieval ---------------------------------------


def candidate_offers(offer: Offer, *, k: int = 5) -> list[Offer]:
    """Top-K nearest competitor offers by pgvector cosine distance.

    Excludes:
      - The offer itself (no self-matching).
      - Other offers belonging to the same retailer (we match across retailers,
        not within).
      - Offers without an embedding.
    """
    if offer.embedding is None:
        return []
    qs = (
        Offer.objects
        .filter(public=True, embedding__isnull=False)
        .exclude(pk=offer.pk)
        .exclude(retailer=offer.retailer)
        .annotate(distance=CosineDistance('embedding', offer.embedding))
        .order_by('distance')[:k]
    )
    return list(qs)


# ---- Stage 2: LLM judge --------------------------------------------------


def _client():
    from openai import OpenAI
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is empty — matching pipeline disabled.')
    return OpenAI(api_key=api_key)


def _format_offer(offer: Offer) -> str:
    latest = offer.price_observations.order_by('-observed_at').first()
    price_str = f'€{latest.price_cents/100:.2f}' if latest else 'unknown'
    return (
        f'  Retailer: {offer.retailer.name}\n'
        f'  SKU: {offer.sku}\n'
        f'  Brand: {offer.brand.name if offer.brand_id else "unknown"}\n'
        f'  Name: {offer.name}\n'
        f'  Description: {offer.description[:500] or "(empty)"}\n'
        f'  Price: {price_str}\n'
        f'  Image: {offer.original_image_url or "(none)"}'
    )


PROMPT_TEMPLATE = """You are a product-matching expert. Compare these two listings from
different retailers. Decide whether they refer to the SAME physical product
(same brand, same model, same size/format/variant).

Product A:
{product_a}

Product B:
{product_b}

Return JSON with: decision (YES / NO / UNCERTAIN), confidence (0.0-1.0
subjective certainty), and a one-sentence reason.

Be conservative: when in doubt, return UNCERTAIN. Brand mismatches,
size mismatches, and color mismatches all warrant NO unless explicitly
stated as identical.
"""


def llm_judge(offer_a: Offer, offer_b: Offer, *, max_attempts: int = 2) -> MatchDecision:
    """Call gpt-4o-mini for a structured YES/NO/UNCERTAIN decision."""
    prompt = PROMPT_TEMPLATE.format(
        product_a=_format_offer(offer_a),
        product_b=_format_offer(offer_b),
    )
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = _client().chat.completions.parse(
                model=settings.LLM_MATCH_MODEL,
                messages=[{'role': 'user', 'content': prompt}],
                response_format=MatchDecision,
                temperature=0.0,
            )
            choice = response.choices[0]
            if choice.message.parsed is None:
                raise ValueError('OpenAI returned no parsed payload')
            return choice.message.parsed
        except Exception as exc:
            last_exc = exc
            logger.warning(
                'llm_judge attempt %d/%d failed (%s)',
                attempt, max_attempts, exc,
            )
    assert last_exc is not None
    raise last_exc


# ---- Top-level pipeline --------------------------------------------------


def run_matching_for_offer(offer: Offer, *, k: int = 5) -> dict:
    """Find candidates + judge each one + write Matching rows.

    Returns counters: skipped_existing, suggested, rejected, errored.

    CRITICAL invariant (Tension B + TODO #4): if a Matching row already exists
    for (offer, candidate), skip. This is the only protection human-confirmed
    matches have from being overwritten by a re-run of the AI pipeline.
    """
    counters = {'skipped_existing': 0, 'suggested': 0, 'rejected': 0, 'errored': 0}
    candidates = candidate_offers(offer, k=k)

    for candidate in candidates:
        already = Matching.objects.filter(
            offer=offer, competing_offer=candidate
        ).exists() or Matching.objects.filter(
            offer=candidate, competing_offer=offer
        ).exists()
        if already:
            counters['skipped_existing'] += 1
            continue

        try:
            decision = llm_judge(offer, candidate)
        except Exception:
            logger.exception('LLM judge failed for offer=%s candidate=%s', offer.pk, candidate.pk)
            Matching.objects.create(
                offer=offer, competing_offer=candidate,
                status=Matching.Status.ERRORED,
                source=Matching.Source.AI_SUGGESTED,
                llm_reason='LLM judge failed after retries',
            )
            counters['errored'] += 1
            continue

        if decision.decision == 'NO':
            Matching.objects.create(
                offer=offer, competing_offer=candidate,
                status=Matching.Status.REJECTED,
                source=Matching.Source.AI_SUGGESTED,
                score=decision.confidence,
                llm_reason=decision.reason,
            )
            counters['rejected'] += 1
        else:
            # YES and UNCERTAIN both land as SUGGESTED at cutover (Tension B —
            # auto-accept disabled until eval exists). Confidence is recorded so
            # we can flip the threshold later.
            Matching.objects.create(
                offer=offer, competing_offer=candidate,
                status=Matching.Status.SUGGESTED,
                source=Matching.Source.AI_SUGGESTED,
                score=decision.confidence,
                llm_reason=decision.reason,
            )
            counters['suggested'] += 1

    return counters


def run_matching_for_queryset(offers: Iterable[Offer], *, k: int = 5) -> dict:
    """Run the matching pipeline across many offers. Roll-up counters."""
    totals = {'offers_processed': 0, 'skipped_existing': 0,
              'suggested': 0, 'rejected': 0, 'errored': 0}
    for offer in offers:
        if offer.embedding is None:
            continue
        c = run_matching_for_offer(offer, k=k)
        totals['offers_processed'] += 1
        for k_ in ('skipped_existing', 'suggested', 'rejected', 'errored'):
            totals[k_] += c[k_]
    return totals
