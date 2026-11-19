"""Servicios simples de geocodificacion para direcciones en Colombia."""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Iterable, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# Endpoints de los dos proveedores de geocodificacion que se consultan.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
ARCGIS_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
# Nominatim solo admite una peticion por segundo; se deja margen para no recibir
# un 429 y perder la respuesta. Tras un bloqueo se espera algo mas antes de
# reintentar.
NOMINATIM_INTERVALO_MINIMO = 1.1
NOMINATIM_ESPERA_TRAS_BLOQUEO = 1.5
# El User-Agent identifica la aplicacion; Nominatim rechaza peticiones sin uno.
USER_AGENT = "RutaExacta/1.0 (+https://rutaexacta.local)"
# Cuando la interpolacion de cuadra (grid) cae mas lejos que esto desde la via
# conocida, significa que una de las esquinas se resolvio mal (por ejemplo, en
# otro municipio) y el punto quedaria a kilometros de la direccion real. En ese
# caso se descarta y se deja el punto sobre la via, que siempre queda cerca.
GRID_DISTANCIA_MAXIMA_METROS = 500
# Distancia maxima permitida entre la direccion ubicada y el centro del municipio
# pedido. Si el proveedor devuelve un punto mas lejos, se considera que lo ubico
# fuera del pueblo y se prefiere el centro del municipio. Asi, en cualquier
# municipio, una casa nunca queda "por alla fuera del mapa".
MUNICIPIO_DISTANCIA_MAXIMA_METROS = 12000
# Caja envolvente aproximada del departamento de Casanare. Sirve para descartar
# resultados que claramente cayeron fuera del area de operacion.
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

    # Intento 1: ArcGIS, pero solo si acierta a una direccion concreta y no al
    # centro del municipio (resultado aproximado). Si la direccion trae numero de
    # casa, tambien se exige que el candidato reconozca ese numero: asi un
    # resultado a nivel de calle no descarta una placa exacta que si conoce el
    # otro proveedor. ArcGIS queda igualmente como ultimo recurso mas abajo.
    arcgis_result = _find_best_result_arcgis(cleaned_parts)
    if (
        arcgis_result
        and not _is_approximate_result(arcgis_result)
        and _matches_house_number(arcgis_result, cleaned_parts)
    ):
        return _ajustar_al_municipio(_build_geodata(arcgis_result), cleaned_parts)

    # Intento 2: Nominatim y, si la direccion lleva numero de casa, la
    # interpolacion de cuadra (grid) cuando es mas precisa que el resultado.
    best_result = _find_best_result(cleaned_parts)
    if best_result and not _should_prefer_grid_fallback(cleaned_parts, best_result):
        # Ya quedo ubicada la via, asi que no hace falta estimar la cuadra: se
        # evita ese calculo, que es el que mas consultas gasta y mas tarda.
        return _ajustar_al_municipio(_build_geodata(best_result), cleaned_parts)
    grid_fallback = _geocode_grid_address(cleaned_parts)
    if grid_fallback and _grid_cerca_de_la_via(grid_fallback, best_result):
        return _ajustar_al_municipio(grid_fallback, cleaned_parts)
    # Si la interpolacion no es confiable, se deja el punto sobre la via ubicada:
    # es aproximado, pero siempre cae en la calle correcta y no a kilometros.
    if best_result:
        return _ajustar_al_municipio(_build_geodata(best_result), cleaned_parts)

    # Intento 3: esquinas ("calle X con carrera Y") que ningun proveedor ubica.
    intersection_result = _geocode_intersection(cleaned_parts)
    if intersection_result:
        return _ajustar_al_municipio(intersection_result, cleaned_parts)

    # Ultimo recurso: se acepta el resultado aproximado si pertenece al municipio pedido.
    if arcgis_result:
        return _ajustar_al_municipio(_build_geodata(arcgis_result), cleaned_parts)
    return None


def _clean_parts(parts: Iterable[str]) -> list[str]:
    """Normaliza y descarta las partes vacias de una direccion.

    Recibe los fragmentos de la direccion (calle, municipio, departamento...) y
    devuelve solo los que aportan texto, ya normalizados.
    """
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
    # Se recorre de atras hacia adelante porque el municipio suele quedar pegado
    # al pais; se saltan los tokens de region ("casanare", "colombia").
    for part in reversed(parts[1:]):
        normalized = _normalize_text(part)
        if normalized and normalized not in _REGION_TOKENS:
            return normalized
    return ""


