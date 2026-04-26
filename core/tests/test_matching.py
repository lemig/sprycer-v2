"""Tests for the AI matching pipeline (H12 + H13).

Includes the CRITICAL Tension B regression: re-running the pipeline must NOT
modify Matching rows that already exist (TODO #4 in eng review).

OpenAI calls are mocked at the llm_judge boundary. pgvector cosine distance
is exercised against a real Postgres so the candidate query is genuinely
verified.
"""
from unittest.mock import patch

import pytest

from core.matching import (
    MatchDecision,
    candidate_offers,
    run_matching_for_offer,
)
from core.models import Channel, Matching, Offer, PriceObservation, Retailer, Website


def _vec(seed: int = 0):
    """Deterministic 1536-dim vector. Seed controls direction in space so
    cosine distance is predictable in tests."""
    base = [0.0] * 1536
    base[seed % 1536] = 1.0
    return base


@pytest.fixture
def world(db):
    """Two retailers + a few offers each, with embeddings set."""
    sch = Retailer.objects.create(name='Schleiper')
    rp = Retailer.objects.create(name='Rougier & Plé')
    sch_w = Website.objects.create(host='www.schleiper.com')
    rp_w = Website.objects.create(host='www.rougier-ple.fr')
    sch_c = Channel.objects.create(name='schleiper.com/eshopexpress', retailer=sch, website=sch_w)
    rp_c = Channel.objects.create(name='rougier-ple.fr', retailer=rp, website=rp_w)

    sch_offer = Offer.objects.create(
        retailer=sch, channel=sch_c, website=sch_w,
        sku='S1', name='Marker n° 1', description='A blue marker.',
        public=True, embedding=_vec(1),
    )
    rp_close = Offer.objects.create(
        retailer=rp, channel=rp_c, website=rp_w,
        sku='RP1', name='Same blue marker', description='blue marker',
        public=True, embedding=_vec(1),  # identical -> cosine=0
    )
    rp_far = Offer.objects.create(
        retailer=rp, channel=rp_c, website=rp_w,
        sku='RP2', name='Unrelated paintbrush',
        public=True, embedding=_vec(500),  # very different
    )
    return {'sch_offer': sch_offer, 'rp_close': rp_close, 'rp_far': rp_far,
            'sch': sch, 'rp': rp}


# ---- Stage 1: candidate retrieval ---------------------------------------


@pytest.mark.django_db
class TestCandidateOffers:
    def test_returns_nearest_first(self, world):
        candidates = candidate_offers(world['sch_offer'], k=5)
        assert candidates[0].pk == world['rp_close'].pk

    def test_excludes_offers_from_same_retailer(self, world):
        sch2 = Offer.objects.create(
            retailer=world['sch'], channel=world['sch_offer'].channel,
            website=world['sch_offer'].website, sku='S2', name='Other Schleiper item',
            public=True, embedding=_vec(1),
        )
        candidates = candidate_offers(world['sch_offer'])
        assert sch2 not in candidates

    def test_excludes_self(self, world):
        candidates = candidate_offers(world['sch_offer'])
        assert world['sch_offer'] not in candidates

    def test_excludes_offers_without_embedding(self, world):
        no_emb = Offer.objects.create(
            retailer=world['rp'], channel=world['rp_close'].channel,
            website=world['rp_close'].website,
            sku='RP_NO_EMB', name='No embedding here', public=True, embedding=None,
        )
        candidates = candidate_offers(world['sch_offer'])
        assert no_emb not in candidates

    def test_returns_empty_when_offer_has_no_embedding(self, world):
        world['sch_offer'].embedding = None
        world['sch_offer'].save()
        assert candidate_offers(world['sch_offer']) == []


# ---- Stage 2 + pipeline -------------------------------------------------


