"""Servicios simples de geocodificacion para direcciones en Colombia."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Iterable, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ARCGIS_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
USER_AGENT = "RutaExacta/1.0 (+https://rutaexacta.local)"
CASANARE_BOUNDS = {
    "min_latitude": 4.15,
    "max_latitude": 6.45,
    "min_longitude": -73.25,
    "max_longitude": -69.65,
}


def geocode_colombia_address(*parts: str) -> Optional[dict]:
    """Convierte una direccion libre en coordenadas.

    Prioriza las coincidencias con direccion concreta (calle o punto) y solo
    acepta un resultado aproximado (por ejemplo, el centro del municipio) como
    ultimo recurso. Asi una direccion nunca termina ubicada en otro municipio.
    """
    cleaned_parts = _clean_parts(parts)
    if not cleaned_parts:
        return None

    arcgis_result = _find_best_result_arcgis(cleaned_parts)
    if arcgis_result and not _is_approximate_result(arcgis_result):
        return _build_geodata(arcgis_result)

    best_result = _find_best_result(cleaned_parts)
    grid_fallback = _geocode_grid_address(cleaned_parts)
    if grid_fallback and _should_prefer_grid_fallback(cleaned_parts, best_result):
        return grid_fallback
    if best_result:
        return _build_geodata(best_result)

    intersection_result = _geocode_intersection(cleaned_parts)
    if intersection_result:
        return intersection_result

    # Ultimo recurso: se acepta el resultado aproximado si pertenece al municipio pedido.
    if arcgis_result:
        return _build_geodata(arcgis_result)
    return None


def _clean_parts(parts: Iterable[str]) -> list[str]:
    cleaned = []
    for part in parts:
        if not part or not str(part).strip():
            continue
        normalized = _normalize_text(part)
        if normalized:
            cleaned.append(normalized)
    return cleaned


# Partes del contexto que describen el departamento o el pais, no el municipio.
_REGION_TOKENS = {"colombia", "casanare"}


def _requested_city(parts: list[str]) -> str:
    """Devuelve el municipio, priorizando el contexto mas cercano al pais."""
    for part in reversed(parts[1:]):
        normalized = _normalize_text(part)
        if normalized and normalized not in _REGION_TOKENS:
            return normalized
    return ""


def _is_within_casanare(result: dict) -> bool:
    """Descarta geocodificaciones que claramente cayeron fuera de Casanare."""
    try:
        if isinstance(result.get("location"), dict):
            latitude = float(result["location"]["y"])
            longitude = float(result["location"]["x"])
        else:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return False

    return (
        CASANARE_BOUNDS["min_latitude"] <= latitude <= CASANARE_BOUNDS["max_latitude"]
        and CASANARE_BOUNDS["min_longitude"] <= longitude <= CASANARE_BOUNDS["max_longitude"]
    )


def _requires_casanare_bounds(parts: list[str]) -> bool:
    return any(_normalize_text(part) == "casanare" for part in parts[1:])


def _has_street_level(value: str) -> bool:
    """True si el texto pedido apunta a una direccion concreta (calle o numero)."""
    return "#" in value or _looks_like_road(value)


def _is_approximate_result(result: dict) -> bool:
    """True si el resultado solo apunta al municipio y no a una direccion exacta."""
    attributes = result.get("attributes") if isinstance(result.get("attributes"), dict) else {}
    addr_type = _normalize_text(attributes.get("Addr_type") or "")
    return addr_type in {"locality", "postal", "admin", "subregion", "region"}


def _build_candidate_queries(parts: list[str]) -> list[str]:
    queries = []
    road = _normalize_road_text(parts[0])
    road_spaced = _expand_numeric_letter_spacing(road)
    road_without_details = _strip_secondary_references(road)
    road_without_house_number = _strip_house_number(road_without_details)
    context = parts[1:]

    queries.append(", ".join([road, *context]))
    if road_spaced != road:
        queries.append(", ".join([road_spaced, *context]))
    if road_without_details != road:
        queries.append(", ".join([road_without_details, *context]))
    if road_without_house_number and road_without_house_number != road_without_details:
        queries.append(", ".join([road_without_house_number, *context]))

    if len(context) >= 2:
        queries.append(", ".join([road_without_details, *context[-2:]]))
        queries.append(", ".join([context[0], road_without_details, *context[1:]]))
    elif context:
        queries.append(", ".join([context[0], road_without_details]))

    intersection_variant = _build_intersection_variant(road_without_details)
    if intersection_variant and intersection_variant != road_without_details:
        queries.append(", ".join([intersection_variant, *context]))

    generic_variant = _build_generic_variant(road_without_details)
    if generic_variant and generic_variant != road_without_details:
        queries.append(", ".join([generic_variant, *context]))

    unique_queries = []
    seen = set()
    for query in queries:
        normalized = query.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_queries.append(query)
    return unique_queries[:7]


def _normalize_road_text(value: str) -> str:
    normalized = _normalize_text(value)
    normalized = re.sub(r"\b(cra|cr|kr|kra|k)\b", "carrera", normalized)
    normalized = re.sub(r"\b(cl|cll|c)\b", "calle", normalized)
    normalized = re.sub(r"\b(av|avda|aven)\b", "avenida", normalized)
    normalized = re.sub(r"\b(tv|tr|transv)\b", "transversal", normalized)
    normalized = re.sub(r"\b(dg|diag)\b", "diagonal", normalized)
    normalized = re.sub(r"\b(nro|no|num|numero)\b", "#", normalized)
    # Convierte la nomenclatura colombiana sin separadores al formato que
    # entienden los geocodificadores: Calle 21 15A 30 -> Calle 21 # 15A-30.
    normalized = re.sub(
        r"\b(calle|carrera|transversal|diagonal|avenida)\s+"
        r"(\d+\s*[a-z]?)\s+(?:#\s*)?(\d+\s*[a-z]?)(?:\s*-\s*|\s+)(\d+)\b",
        r"\1 \2 # \3-\4",
        normalized,
    )
    normalized = re.sub(r"\besquina\s+con\b", "con", normalized)
    normalized = re.sub(r"\s*#\s*", " # ", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = re.sub(r"[;|]+", " ", normalized)
    normalized = re.sub(r"[,]+", ", ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" ,")


def _strip_secondary_references(value: str) -> str:
    return re.sub(
        r"\b(apto|apartamento|interior|int|bloque|torre|casa|manzana|mz|lote|local|barrio|sector|conjunto|urbanizacion)\b.*$",
        "",
        value,
    ).strip(" ,")


def _strip_house_number(value: str) -> str:
    return re.sub(r"\s+#\s*\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?.*$", "", value).strip(" ,")


def _build_intersection_variant(value: str) -> str:
    return re.sub(r"\bcon\b", " y ", value).strip(" ,")


def _build_generic_variant(value: str) -> str:
    generic = re.sub(r"\b(carrera|calle|avenida|transversal|diagonal)\b", "", value)
    generic = re.sub(r"\s+", " ", generic)
    return generic.strip(" ,")


def _expand_numeric_letter_spacing(value: str) -> str:
    return re.sub(r"\b(\d+)([a-z])\b", r"\1 \2", value)


def _find_best_result(parts: list[str]) -> Optional[dict]:
    queries = _build_candidate_queries(parts)
    best_result = None
    best_score = float("-inf")

    for query in queries:
        results = _search_nominatim(query)
        for result in results:
            if _requires_casanare_bounds(parts) and not _is_within_casanare(result):
                continue
            score = _score_result(result, parts)
            if score > best_score:
                best_result = result
                best_score = score
        if best_score >= 8:
            break
    # Se descartan coincidencias demasiado debiles (por ejemplo, de otro municipio).
    return best_result if best_score >= 4 else None


def _find_best_result_arcgis(parts: list[str]) -> Optional[dict]:
    queries = _build_candidate_queries(parts)
    best_result = None
    best_score = float("-inf")

    for query in queries:
        results = _search_arcgis(query)
        for result in results:
            if _requires_casanare_bounds(parts) and not _is_within_casanare(result):
                continue
            score = _score_arcgis_result(result, parts)
            if score > best_score:
                best_result = result
                best_score = score
        if best_score >= 105:
            break
    return best_result if best_score >= 90 else None


def _build_geodata(result: dict) -> Optional[dict]:
    try:
        if "location" in result:
            latitud = float(result["location"]["y"])
            longitud = float(result["location"]["x"])
            display_name = result.get("address") or result.get("attributes", {}).get("Match_addr")
        else:
            latitud = float(result["lat"])
            longitud = float(result["lon"])
            display_name = result.get("display_name")
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "latitud": latitud,
        "longitud": longitud,
        "display_name": display_name,
    }


def _geocode_intersection(parts: list[str]) -> Optional[dict]:
    roads = _split_intersection(parts[0])
    if not roads:
        return None

    context = parts[1:]
    first_result = _find_best_result([roads[0], *context])
    second_result = _find_best_result([roads[1], *context])
    first_geodata = _build_geodata(first_result) if first_result else None
    second_geodata = _build_geodata(second_result) if second_result else None
    if not first_geodata or not second_geodata:
        return None

    return {
        "latitud": (first_geodata["latitud"] + second_geodata["latitud"]) / 2,
        "longitud": (first_geodata["longitud"] + second_geodata["longitud"]) / 2,
        "display_name": f"{roads[0]} con {roads[1]}",
    }


def _geocode_grid_address(parts: list[str]) -> Optional[dict]:
    parsed = _parse_grid_address(parts[0] if parts else "")
    if not parsed:
        return None

    main_type, main_number, cross_type, cross_number, house_distance = parsed
    base_geodata = _geocode_intersection([
        f"{main_type} {main_number} con {cross_type} {cross_number}",
        *parts[1:],
    ])
    if not base_geodata:
        return None

    if house_distance is None:
        return base_geodata

    next_cross = _increment_road_number(cross_number)
    if not next_cross:
        return base_geodata

    next_geodata = _geocode_intersection([
        f"{main_type} {main_number} con {cross_type} {next_cross}",
        *parts[1:],
    ])
    if not next_geodata:
        previous_cross = _decrement_road_number(cross_number)
        if previous_cross:
            next_geodata = _geocode_intersection([
                f"{main_type} {main_number} con {cross_type} {previous_cross}",
                *parts[1:],
            ])
    if not next_geodata:
        return base_geodata

    fraction = max(0.05, min(house_distance / 100.0, 0.95))
    return {
        "latitud": base_geodata["latitud"] + ((next_geodata["latitud"] - base_geodata["latitud"]) * fraction),
        "longitud": base_geodata["longitud"] + ((next_geodata["longitud"] - base_geodata["longitud"]) * fraction),
        "display_name": f"{main_type} {main_number} # {cross_number}-{house_distance}",
    }


def _parse_grid_address(value: str) -> Optional[tuple[str, str, str, str, Optional[int]]]:
    normalized = _normalize_road_text(value)
    match = re.search(
        r"\b(carrera|calle|transversal|diagonal|avenida)\s+(\d+\s*[a-z]?)\s+#\s*(\d+\s*[a-z]?)(?:\s*-\s*(\d+))?",
        normalized,
    )
    if not match:
        return None

    main_type = match.group(1)
    main_number = re.sub(r"\s+", " ", match.group(2)).strip()
    cross_number = re.sub(r"\s+", " ", match.group(3)).strip()
    house_distance = int(match.group(4)) if match.group(4) else None
    cross_type = "calle" if main_type in {"carrera", "transversal", "avenida"} else "carrera"
    return main_type, main_number, cross_type, cross_number, house_distance


def _increment_road_number(value: str) -> Optional[str]:
    match = re.match(r"^(\d+)(\s*[a-z]?)$", value.strip())
    if not match:
        return None
    return f"{int(match.group(1)) + 1}{match.group(2)}".strip()


def _decrement_road_number(value: str) -> Optional[str]:
    match = re.match(r"^(\d+)(\s*[a-z]?)$", value.strip())
    if not match:
        return None
    number = int(match.group(1)) - 1
    if number <= 0:
        return None
    return f"{number}{match.group(2)}".strip()


def _should_prefer_grid_fallback(parts: list[str], best_result: Optional[dict]) -> bool:
    road = parts[0] if parts else ""
    if "#" not in road:
        return False
    if not best_result:
        return True

    address = best_result.get("address") if isinstance(best_result.get("address"), dict) else {}
    address_house_number = _normalize_text(address.get("house_number") or "")
    return not bool(address_house_number)


def _split_intersection(value: str) -> Optional[tuple[str, str]]:
    normalized = _normalize_road_text(value)
    for separator in (" con ", " y "):
        if separator not in normalized:
            continue
        left, right = normalized.split(separator, 1)
        left = left.strip(" ,")
        right = right.strip(" ,")
        if _looks_like_road(left) and _looks_like_road(right):
            return left, right
    return None


def _looks_like_road(value: str) -> bool:
    return bool(re.search(r"\b(carrera|calle|avenida|transversal|diagonal)\b", value))


def _search_nominatim(query: str) -> list[dict]:
    params = urlencode(
        {
            "q": query,
            "format": "jsonv2",
            "limit": 5,
            "countrycodes": "co",
            "addressdetails": 1,
            "dedupe": 1,
        }
    )
    request = Request(
        f"{NOMINATIM_URL}?{params}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _search_arcgis(query: str) -> list[dict]:
    params = urlencode(
        {
            "f": "json",
            "singleLine": query,
            "countryCode": "COL",
            "maxLocations": 5,
            "outFields": "Match_addr,Addr_type,City,Region,Subregion,CountryCode",
        }
    )
    request = Request(
        f"{ARCGIS_URL}?{params}",
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "es",
        },
    )
    try:
        with urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    return candidates if isinstance(candidates, list) else []


def _score_result(result: dict, parts: list[str]) -> float:
    score = 0.0
    road = _normalize_road_text(parts[0]) if parts else ""
    display_name = _normalize_text(result.get("display_name") or "")
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    address_road = _normalize_text(address.get("road") or "")
    address_house_number = _normalize_text(address.get("house_number") or "")
    haystack = " ".join(filter(None, [display_name, address_road, address_house_number]))

    for index, part in enumerate(parts):
        normalized = _normalize_text(part)
        if normalized and normalized in haystack:
            score += 4 if index == 0 else 2

    # El municipio pedido manda: un resultado de otro municipio debe quedar descartado.
    requested_city = _requested_city(parts)
    city_fields = [
        _normalize_text(address.get("city") or ""),
        _normalize_text(address.get("town") or ""),
        _normalize_text(address.get("municipality") or ""),
        _normalize_text(address.get("village") or ""),
        _normalize_text(address.get("county") or ""),
    ]
    if requested_city:
        if requested_city in city_fields:
            score += 20
        elif any(requested_city in field or field in requested_city for field in city_fields if field):
            score += 6
        else:
            score -= 50

    if road and road == address_road:
        score += 8

    # Un resultado sin via (solo ciudad) es un aproximado y no debe superar a una
    # direccion real cuando el usuario pidio calle y numero.
    if _has_street_level(road) and not address_road:
        score -= 12

    numeric_tokens = re.findall(r"\d+[a-z]?(?:-\d+[a-z]?)?", road)
    if numeric_tokens:
        matched_tokens = [token for token in numeric_tokens if token in haystack]
        score += len(matched_tokens) * 1.5
        if len(matched_tokens) == len(numeric_tokens):
            score += 5

    if address_house_number and "#" in road and address_house_number in road:
        score += 4

    importance = result.get("importance")
    try:
        score += float(importance or 0)
    except (TypeError, ValueError):
        pass

    return score


def _score_arcgis_result(result: dict, parts: list[str]) -> float:
    score = 0.0
    attributes = result.get("attributes", {}) if isinstance(result.get("attributes"), dict) else {}
    address_text = _normalize_text(result.get("address") or attributes.get("Match_addr") or "")
    addr_type = _normalize_text(attributes.get("Addr_type") or "")
    try:
        score += float(result.get("score") or 0)
    except (TypeError, ValueError):
        pass

    road = _normalize_road_text(parts[0]) if parts else ""
    if road and road in address_text:
        score += 12

    numeric_tokens = re.findall(r"\d+[a-z]?(?:-\d+[a-z]?)?", road)
    if numeric_tokens:
        matched_tokens = [token for token in numeric_tokens if token in address_text]
        score += len(matched_tokens) * 3
        if len(matched_tokens) == len(numeric_tokens):
            score += 8

    for part in parts[1:]:
        normalized = _normalize_text(part)
        if normalized and normalized in address_text:
            score += 2

    # El municipio pedido manda: un resultado de otro municipio debe quedar descartado.
    requested_city = _requested_city(parts)
    city_fields = [
        _normalize_text(attributes.get("City") or ""),
        _normalize_text(attributes.get("Subregion") or ""),
        _normalize_text(attributes.get("District") or ""),
    ]
    if requested_city:
        if requested_city in city_fields:
            score += 25
        elif any(requested_city in field or field in requested_city for field in city_fields if field):
            score += 8
        else:
            score -= 60

    if addr_type == "pointaddress":
        score += 10
    elif addr_type == "streetaddress":
        score += 6
    elif addr_type == "streetname":
        score += 4

    # Si se pidio una direccion concreta, un resultado a nivel de municipio es solo
    # un aproximado y no debe competir contra una direccion real.
    if addr_type in {"locality", "postal", "admin", "subregion", "region"} and _has_street_level(parts[0] if parts else ""):
        score -= 25

    return score