def _is_within_casanare(result: dict) -> bool:
    """Descarta geocodificaciones que claramente cayeron fuera de Casanare."""
    # Cada proveedor entrega las coordenadas con nombres distintos: ArcGIS usa
    # location.x/location.y y Nominatim usa lat/lon.
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
    """True si la direccion menciona Casanare y por tanto se exige ese limite."""
    return any(_normalize_text(part) == "casanare" for part in parts[1:])


def _has_street_level(value: str) -> bool:
    """True si el texto pedido apunta a una direccion concreta (calle o numero)."""
    return "#" in value or _looks_like_road(value)


def _is_approximate_result(result: dict) -> bool:
    """True si el resultado solo apunta al municipio y no a una direccion exacta."""
    attributes = result.get("attributes") if isinstance(result.get("attributes"), dict) else {}
    addr_type = _normalize_text(attributes.get("Addr_type") or "")
    return addr_type in {"locality", "postal", "admin", "subregion", "region"}


def _house_number_in_query(parts: list[str]) -> str:
    """Devuelve el numero de casa pedido ("6-45"); cadena vacia si no hay."""
    road = _normalize_road_text(parts[0]) if parts else ""
    match = re.search(r"#\s*(\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?)", road)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def _house_number_in_text(haystack: str, house_number: str) -> bool:
    """True si el numero de casa aparece en el texto del proveedor.

    Los geocodificadores escriben el numero de dos formas ("8-25" o "8 25"),
    asi que se aceptan ambas: sin esto se descartaba una placa exacta solo
    porque venia con espacio en vez de guion, y la casa terminaba en otra
    calle. Se exige que el numero no sea parte de un numero mas largo (que
    "8-25" no coincida dentro de "18-25").
    """
    for variante in (house_number, house_number.replace("-", " ")):
        if re.search(r"(?<!\d)" + re.escape(variante) + r"(?!\d)", haystack):
            return True
    return False


def _matches_house_number(result: dict, parts: list[str]) -> bool:
    """True si el candidato reconoce el numero de casa que pidio el usuario.

    Cuando la direccion lleva numero de casa, un resultado que solo ubica la via
    (por ejemplo, el centro de la calle) deja la casa a cientos de metros de su
    sitio. En ese caso conviene seguir buscando, porque otro proveedor puede
    tener la placa exacta. Si la direccion no trae numero, todo candidato sirve.
    """
    house_number = _house_number_in_query(parts)
    if not house_number:
        return True

    attributes = result.get("attributes") if isinstance(result.get("attributes"), dict) else {}
    haystack = _normalize_text(result.get("address") or attributes.get("Match_addr") or "")
    return _house_number_in_text(haystack, house_number)


def _tiene_placa_exacta(result: dict, parts: list[str]) -> bool:
    """True si el resultado ya reconoce el numero de casa que pidio el usuario."""
    numero = _house_number_in_query(parts)
    if not numero or not result:
        return False
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    reconocido = _normalize_text(address.get("house_number") or "")
    return _house_number_in_text(reconocido, numero)


