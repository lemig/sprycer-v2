"""Scraper registry: host → parser callable + retailer/channel bootstrap."""
from dataclasses import dataclass
from typing import Callable

from . import geant, rougier


@dataclass(frozen=True)
class ScraperSpec:
    """Per-host scraper config.

    vat_rate: the tax rate baked into the site's displayed price. The runner
    divides parsed TTC cents by (1 + vat_rate) to land HT cents in
    PriceObservation, matching the legacy parsers exactly:

      - Géant BE  -> 0.21  (Belgian VAT 21%; legacy: ttc * 100 / 121)
      - Géant FR  -> 0.20  (French VAT 20%; legacy: ttc * 100 / 120)
      - R&P       -> 0.20  (legacy: ttc / 120 * 100)
      - A site that already shows HT would set vat_rate=0.
    """
    parse: Callable
    retailer_name: str
    channel_name: str
    website_host: str
    vat_rate: float = 0.0


REGISTRY: dict[str, ScraperSpec] = {
    'www.geant-beaux-arts.be': ScraperSpec(
        parse=geant.parse_jsonld,
        retailer_name='Le Géant des Beaux-Arts (BE)',
        channel_name='www.geant-beaux-arts.be',
        website_host='www.geant-beaux-arts.be',
        vat_rate=0.21,
    ),
    'www.geant-beaux-arts.fr': ScraperSpec(
        parse=geant.parse_jsonld,
        retailer_name='Le Géant des Beaux-Arts (FR)',
        channel_name='www.geant-beaux-arts.fr',
        website_host='www.geant-beaux-arts.fr',
        vat_rate=0.20,
    ),
    'www.rougier-ple.fr': ScraperSpec(
        parse=rougier.parse,
        retailer_name='Rougier & Plé',
        channel_name='rougier-ple.fr',
        website_host='www.rougier-ple.fr',
        vat_rate=0.20,
    ),
}


def get_spec(host: str) -> ScraperSpec | None:
    return REGISTRY.get(host)
