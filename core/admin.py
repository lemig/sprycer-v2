"""Django admin registrations.

List displays mirror the legacy /imports and /exports columns visible in
production screenshots so the admin alone is usable for cutover-day operations.
A custom Sprycer-red layout for /imports, /exports, /matchings is added in H16.
"""
from django.contrib import admin

from .models import (
    Brand,
    Channel,
    Export,
    Import,
    MainCompetition,
    Matching,
    Offer,
    Page,
    PriceObservation,
    Retailer,
    Review,
    Website,
)


@admin.register(Retailer)
class RetailerAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')
    search_fields = ('name',)


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'aliases', 'created_at')
    search_fields = ('name', 'aliases')


@admin.register(Website)
class WebsiteAdmin(admin.ModelAdmin):
    list_display = ('host', 'scrapable', 'created_at')
    list_filter = ('scrapable',)
    search_fields = ('host',)


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ('name', 'retailer', 'website', 'created_at')
    list_filter = ('retailer',)
    search_fields = ('name',)


@admin.register(MainCompetition)
class MainCompetitionAdmin(admin.ModelAdmin):
    list_display = ('retailer', 'position', 'competitor')
    list_filter = ('retailer',)
    ordering = ('retailer', 'position')


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ('url', 'website', 'scraped_at', 'last_status_code', 'consecutive_failures')
    list_filter = ('website', 'last_status_code')
    search_fields = ('url',)
    ordering = ('-scraped_at',)


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('id', 'sku', 'retailer', 'channel', 'brand', 'name', 'public', 'updated_at')
    list_filter = ('retailer', 'channel', 'public', 'brand')
    search_fields = ('sku', 'common_sku', 'name', 'ean')
    raw_id_fields = ('brand', 'channel', 'website', 'pages')
    readonly_fields = ('created_at', 'updated_at', 'embedding_input_hash')
    ordering = ('-updated_at',)


@admin.register(Matching)
class MatchingAdmin(admin.ModelAdmin):
    list_display = ('id', 'offer', 'competing_offer', 'status', 'source', 'score', 'updated_at')
    list_filter = ('status', 'source')
    search_fields = ('offer__sku', 'competing_offer__sku')
    raw_id_fields = ('offer', 'competing_offer')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(PriceObservation)
class PriceObservationAdmin(admin.ModelAdmin):
    list_display = ('offer', 'observed_at', 'price_cents', 'price_currency')
    list_filter = ('price_currency',)
    raw_id_fields = ('offer',)
    ordering = ('-observed_at',)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('offer', 'retailer', 'competitor', 'reviewed_at')
    list_filter = ('retailer', 'competitor')
    raw_id_fields = ('offer',)
    ordering = ('-reviewed_at',)


@admin.register(Import)
class ImportAdmin(admin.ModelAdmin):
    # Columns mirror legacy /imports screenshot: ID, User, Model (= importer),
    # Created at, Status, Total, Pending, Failures.
    list_display = ('id', 'user', 'importer_class_name', 'created_at', 'status',
                    'total', 'pending', 'failures')
    list_filter = ('status', 'importer_class_name')
    readonly_fields = ('created_at', 'updated_at', 'failure_info')
    ordering = ('-created_at',)


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    # Columns mirror legacy /exports screenshot: ID, User, Model, Created at,
    # Records (= count), File.
    list_display = ('id', 'user', 'model', 'created_at', 'count', 'file')
    list_filter = ('model',)
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
