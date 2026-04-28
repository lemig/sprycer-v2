"""Tests for the embedding pipeline (H11).

OpenAI calls are mocked at the embed_texts boundary so the suite stays
network-free + deterministic.
"""
from unittest.mock import patch

import pytest
from django.test import override_settings

from core.embeddings import (
    compute_embedding_hash,
    embed_offer,
    embed_offers_bulk,
    is_enabled,
)
from core.models import Channel, Offer, Retailer, Website


@pytest.fixture
def offer(db):
    r = Retailer.objects.create(name='Schleiper')
    w = Website.objects.create(host='www.schleiper.com')
    c = Channel.objects.create(name='schleiper.com/eshopexpress', retailer=r, website=w)
    return Offer.objects.create(
        retailer=r, channel=c, website=w,
        sku='X1', name='Item One', description='A nice item.', public=True,
    )


def _fake_vec(seed: int = 0):
    """Deterministic vector helper. Real vectors are 1536-dim; using small ones
    is fine for tests (we never actually query pgvector here)."""
    return [float(seed + i) / 100 for i in range(1536)]


# ---- Pure helpers --------------------------------------------------------


class TestComputeHash:
    def test_changes_when_name_changes(self):
        a = compute_embedding_hash('A', 'desc')
        b = compute_embedding_hash('B', 'desc')
        assert a != b

    def test_changes_when_description_changes(self):
        a = compute_embedding_hash('A', 'desc1')
        b = compute_embedding_hash('A', 'desc2')
        assert a != b

    def test_stable_for_same_input(self):
        assert compute_embedding_hash('X', 'Y') == compute_embedding_hash('X', 'Y')


# ---- is_enabled gate ----------------------------------------------------


class TestIsEnabled:
    @override_settings(OPENAI_API_KEY='')
    def test_disabled_when_no_key(self):
        assert is_enabled() is False

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_enabled_when_key_set(self):
        assert is_enabled() is True


# ---- embed_offer behavior ------------------------------------------------


@pytest.mark.django_db
class TestEmbedOffer:
    @override_settings(OPENAI_API_KEY='')
    def test_no_op_when_disabled(self, offer):
        assert embed_offer(offer) is False
        offer.refresh_from_db()
        assert offer.embedding is None
        assert offer.embedding_input_hash == ''

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_first_call_writes_embedding_and_hash(self, offer):
        with patch('core.embeddings.embed_texts', return_value=[_fake_vec(1)]) as m:
            assert embed_offer(offer) is True
        offer.refresh_from_db()
        assert offer.embedding is not None
        assert offer.embedding_input_hash == compute_embedding_hash(offer.name, offer.description)
        assert m.call_count == 1

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_no_op_when_hash_matches_existing(self, offer):
        with patch('core.embeddings.embed_texts', return_value=[_fake_vec(1)]):
            embed_offer(offer)
        # Second call with same name/description: no re-embed
        with patch('core.embeddings.embed_texts') as m:
            assert embed_offer(offer) is True
            assert m.call_count == 0

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_re_embeds_when_name_changes(self, offer):
        with patch('core.embeddings.embed_texts', return_value=[_fake_vec(1)]):
            embed_offer(offer)
        offer.name = 'New Name'
        offer.save()
        with patch('core.embeddings.embed_texts', return_value=[_fake_vec(2)]) as m:
            embed_offer(offer)
            assert m.call_count == 1

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_returns_false_on_api_failure(self, offer):
        def boom(*args, **kwargs):
            raise RuntimeError('OpenAI down')
        with patch('core.embeddings.embed_texts', side_effect=boom):
            assert embed_offer(offer) is False
        offer.refresh_from_db()
        # Embedding stayed NULL — backfill cron will retry
        assert offer.embedding is None
        assert offer.embedding_input_hash == ''


# ---- Bulk backfill -------------------------------------------------------


@pytest.mark.django_db
class TestEmbedOffersBulk:
    @override_settings(OPENAI_API_KEY='sk-test')
    def test_chunked_call(self, db):
        r = Retailer.objects.create(name='R')
        w = Website.objects.create(host='r.com')
        c = Channel.objects.create(name='r.com', retailer=r, website=w)
        offers = [
            Offer.objects.create(retailer=r, channel=c, website=w,
                                 sku=f'SKU{i}', name=f'Name {i}', public=True)
            for i in range(7)
        ]

        # chunk_size=3 => 3 batches: [3, 3, 1]
        with patch('core.embeddings.embed_texts',
                   side_effect=lambda texts, **kw: [_fake_vec(i) for i in range(len(texts))]) as m:
            counters = embed_offers_bulk(offers, chunk_size=3)
        assert counters['embedded'] == 7
        assert counters['failed'] == 0
        assert counters['skipped_unchanged'] == 0
        assert m.call_count == 3

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_skips_unchanged(self, offer):
        # Pre-set the hash + embedding so we look "current"
        offer.embedding_input_hash = compute_embedding_hash(offer.name, offer.description)
        offer.embedding = _fake_vec(0)
        offer.save()

        with patch('core.embeddings.embed_texts') as m:
            counters = embed_offers_bulk([offer])
        assert counters['skipped_unchanged'] == 1
        assert counters['embedded'] == 0
        assert m.call_count == 0

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_chunk_failure_marks_failed_and_continues(self, db):
        r = Retailer.objects.create(name='R')
        w = Website.objects.create(host='r.com')
        c = Channel.objects.create(name='r.com', retailer=r, website=w)
        offers = [
            Offer.objects.create(retailer=r, channel=c, website=w,
                                 sku=f'SKU{i}', name=f'Name {i}', public=True)
            for i in range(4)
        ]
        # Both chunks (size=2) fail: counters reflect 4 failed, 0 embedded
        with patch('core.embeddings.embed_texts', side_effect=RuntimeError('OpenAI down')):
            counters = embed_offers_bulk(offers, chunk_size=2)
        assert counters['embedded'] == 0
        assert counters['failed'] == 4
        for o in offers:
            o.refresh_from_db()
            assert o.embedding is None
