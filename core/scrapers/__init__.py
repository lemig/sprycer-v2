"""Scraper registry: host → parser callable + retailer/channel bootstrap."""
from dataclasses import dataclass
from typing import Callable

from . import geant


@dataclass(frozen=True)
class ScraperSpec:
    parse: Callable
    retailer_name: str
    channel_name: str
    website_host: str


REGISTRY: dict[str, ScraperSpec] = {
    'www.geant-beaux-arts.be': ScraperSpec(
        parse=geant.parse_jsonld,
        retailer_name='Le Géant des Beaux-Arts (BE)',
        channel_name='www.geant-beaux-arts.be',
        website_host='www.geant-beaux-arts.be',
    ),
    'www.geant-beaux-arts.fr': ScraperSpec(
        parse=geant.parse_jsonld,
        retailer_name='Le Géant des Beaux-Arts (FR)',
        channel_name='www.geant-beaux-arts.fr',
        website_host='www.geant-beaux-arts.fr',
    ),
    # Rougier & Plé scraper lands in H7 (microdata per eng review 1D)
}


def get_spec(host: str) -> ScraperSpec | None:
    return REGISTRY.get(host)
