"""Django admin registrations.

List displays mirror the legacy /imports and /exports columns visible in
production screenshots so the admin alone is usable for cutover-day operations.
A custom Sprycer-red layout for /imports, /exports, /matchings is added in H16
(eng review Tension A).
"""
from django.contrib import admin
from django.db.models import OuterRef, Subquery
from django.utils.html import format_html

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
from .money import format_euro


# ---- Inlines --------------------------------------------------------------


class MainCompetitionInline(admin.TabularInline):
    """Manage a retailer's ordered competitor list inline. Drives the dynamic
    'Competitor N' export columns, so Miguel needs to ship cutover with the
    right ordering for Schleiper (1 = Géant FR, 2 = Rougier & Plé, 3 = Géant BE
    per the user-provided exports.csv)."""
    model = MainCompetition
    fk_name = 'retailer'
    fields = ('position', 'competitor')
    extra = 1
    ordering = ('position',)


class OfferPriceObservationInline(admin.TabularInline):
    """Read-only view of recent prices for this offer. Operational sanity."""
    model = PriceObservation
    fields = ('observed_at', 'formatted_price', 'price_cents', 'list_price_cents',
              'shipping_charges_cents', 'price_currency')
    readonly_fields = ('observed_at', 'formatted_price', 'price_cents', 'list_price_cents',
                       'shipping_charges_cents', 'price_currency')
    extra = 0
    can_delete = False
    show_change_link = False
    ordering = ('-observed_at',)
    verbose_name = 'Recent price'
    verbose_name_plural = 'Recent prices (newest first)'

    def has_add_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        # Cap to most recent 10 to keep the form snappy on offers with months
        # of history (post-12-month-backfill at H17).
        qs = super().get_queryset(request).order_by('-observed_at')
        return qs

    @admin.display(description='Price')
    def formatted_price(self, obj):
        return format_euro(obj.price_cents)


class OfferMatchingInline(admin.TabularInline):
    """Read-only view of current matchings (this offer's competitors)."""
    model = Matching
    fk_name = 'offer'
    fields = ('competing_offer', 'status', 'source', 'score', 'updated_at')
    readonly_fields = ('competing_offer', 'status', 'source', 'score', 'updated_at')
    extra = 0
    can_delete = False
    show_change_link = True
    verbose_name = 'Matching (this offer → competing)'
    verbose_name_plural = 'Matchings (this offer → competing)'

    def has_add_permission(self, request, obj=None):
        return False


# ---- Reference data --------------------------------------------------------


@admin.register(Retailer)
class RetailerAdmin(admin.ModelAdmin):
    list_display = ('name', 'main_competitor_count', 'created_at')
    search_fields = ('name',)
    inlines = [MainCompetitionInline]

    @admin.display(description='# main competitors')
    def main_competitor_count(self, obj):
        return obj.main_competitions.count()


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


# ---- The core: Offer + Matching -------------------------------------------


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ('id', 'sku', 'retailer', 'channel', 'brand', 'name',
                    'public', 'latest_price', 'updated_at')
    list_filter = ('retailer', 'channel', 'public', 'brand')
    list_select_related = ('retailer', 'channel', 'brand')
    search_fields = ('sku', 'common_sku', 'name', 'ean', 'description')
    raw_id_fields = ('brand', 'channel', 'website')
    filter_horizontal = ('pages',)
    readonly_fields = ('created_at', 'updated_at', 'embedding_input_hash')
    ordering = ('-updated_at',)
    inlines = [OfferPriceObservationInline, OfferMatchingInline]
    fieldsets = (
        ('Identity', {
            'fields': ('retailer', 'channel', 'sku', 'common_sku', 'brand', 'ean'),
        }),
        ('Content', {
            'fields': ('name', 'description', 'original_image_url', 'categories', 'custom_attributes'),
        }),
        ('URLs', {
            'fields': ('website', 'pages'),
        }),
        ('Status', {
            'fields': ('public', 'matchings_reviewed_at'),
        }),
        ('Embedding', {
            'classes': ('collapse',),
            'fields': ('embedding', 'embedding_input_hash'),
        }),
        ('Metadata', {
            'classes': ('collapse',),
            'fields': ('created_at', 'updated_at'),
        }),
    )

    def get_queryset(self, request):
        # Annotate latest price via correlated subquery so the list view doesn't
        # N+1 when Schleiper has 22K offers (eng review 4A: prefetch/select_related
        # principle applied).
        latest = PriceObservation.objects.filter(offer=OuterRef('pk')).order_by('-observed_at')
        return super().get_queryset(request).annotate(_latest_price_cents=Subquery(latest.values('price_cents')[:1]))

    @admin.display(description='Latest price', ordering='_latest_price_cents')
    def latest_price(self, obj):
        return format_euro(obj._latest_price_cents) or '—'


@admin.register(Matching)
class MatchingAdmin(admin.ModelAdmin):
    list_display = ('id', 'offer_label', 'competing_offer_label', 'status',
                    'source', 'score', 'updated_at')
    list_filter = ('status', 'source')
    list_select_related = ('offer__retailer', 'competing_offer__retailer')
    search_fields = ('offer__sku', 'offer__name', 'competing_offer__sku', 'competing_offer__name')
    raw_id_fields = ('offer', 'competing_offer')
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Offer', ordering='offer__sku')
    def offer_label(self, obj):
        return format_html('<b>{}</b> {}', obj.offer.sku, obj.offer.name[:60])

    @admin.display(description='Competing offer', ordering='competing_offer__sku')
    def competing_offer_label(self, obj):
        return format_html('<b>{}</b> {}', obj.competing_offer.sku, obj.competing_offer.name[:60])


@admin.register(PriceObservation)
class PriceObservationAdmin(admin.ModelAdmin):
    list_display = ('offer', 'observed_at', 'formatted_price', 'price_cents',
                    'list_price_cents', 'price_currency')
    list_filter = ('price_currency',)
    list_select_related = ('offer',)
    raw_id_fields = ('offer',)
    ordering = ('-observed_at',)

    @admin.display(description='Price', ordering='price_cents')
    def formatted_price(self, obj):
        return format_euro(obj.price_cents)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ('offer', 'retailer', 'competitor', 'reviewed_at')
    list_filter = ('retailer', 'competitor')
    list_select_related = ('offer', 'retailer', 'competitor')
    raw_id_fields = ('offer',)
    ordering = ('-reviewed_at',)


# ---- Job records: list_display mirrors legacy /imports + /exports ---------


@admin.register(Import)
class ImportAdmin(admin.ModelAdmin):
    # Columns mirror legacy /imports screenshot: ID, User, Model (= importer),
    # Created at, Status, Total, Pending, Failures.
    list_display = ('id', 'user', 'importer_class_name', 'created_at', 'status',
                    'total', 'pending', 'failures')
    list_filter = ('status', 'importer_class_name')
    list_select_related = ('user',)
    readonly_fields = ('created_at', 'updated_at', 'failure_info')
    ordering = ('-created_at',)


@admin.register(Export)
class ExportAdmin(admin.ModelAdmin):
    # Columns mirror legacy /exports screenshot: ID, User, Model, Created at,
    # Records (= count), File.
    list_display = ('id', 'user', 'model', 'created_at', 'count', 'file')
    list_filter = ('model',)
    list_select_related = ('user',)
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
