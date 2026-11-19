"""Catalogo ligero de municipios y colegios para formularios operativos."""
from functools import lru_cache
from pathlib import Path
import re
import unicodedata


# Archivo Markdown que sirve de fuente del catalogo (queda junto a este modulo).
CATALOG_FILENAME = "levantamiento_colegios_casanare.md"
# Una seccion de municipio es una linea "## Nombre"; un colegio es "1. Nombre".
SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
ITEM_PATTERN = re.compile(r"^\d+\.\s+(.+?)\s*$")
# Secciones del Markdown que no son municipios y no deben listarse.
IGNORED_SECTIONS = {
    "Resumen Ejecutivo",
    "Alcance y criterio",
    "Priorizacion sugerida para arranque comercial de rutas",
    "Siguiente paso recomendado",
    "Fuentes base",
}

# Municipios de Casanare usados como base de los formularios.
CASANARE_MUNICIPALITIES = [
    "Aguazul",
    "Chameza",
    "Hato Corozal",
    "La Salina",
    "Mani",
    "Monterrey",
    "Nunchia",
    "Orocue",
    "Paz de Ariporo",
    "Pore",
    "Recetor",
    "Sabanalarga",
    "Sacama",
    "San Luis de Palenque",
    "Tamara",
    "Tauramena",
    "Trinidad",
    "Villanueva",
    "Yopal",
]


def _catalog_path() -> Path:
    """Ruta del archivo Markdown del catalogo (en la misma carpeta del modulo)."""
    return Path(__file__).resolve().with_name(CATALOG_FILENAME)


def _normalize(value: str) -> str:
    """Quita tildes y pasa a minusculas para comparar nombres sin depender de acentos."""
    normalized = unicodedata.normalize("NFD", value or "")
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn").strip().lower()


# Version normalizada de las secciones ignoradas, lista para comparar.
NORMALIZED_IGNORED_SECTIONS = {_normalize(item) for item in IGNORED_SECTIONS}


@lru_cache(maxsize=1)
def load_school_catalog():
    """Lee el levantamiento base y devuelve colegios agrupados por municipio."""
    path = _catalog_path()
    if not path.exists():
        return {}

    catalog = {}
    current_city = None

    # Se recorre el Markdown linea por linea: las secciones "##" abren el
    # municipio actual y los items numerados se agregan a ese municipio.
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
    """Entrega el listado completo de municipios de Casanare para la UI."""
    municipalities_by_key = {
        _normalize(municipio): municipio for municipio in CASANARE_MUNICIPALITIES
    }
    for municipio in load_school_catalog().keys():
        municipalities_by_key[_normalize(municipio)] = municipio
    return sorted(municipalities_by_key.values(), key=str.casefold)
