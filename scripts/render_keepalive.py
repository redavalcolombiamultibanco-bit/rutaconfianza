"""Hace un ping HTTP a la app desplegada para mantenerla activa en Render Free.

Render apaga (duerme) los servicios gratuitos tras un periodo sin trafico. Este
script se ejecuta periodicamente (por ejemplo con un cron) y hace una peticion
GET al endpoint /healthz de la app para que Render la considere activa y no la
duerma, evitando el arranque lento en frio.

Configuracion: define la variable de entorno KEEPALIVE_URL con la URL base de
la app (sin barra final). Devuelve 0 si el ping fue correcto y 1 si fallo.
"""
from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    """Hace el ping a {KEEPALIVE_URL}/healthz y devuelve el codigo de salida.

    Lee la URL base de la variable de entorno KEEPALIVE_URL, arma la peticion al
    endpoint de salud y la ejecuta con un tiempo de espera. Codigo 0 si la app
    respondio; 1 si falta la configuracion o el ping falla (error HTTP o de red).
    """
    # Lee y limpia la URL base (quita espacios y la barra final para no duplicar /).
    base_url = (os.getenv("KEEPALIVE_URL") or "").strip().rstrip("/")
    if not base_url:
        print("Falta la variable KEEPALIVE_URL.", file=sys.stderr)
        return 1

    target = f"{base_url}/healthz"
    # Cabeceras minimas: un User-Agent propio para identificar el ping y aceptar JSON.
    request = Request(
        target,
        headers={
            "User-Agent": "ruta-confianza-keepalive/1.0",
            "Accept": "application/json",
        },
    )

    try:
        # timeout evita que el script quede colgado si la app no responde.
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
            print(f"Ping OK {response.status}: {body}")
            return 0
    except HTTPError as error:
        # La app respondio pero con un codigo de error (4xx/5xx).
        body = error.read().decode("utf-8", errors="replace")
        print(f"Ping HTTP {error.code}: {body}", file=sys.stderr)
        return 1
    except URLError as error:
        # No hubo respuesta (DNS, red caida, timeout, etc.).
        print(f"Ping fallo: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
