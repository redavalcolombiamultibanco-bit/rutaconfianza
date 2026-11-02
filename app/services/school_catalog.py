"""Catalogo ligero de municipios y colegios para formularios operativos."""
from functools import lru_cache
from pathlib import Path
import re
import unicodedata


CATALOG_FILENAME = "levantamiento_colegios_casanare.md"
SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
ITEM_PATTERN = re.compile(r"^\d+\.\s+(.+?)\s*$")
IGNORED_SECTIONS = {
    "Resumen Ejecutivo",
    "Alcance y criterio",
    "Priorizacion sugerida para arranque comercial de rutas",
    "Siguiente paso recomendado",
    "Fuentes base",
}


def _catalog_path() -> Path:
    return Path(__file__).resolve().parents[4] / CATALOG_FILENAME


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn").strip().lower()


NORMALIZED_IGNORED_SECTIONS = {_normalize(item) for item in IGNORED_SECTIONS}


@lru_cache(maxsize=1)
def load_school_catalog():
    """Lee el levantamiento base y devuelve colegios agrupados por municipio."""
    path = _catalog_path()
    if not path.exists():
        return {}

    catalog = {}
    current_city = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        section_match = SECTION_PATTERN.match(line)
        if section_match:
            section_name = section_match.group(1).strip()
            normalized_section = _normalize(section_name)
            if normalized_section in NORMALIZED_IGNORED_SECTIONS or normalized_section.startswith("nota operativa"):
                current_city = None
            else:
                current_city = section_name
                catalog.setdefault(current_city, [])
            continue

        if current_city is None:
            continue

        item_match = ITEM_PATTERN.match(line)
        if item_match:
            school_name = item_match.group(1).strip()
            if school_name and school_name not in catalog[current_city]:
                catalog[current_city].append(school_name)

    return catalog


def get_municipalities():
    """Entrega los municipios disponibles ordenados para sugerencias UI."""
    return sorted(load_school_catalog().keys(), key=str.casefold)