@pytest.mark.django_db
class TestRunMatchingForOffer:
    def test_suggested_match_is_written(self, world):
        decision = MatchDecision(decision='YES', confidence=0.9, reason='Same product')
        with patch('core.matching.llm_judge', return_value=decision):
            counters = run_matching_for_offer(world['sch_offer'], k=2)
        assert counters['suggested'] >= 1
        # All AI matches at cutover are SUGGESTED, never CONFIRMED (Tension B)
        for m in Matching.objects.filter(offer=world['sch_offer']):
            assert m.status == Matching.Status.SUGGESTED
            assert m.source == Matching.Source.AI_SUGGESTED

    def test_uncertain_lands_as_suggested(self, world):
        decision = MatchDecision(decision='UNCERTAIN', confidence=0.55, reason='Hard to tell')
        with patch('core.matching.llm_judge', return_value=decision):
            run_matching_for_offer(world['sch_offer'], k=1)
        m = Matching.objects.get(offer=world['sch_offer'], competing_offer=world['rp_close'])
        assert m.status == Matching.Status.SUGGESTED

    def test_no_lands_as_rejected(self, world):
        decision = MatchDecision(decision='NO', confidence=0.95, reason='Different brands')
        with patch('core.matching.llm_judge', return_value=decision):
            run_matching_for_offer(world['sch_offer'], k=1)
        m = Matching.objects.get(offer=world['sch_offer'], competing_offer=world['rp_close'])
        assert m.status == Matching.Status.REJECTED

    def test_llm_failure_lands_as_errored(self, world):
        with patch('core.matching.llm_judge', side_effect=RuntimeError('OpenAI down')):
            counters = run_matching_for_offer(world['sch_offer'], k=1)
        assert counters['errored'] >= 1
        m = Matching.objects.get(offer=world['sch_offer'], competing_offer=world['rp_close'])
        assert m.status == Matching.Status.ERRORED


# ---- Tension B critical regression --------------------------------------


@pytest.mark.django_db
class TestSkipExistingMatchings:
    """The whole protection of human-confirmed matches depends on this skip-if-
    exists invariant. If this test ever regresses, AI re-runs will silently
    overwrite weeks of human review work (eng review TODO #4)."""

    def test_existing_confirmed_match_is_not_overwritten(self, world):
        Matching.objects.create(
            offer=world['sch_offer'], competing_offer=world['rp_close'],
            status=Matching.Status.CONFIRMED,
            source=Matching.Source.HUMAN_CONFIRMED,
            llm_reason='manually confirmed',
        )
        decision = MatchDecision(decision='NO', confidence=0.99, reason='Different products')
        with patch('core.matching.llm_judge', return_value=decision) as m:
            run_matching_for_offer(world['sch_offer'], k=2)
        # llm_judge should NOT have been called for the (sch_offer, rp_close) pair
        # (it might have been for rp_far if it's in the candidate set)
        existing = Matching.objects.get(offer=world['sch_offer'], competing_offer=world['rp_close'])
        assert existing.status == Matching.Status.CONFIRMED
        assert existing.source == Matching.Source.HUMAN_CONFIRMED
        assert existing.llm_reason == 'manually confirmed'

    def test_existing_rejected_match_is_not_re_suggested(self, world):
        Matching.objects.create(
            offer=world['sch_offer'], competing_offer=world['rp_close'],
            status=Matching.Status.REJECTED,
            source=Matching.Source.HUMAN_REJECTED,
            llm_reason='different sizes',
        )
        decision = MatchDecision(decision='YES', confidence=0.99, reason='Same product')
        with patch('core.matching.llm_judge', return_value=decision):
            run_matching_for_offer(world['sch_offer'], k=2)
        existing = Matching.objects.get(offer=world['sch_offer'], competing_offer=world['rp_close'])
        assert existing.status == Matching.Status.REJECTED
        assert existing.source == Matching.Source.HUMAN_REJECTED

    def test_existing_match_in_reverse_direction_also_skipped(self, world):
        """If Matching exists with (rp_close as offer, sch_offer as competing),
        re-running for sch_offer must still skip the pair to preserve the human
        decision regardless of direction."""
        Matching.objects.create(
            offer=world['rp_close'], competing_offer=world['sch_offer'],
            status=Matching.Status.CONFIRMED,
            source=Matching.Source.HUMAN_CONFIRMED,
        )
        decision = MatchDecision(decision='NO', confidence=0.99, reason='different')
        with patch('core.matching.llm_judge', return_value=decision):
            counters = run_matching_for_offer(world['sch_offer'], k=2)
        # rp_close was skipped — but rp_far might have been processed
        assert Matching.objects.filter(
            offer=world['sch_offer'], competing_offer=world['rp_close']
        ).count() == 0  # not created in the forward direction
        # Counter shows the skip
        assert counters['skipped_existing'] >= 1

    def test_re_run_is_idempotent(self, world):
        """Running the pipeline twice produces no additional Matching rows for
        the same pair."""
        decision = MatchDecision(decision='YES', confidence=0.9, reason='Same product')
        with patch('core.matching.llm_judge', return_value=decision):
            run_matching_for_offer(world['sch_offer'], k=2)
        first_count = Matching.objects.count()
        with patch('core.matching.llm_judge', return_value=decision):
            run_matching_for_offer(world['sch_offer'], k=2)
        assert Matching.objects.count() == first_count