def _build_candidate_queries(parts: list[str]) -> list[str]:
    """Arma variantes de la consulta para dar varias oportunidades al proveedor.

    Genera combinaciones con la via y el contexto (municipio) en distinto orden y
    con distinto nivel de detalle, porque los geocodificadores aciertan mas con
    unas formas que con otras. Devuelve como maximo 7 consultas sin repetir.
    """
    queries = []
    road = _normalize_road_text(parts[0])
    road_spaced = _expand_numeric_letter_spacing(road)
    road_without_details = _strip_secondary_references(road)
    road_without_house_number = _strip_house_number(road_without_details)
    context = parts[1:]

    # El barrio es el dato menos confiable: suele estar mal escrito o no existir
    # para el proveedor y, cuando no lo reconoce, hace perder la coincidencia
    # exacta de la placa. Por eso la variante con la via y el municipio, sin el
    # barrio, se prueba primero; la que si lo incluye queda como alternativa.
    sin_barrio = [parte for parte in context if parte == _requested_city(parts) or parte in _REGION_TOKENS]
    if len(sin_barrio) != len(context):
        queries.append(", ".join([road, *sin_barrio]))
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
    # Estandariza las abreviaturas de nomenclatura (cra -> carrera, cl -> calle,
    # av -> avenida...) para que todos los intentos usen las mismas palabras.
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
    # Quita tildes (NFKD + ascii), pasa a minusculas y unifica separadores. Deja el
    # texto comparable sin depender de acentos ni de espacios extra.
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    # El punto de las abreviaturas ("Cl. 14", "Cra. 6") se cambia por un espacio
    # para que la via quede como "cl 14" y las reglas siguientes la reconozcan.
    # Sin esto quedaba "cl. 14" y la direccion no se podia descomponer.
    normalized = re.sub(r"\.", " ", normalized)
    normalized = re.sub(r"[;|]+", " ", normalized)
    normalized = re.sub(r"[,]+", ", ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" ,")


def _strip_secondary_references(value: str) -> str:
    # Corta desde palabras como "barrio", "torre" o "apartamento" en adelante:
    # esos detalles no ayudan al proveedor y suelen desviar la busqueda.
    return re.sub(
        r"\b(apto|apartamento|interior|int|bloque|torre|casa|manzana|mz|lote|local|barrio|sector|conjunto|urbanizacion)\b.*$",
        "",
        value,
    ).strip(" ,")


def _strip_house_number(value: str) -> str:
    # Quita el numero de casa (por ejemplo "# 15-30"); deja solo la via base.
    return re.sub(r"\s+#\s*\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?.*$", "", value).strip(" ,")


def _build_intersection_variant(value: str) -> str:
    # Los proveedores entienden mejor las esquinas escritas con "y" que con "con".
    return re.sub(r"\bcon\b", " y ", value).strip(" ,")


def _build_generic_variant(value: str) -> str:
    # Variante sin el tipo de via: a veces el proveedor reconoce el numero solo.
    generic = re.sub(r"\b(carrera|calle|avenida|transversal|diagonal)\b", "", value)
    generic = re.sub(r"\s+", " ", generic)
    return generic.strip(" ,")


def _expand_numeric_letter_spacing(value: str) -> str:
    # Separa numero y letra ("15a" -> "15 a"); algunos proveedores prefieren el espacio.
    return re.sub(r"\b(\d+)([a-z])\b", r"\1 \2", value)


def _find_best_result(parts: list[str], max_queries: Optional[int] = None) -> Optional[dict]:
    """Busca en Nominatim y devuelve la mejor coincidencia segun su puntaje.

    Recorre las consultas candidatas, descarta resultados fuera de Casanare
    cuando la direccion lo exige y se queda con el de mayor puntaje. Descarta los
    puntajes demasiado bajos para no ubicar la direccion en otro municipio.
    `max_queries` limita cuantas variantes se prueban, para las busquedas
    auxiliares donde cada consulta cuesta una peticion al proveedor.
    """
    queries = _build_candidate_queries(parts)
    if max_queries is not None:
        queries = queries[:max_queries]
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
        # Si ya se encontro la placa exacta no hace falta seguir consultando: se
        # ahorran peticiones y se reduce el riesgo de bloqueo del proveedor.
        if _tiene_placa_exacta(best_result, parts) or best_score >= 8:
            break
    # Se descartan coincidencias demasiado debiles (por ejemplo, de otro municipio).
    return best_result if best_score >= 4 else None


def _find_best_result_arcgis(parts: list[str]) -> Optional[dict]:
    """Equivalente a `_find_best_result` pero consultando ArcGIS.

    Los umbrales de puntaje (105/90) son mas altos porque ArcGIS usa otra escala
    de puntuacion que Nominatim.
    """
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
    """Convierte un resultado crudo de cualquier proveedor al formato interno.

    Devuelve un dict con latitud, longitud y display_name; o None si el resultado
    no trae coordenadas validas.
    """
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


def _distancia_metros(punto_a: dict, punto_b: dict) -> float:
    """Distancia aproximada en metros entre dos puntos con latitud y longitud."""
    diferencia_latitud = punto_a["latitud"] - punto_b["latitud"]
    diferencia_longitud = punto_a["longitud"] - punto_b["longitud"]
    return (diferencia_latitud ** 2 + diferencia_longitud ** 2) ** 0.5 * 111320


def _grid_cerca_de_la_via(grid_result: dict, best_result: Optional[dict]) -> bool:
    """True si el punto interpolado cae cerca de la via ya ubicada.

    La interpolacion de cuadra suma las coordenadas de dos esquinas; si una de
    ellas se resolvio mal (por ejemplo, en otro municipio), el punto queda a
    kilometros de la calle. En ese caso conviene descartarlo. Si no hay via
    conocida no hay con que comparar y se acepta.
    """
    if not best_result:
        return True
    via = _build_geodata(best_result)
    if not via:
        return True
    return _distancia_metros(grid_result, via) <= GRID_DISTANCIA_MAXIMA_METROS


# Centros de municipio ya resueltos, para no repetir la consulta (se recuerda el
# resultado por municipio durante la vida del proceso).
_centros_municipio: dict[str, Optional[dict]] = {}
# Radio para considerar que dos resultados del mismo municipio apuntan al mismo
# sitio. Sirve para separar el casco urbano de un centroide municipal lejano.
CENTRO_MUNICIPIO_RADIO_METROS = 3000


def _punto_mas_denso(puntos: list[dict]) -> Optional[dict]:
    """Devuelve el punto con mas vecinos cercanos (la ubicacion urbana repetida).

    Los proveedores suelen incluir, ademas del casco urbano, el centroide del
    municipio completo, que puede quedar a decenas de kilometros del pueblo. El
    punto urbano aparece varias veces y los centroides quedan aislados, asi que
    se elige el que agrupa mas coincidencias.
    """
    mejor = None
    mejor_vecinos = -1
    for punto in puntos:
        vecinos = sum(
            1
            for otro in puntos
            if otro is not punto and _distancia_metros(punto, otro) <= CENTRO_MUNICIPIO_RADIO_METROS
        )
        if vecinos > mejor_vecinos:
            mejor = punto
            mejor_vecinos = vecinos
    return mejor


def _municipio_centro(parts: list[str]) -> Optional[dict]:
    """Ubica el casco urbano del municipio pedido; None si no se puede resolver.

    Es la referencia para saber si una direccion quedo dentro del pueblo. Se
    consulta una sola vez por municipio y se guarda el resultado. Se prueba
    primero ArcGIS porque no tiene el limite de una peticion por segundo.
    """
    ciudad = _requested_city(parts)
    if not ciudad:
        return None
    if ciudad in _centros_municipio:
        return _centros_municipio[ciudad]

    consulta = ", ".join([ciudad, "Casanare", "Colombia"])
    puntos = []
    for buscar in (_search_arcgis, _search_nominatim):
        for result in buscar(consulta):
            punto = _build_geodata(result)
            if punto and _is_within_casanare(result):
                puntos.append(punto)

    centro = _punto_mas_denso(puntos)
    _centros_municipio[ciudad] = centro
    return centro


def _dentro_del_municipio(geodata: dict, parts: list[str]) -> bool:
    """True si la direccion ubicada cae dentro del municipio pedido.

    Si no se pudo obtener el centro del municipio no hay con que comparar y se
    acepta el resultado del proveedor (no se descarta por falta de referencia).
    """
    centro = _municipio_centro(parts)
    if not centro:
        return True
    return _distancia_metros(geodata, centro) <= MUNICIPIO_DISTANCIA_MAXIMA_METROS


def _ajustar_al_municipio(geodata: Optional[dict], parts: list[str]) -> Optional[dict]:
    """Evita que una direccion quede ubicada fuera del municipio pedido.

    Si el punto cae demasiado lejos del centro del municipio, se prefiere ese
    centro: es mejor una ubicacion aproximada dentro del pueblo que una casa
    "por alla fuera del mapa".
    """
    if not geodata or _dentro_del_municipio(geodata, parts):
        return geodata
    return _municipio_centro(parts) or geodata


def _geocode_intersection(parts: list[str]) -> Optional[dict]:
    """Ubica una esquina promediando las coordenadas de las dos vias.

    Geocodifica cada via por separado y devuelve el punto medio; si alguna de las
    dos vias no se encuentra, no hay esquina y devuelve None.
    """
    roads = _split_intersection(parts[0])
    if not roads:
        return None

    context = parts[1:]
    # Cada via es un nombre simple ("calle 14"), asi que con las dos primeras
    # variantes basta: probar mas solo suma consultas al proveedor y demora.
    first_result = _find_best_result([roads[0], *context], max_queries=2)
    second_result = _find_best_result([roads[1], *context], max_queries=2)
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
    """Estima el punto de una direccion con numero interpolando entre esquinas.

    Cuando el proveedor no conoce la placa exacta, ubica la esquina base y la
    siguiente (o la anterior) y coloca el punto a lo largo de esa cuadra segun el
    numero de casa.
    """
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
    """Descompone una direccion tipo "carrera 21 # 15-30".

    Devuelve (tipo principal, numero principal, tipo transversal, numero
    transversal, distancia de casa) o None si el texto no tiene ese formato. La
    via transversal se deduce: si la principal es calle, la cruz es carrera.
    """
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
    """Suma 1 al numero de una via ("15" -> "16"); None si no es numerica."""
    match = re.match(r"^(\d+)(\s*[a-z]?)$", value.strip())
    if not match:
        return None
    return f"{int(match.group(1)) + 1}{match.group(2)}".strip()


def _decrement_road_number(value: str) -> Optional[str]:
    """Resta 1 al numero de una via; None si no es numerica o quedaria en cero."""
    match = re.match(r"^(\d+)(\s*[a-z]?)$", value.strip())
    if not match:
        return None
    number = int(match.group(1)) - 1
    if number <= 0:
        return None
    return f"{number}{match.group(2)}".strip()


def _should_prefer_grid_fallback(parts: list[str], best_result: Optional[dict]) -> bool:
    """Decide si conviene la interpolacion de cuadra en vez del resultado.

    Solo aplica cuando la direccion lleva numero de casa ("#") y el proveedor no
    aporto ese numero (o no hubo resultado): ahi la interpolacion es mas precisa
    que ubicar el punto en el centro del municipio.
    """
    road = parts[0] if parts else ""
    if "#" not in road:
        return False
    if not best_result:
        return True

    address = best_result.get("address") if isinstance(best_result.get("address"), dict) else {}
    address_house_number = _normalize_text(address.get("house_number") or "")
    return not bool(address_house_number)


def _split_intersection(value: str) -> Optional[tuple[str, str]]:
    """Separa una esquina en sus dos vias ("calle 10 y carrera 5"); None si no es esquina."""
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
    """True si el texto contiene un tipo de via reconocido (calle, carrera...)."""
    return bool(re.search(r"\b(carrera|calle|avenida|transversal|diagonal)\b", value))


# Marca de tiempo de la ultima consulta enviada a Nominatim, para no superar su
# limite de una peticion por segundo.
_ultima_consulta_nominatim = 0.0


def _esperar_turno_nominatim():
    """Espera lo necesario para que la siguiente consulta no llegue demasiado pronto."""
    global _ultima_consulta_nominatim
    espera = NOMINATIM_INTERVALO_MINIMO - (time.monotonic() - _ultima_consulta_nominatim)
    if espera > 0:
        time.sleep(espera)
    _ultima_consulta_nominatim = time.monotonic()


def _search_nominatim(query: str) -> list[dict]:
    """Consulta Nominatim/OpenStreetMap y devuelve la lista cruda de resultados.

    Nominatim permite una peticion por segundo: si se envian varias seguidas
    responde 429 y la respuesta se perderia, dejando la direccion mal ubicada.
    Por eso se espacian las consultas y se reintenta una vez tras el bloqueo.
    Llama a una API externa con un timeout; ante cualquier error de red o de
    formato devuelve una lista vacia para no romper el flujo de geocodificacion.
    """
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
    for intento in range(2):
        _esperar_turno_nominatim()
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, list) else []
        except HTTPError as error:
            if error.code != 429:
                return []
            # Bloqueado por exceso de peticiones: se espera antes de reintentar.
            time.sleep(NOMINATIM_ESPERA_TRAS_BLOQUEO)
        except Exception:
            return []
    return []


def _search_arcgis(query: str) -> list[dict]:
    """Consulta ArcGIS y devuelve la lista de candidatos.

    Igual que `_search_nominatim`, ante cualquier fallo responde con lista vacia.
    """
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
    """Puntua un resultado de Nominatim frente a lo que pidio el usuario.

    Suma puntos por coincidir con la via y los tokens numericos, da mucho peso a
    acertar el municipio y resta cuando el resultado es solo un aproximado o de
    otro municipio. Cuanto mas alto, mejor coincidencia.
    """
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
    """Puntua un candidato de ArcGIS (misma idea que `_score_result`).

    Parte del score propio de ArcGIS y ajusta segun la via, los numeros, el
    municipio y la precision del tipo de direccion.
    """
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
