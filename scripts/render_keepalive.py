"""Hace un ping HTTP a la app desplegada para mantenerla activa en Render Free."""
from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> int:
    base_url = (os.getenv("KEEPALIVE_URL") or "").strip().rstrip("/")
    if not base_url:
        print("Falta la variable KEEPALIVE_URL.", file=sys.stderr)
        return 1

    target = f"{base_url}/healthz"
    request = Request(
        target,
        headers={
            "User-Agent": "ruta-confianza-keepalive/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
            print(f"Ping OK {response.status}: {body}")
            return 0
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        print(f"Ping HTTP {error.code}: {body}", file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Ping fallo: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
