"""Control de intentos y de peticiones repetidas por IP.

Frena la fuerza bruta contra los PIN de las rutas y contra los accesos de
administrador y conductor, y limita el abuso de los formularios publicos.

Todo se guarda en memoria del proceso. El despliegue corre con un solo worker
de Gunicorn (ver render.yaml), por eso el contador es comun a todas las
peticiones. Si algun dia se usan varios workers o instancias, cada uno tendria
su propio contador y convendria mover esto a un almacen compartido.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from flask import request

# Cuanto se recuerdan los fallos antes de perdonarlos.
VENTANA_FALLOS_SEGUNDOS = 15 * 60
# Tope defensivo para que las tablas no crezcan sin limite.
MAX_CLAVES = 4000

# --- Politica compartida por las pantallas que la usan -----------------------
# Fuerza bruta contra los PIN de las rutas (formulario y WebSocket).
MAX_FALLOS_PIN = 10
BLOQUEO_PIN_SEGUNDOS = 15 * 60
# Fuerza bruta contra los accesos de administrador y conductor.
MAX_FALLOS_ACCESO = 8
BLOQUEO_ACCESO_SEGUNDOS = 15 * 60
# Intentos permitidos al recuperar la contrasena con la pregunta de seguridad.
MAX_INTENTOS_RECUPERACION = 10
VENTANA_RECUPERACION_SEGUNDOS = 10 * 60
# Envios permitidos desde una misma IP en los formularios publicos.
MAX_ENVIOS_FORMULARIO = 5
VENTANA_FORMULARIO_SEGUNDOS = 10 * 60

_candado = threading.Lock()
_accesos: dict[str, deque] = {}
_fallos: dict[str, deque] = {}
_bloqueos: dict[str, float] = {}


def _ahora() -> float:
    return time.monotonic()


def _descartar_vencidos(marcas: deque, desde: float) -> None:
    """Quita del frente las marcas mas antiguas que `desde`."""
    while marcas and marcas[0] < desde:
        marcas.popleft()


def _liberar_memoria(ahora: float) -> None:
    """Borra las claves que ya expiraron cuando las tablas crecen demasiado."""
    if len(_accesos) + len(_fallos) + len(_bloqueos) <= MAX_CLAVES:
        return
    limite = ahora - VENTANA_FALLOS_SEGUNDOS
    for tabla in (_accesos, _fallos):
        for clave in [clave for clave, marcas in tabla.items() if not marcas or marcas[-1] < limite]:
            tabla.pop(clave, None)
    for clave in [clave for clave, hasta in _bloqueos.items() if hasta <= ahora]:
        _bloqueos.pop(clave, None)


def cliente_ip() -> str:
    """Devuelve la IP del visitante considerando el proxy de Render.

    Render agrega la IP real al final de X-Forwarded-For, por eso se toma la
    ultima entrada: asi un encabezado inventado por el cliente no engaña al
    contador. Se lee primero `environ` para que tambien funcione dentro de los
    eventos de Socket.IO.
    """
    entorno = getattr(request, "environ", None) or {}
    reenviada = entorno.get("HTTP_X_FORWARDED_FOR") or ""
    if not reenviada:
        try:
            reenviada = request.headers.get("X-Forwarded-For") or ""
        except (AttributeError, RuntimeError):
            reenviada = ""
    partes = [parte.strip() for parte in reenviada.split(",") if parte.strip()]
    if partes:
        return partes[-1]
    return entorno.get("REMOTE_ADDR") or getattr(request, "remote_addr", None) or "desconocida"


def demasiadas_peticiones(clave: str, maximo: int, ventana_segundos: int) -> bool:
    """True si `clave` supero `maximo` peticiones dentro de la ventana."""
    ahora = _ahora()
    with _candado:
        marcas = _accesos.setdefault(clave, deque())
        _descartar_vencidos(marcas, ahora - ventana_segundos)
        if len(marcas) >= maximo:
            return True
        marcas.append(ahora)
        _liberar_memoria(ahora)
        return False


def bloqueo_restante(clave: str) -> int:
    """Segundos que faltan para que termine el bloqueo (0 si no hay ninguno)."""
    ahora = _ahora()
    with _candado:
        hasta = _bloqueos.get(clave)
        if not hasta:
            return 0
        if hasta <= ahora:
            _bloqueos.pop(clave, None)
            return 0
        return int(hasta - ahora) + 1


def registrar_fallo(clave: str, maximo_fallos: int, segundos_bloqueo: int) -> int:
    """Anota un intento fallido y bloquea la clave al llegar al limite.

    Devuelve los segundos de bloqueo aplicados, o 0 si aun no se bloquea.
    """
    ahora = _ahora()
    with _candado:
        marcas = _fallos.setdefault(clave, deque())
        _descartar_vencidos(marcas, ahora - VENTANA_FALLOS_SEGUNDOS)
        marcas.append(ahora)
        if len(marcas) >= maximo_fallos:
            marcas.clear()
            _bloqueos[clave] = ahora + segundos_bloqueo
            _liberar_memoria(ahora)
            return segundos_bloqueo
        _liberar_memoria(ahora)
        return 0


def olvidar(clave: str) -> None:
    """Limpia fallos y bloqueos de una clave, por ejemplo despues de acertar."""
    with _candado:
        _fallos.pop(clave, None)
        _bloqueos.pop(clave, None)


def espera_legible(segundos: int) -> str:
    """Texto corto de espera para mostrar al usuario."""
    if segundos <= 60:
        return "1 minuto"
    return f"{round(segundos / 60)} minutos"
