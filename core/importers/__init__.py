"""Importer registry. New importers register their class via REGISTRY below."""
from .schleiper import SchleiperImporter

REGISTRY = {
    SchleiperImporter.name: SchleiperImporter,
}


def get_importer(class_name: str):
    try:
        return REGISTRY[class_name]
    except KeyError:
        raise ValueError(
            f'Unknown importer {class_name!r}. Registered: {sorted(REGISTRY)}'
        )
