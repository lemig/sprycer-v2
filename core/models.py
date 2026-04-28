"""
Sprycer v2 data model.

Mirrors the legacy Rails schema (../sprycer/db/schema.rb) with these deliberate
amendments per the 2026-04-26 eng review:

  - Single Offer table with retailer_id discriminator + self-referential Matching
    (1A: replaces PLAN's Product/CompetitorProduct split)
  - Append-only PriceObservation table (Tension C: replaces single-row PricePoint;
    enables last-good-price fallback when a scrape partially fails)
  - Matching.source tracks who created/last-touched the row so AI re-runs do not
    overwrite human corrections (1E + Tension B)
  - Brand.aliases preserved for cross-retailer name normalization
  - MainCompetition.position drives the dynamic 'Competitor N' export columns
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone

from pgvector.django import HnswIndex, VectorField


# ---- Reference data --------------------------------------------------------


class Retailer(models.Model):
    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class Brand(models.Model):
    name = models.CharField(max_length=255, unique=True)
    aliases = ArrayField(models.CharField(max_length=255), default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name

    @classmethod
    def find_or_create_by_name_or_alias(cls, name: str) -> 'Brand':
        if not name:
            raise ValueError('name is required')
        existing = cls.objects.filter(
            models.Q(name__iexact=name) | models.Q(aliases__contains=[name])
        ).first()
        return existing or cls.objects.create(name=name)


class Website(models.Model):
    host = models.CharField(max_length=255, unique=True)
    scrapable = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['host']

    def __str__(self) -> str:
        return self.host


class Channel(models.Model):
    # Production examples: 'schleiper.com/eshopexpress',
    # 'schleiper.com/onlinecatalogue', 'rougier-ple.fr', 'www.geant-beaux-arts.be'.
    name = models.CharField(max_length=255, unique=True)
    website = models.ForeignKey(
        Website, on_delete=models.PROTECT, related_name='channels', null=True, blank=True
    )
    retailer = models.ForeignKey(Retailer, on_delete=models.PROTECT, related_name='channels')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self) -> str:
        return self.name


class MainCompetition(models.Model):
    # Drives 'Competitor N' column ordering in the export. Position is gap-friendly:
    # legacy supports (1, 3, 5) without auto-renumber; preserve that semantic.
    retailer = models.ForeignKey(
        Retailer, on_delete=models.CASCADE, related_name='main_competitions'
    )
    competitor = models.ForeignKey(
        Retailer, on_delete=models.CASCADE, related_name='ranked_in_main_competitions'
    )
    position = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['retailer_id', 'position']
        constraints = [
            models.UniqueConstraint(
                fields=['retailer', 'position'], name='uniq_main_comp_retailer_position'
            ),
            models.UniqueConstraint(
                fields=['retailer', 'competitor'], name='uniq_main_comp_retailer_competitor'
            ),
        ]

    def __str__(self) -> str:
        return f'{self.retailer} #{self.position} = {self.competitor}'


# ---- URL queue (legacy "pages" doubles as scrape queue) -------------------


class Page(models.Model):
    # Walk the queue with: Page.objects.filter(scraped_at__lt=cutoff).order_by('scraped_at').
    # Per H8 simplification (eng review 1J): no separate ScrapeQueue table.
    website = models.ForeignKey(Website, on_delete=models.CASCADE, related_name='pages')
    url = models.CharField(max_length=2048, unique=True)
    scraped_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status_code = models.IntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True, default='')
    consecutive_failures = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['url']

    def __str__(self) -> str:
        return self.url


# ---- The core: Offer + Matching -------------------------------------------


class Offer(models.Model):
    # ONE table for both Schleiper and competitor offers. retailer_id discriminates.
    # Matching is self-referential via competing_offers (see Matching below).
    # Unique key (website, sku, public) mirrors legacy index.
    website = models.ForeignKey(
        Website, on_delete=models.PROTECT, related_name='offers', null=True, blank=True
    )
    channel = models.ForeignKey(Channel, on_delete=models.PROTECT, related_name='offers')
    retailer = models.ForeignKey(Retailer, on_delete=models.PROTECT, related_name='offers')
    brand = models.ForeignKey(
        Brand, on_delete=models.SET_NULL, related_name='offers', null=True, blank=True
    )
    pages = models.ManyToManyField(Page, related_name='offers', blank=True)

    sku = models.CharField(max_length=512)
    common_sku = models.CharField(max_length=512, blank=True, default='')
    name = models.CharField(max_length=2048)
    description = models.TextField(blank=True, default='')
    ean = models.CharField(max_length=64, blank=True, default='')
    original_image_url = models.URLField(max_length=2048, blank=True, default='')
    categories = ArrayField(models.CharField(max_length=255), default=list, blank=True)
    custom_attributes = models.JSONField(default=dict, blank=True)

    public = models.BooleanField(default=False)
    matchings_reviewed_at = models.DateTimeField(null=True, blank=True)

    # Embedding for AI matching pipeline. NULL until first embed pass.
    embedding = VectorField(dimensions=1536, null=True, blank=True)
    embedding_input_hash = models.CharField(max_length=64, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['website', 'sku', 'public'], name='uniq_offer_website_sku_public'
            ),
        ]
        indexes = [
            models.Index(fields=['retailer', 'public']),
            models.Index(fields=['channel']),
            HnswIndex(
                name='offer_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

    def __str__(self) -> str:
        return f'[{self.retailer}] {self.sku} — {self.name[:60]}'


class Matching(models.Model):
    # Self-referential. status mirrors legacy enum (suggested/confirmed/rejected)
    # plus 'errored' for LLM parse failures. source tracks provenance so AI re-runs
    # preserve human work (Tension B regression risk).
    class Status(models.TextChoices):
        SUGGESTED = 'suggested', 'Suggested'
        CONFIRMED = 'confirmed', 'Confirmed'
        REJECTED = 'rejected', 'Rejected'
        ERRORED = 'errored', 'Errored'

    class Source(models.TextChoices):
        LEGACY_IMPORTED = 'legacy_imported', 'Legacy Imported'
        AI_SUGGESTED = 'ai_suggested', 'AI Suggested'
        HUMAN_CONFIRMED = 'human_confirmed', 'Human Confirmed'
        HUMAN_REJECTED = 'human_rejected', 'Human Rejected'

    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name='matchings')
    competing_offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name='matchings_as_competing_offer'
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUGGESTED)
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.AI_SUGGESTED)
    score = models.FloatField(null=True, blank=True)
    predicted = models.BooleanField(default=False)
    llm_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['offer', 'competing_offer'], name='uniq_matching_offer_competing'
            ),
        ]
        indexes = [
            models.Index(fields=['competing_offer']),
            models.Index(fields=['status']),
        ]

    def __str__(self) -> str:
        return f'{self.offer_id} ↔ {self.competing_offer_id} ({self.status})'

    @property
    def is_identical(self) -> bool:
        return self.offer_id == self.competing_offer_id


# ---- Prices: append-only observation log ----------------------------------


class PriceObservation(models.Model):
    # One row per scrape event per offer. Append-only. Export queries select latest
    # non-null per offer so a partial scrape leaves prior prices visible (Tension C).
    # Live operational table is pruned to 30 days nightly. The 12-month historical
    # backfill from paper_trail.versions (TODO #7) lands here too.
    offer = models.ForeignKey(
        Offer, on_delete=models.CASCADE, related_name='price_observations'
    )
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)
    price_cents = models.IntegerField()
    list_price_cents = models.IntegerField(null=True, blank=True)
    shipping_charges_cents = models.IntegerField(null=True, blank=True)
    price_currency = models.CharField(max_length=3, default='EUR')

    class Meta:
        indexes = [
            models.Index(fields=['offer', '-observed_at'], name='price_obs_offer_recent_idx'),
        ]
        constraints = [
            # Gates migrate_legacy rerun (nightly sync during parallel run)
            # and any future write path from appending duplicate rows.
            models.UniqueConstraint(
                fields=['offer', 'observed_at', 'price_cents'],
                name='unique_price_observation',
            ),
        ]

    def __str__(self) -> str:
        cents = self.price_cents / 100
        return f'offer={self.offer_id} {cents:.2f} {self.price_currency} @ {self.observed_at:%Y-%m-%d}'


# ---- Reviews drive the export "Reviewed" column ---------------------------


class Review(models.Model):
    # Powers the "Main competitors offers reviewed" / "Some competitors offers
    # reviewed" / "Competitors offers not yet reviewed" cell in the export.
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name='reviews')
    retailer = models.ForeignKey(Retailer, on_delete=models.CASCADE, related_name='reviews')
    competitor = models.ForeignKey(
        Retailer, on_delete=models.CASCADE, related_name='reviews_as_competitor'
    )
    reviewed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['offer', 'retailer', 'competitor'], name='uniq_review_offer_ret_comp'
            ),
        ]
        indexes = [
            models.Index(fields=['offer', 'retailer'], name='review_offer_retailer_idx'),
        ]

    def __str__(self) -> str:
        return (
            f'offer={self.offer_id} {self.retailer}→{self.competitor} '
            f'@ {self.reviewed_at:%Y-%m-%d}'
        )


# ---- Import / Export job records ------------------------------------------


class Import(models.Model):
    class Status(models.TextChoices):
        UNPROCESSED = 'Unprocessed', 'Unprocessed'
        ENQUEUED = 'Enqueued', 'Enqueued'
        IMPORTING = 'Importing', 'Importing'
        ERROR = 'Error', 'Error'
        CANCELLED = 'Cancelled', 'Cancelled'
        COMPLETED = 'Completed', 'Completed'
        SUCCESS = 'Success', 'Success'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='imports'
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNPROCESSED)
    total = models.PositiveIntegerField(default=0)
    pending = models.PositiveIntegerField(default=0)
    failures = models.PositiveIntegerField(default=0)
    failure_info = ArrayField(models.TextField(), default=list, blank=True)

    importer_class_name = models.CharField(max_length=255, default='SchleiperImporter')
    file = models.FileField(upload_to='imports/%Y/%m/')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'Import #{self.pk} {self.status} ({self.total} rows)'


class Export(models.Model):
    class Model(models.TextChoices):
        OFFER = 'Offer', 'Offer'
        MATCHING = 'Matching', 'Matching'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='exports'
    )
    model = models.CharField(max_length=16, choices=Model.choices, default=Model.OFFER)
    count = models.PositiveIntegerField(default=0)
    file = models.FileField(upload_to='exports/%Y/%m/', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'Export #{self.pk} {self.model} ({self.count} rows)'
