"""Servicio de copias de seguridad: empaqueta la base de datos y las fotos subidas.

Cada respaldo es un archivo .zip con la base de datos SQLite, la carpeta de
fotos subidas y un manifiesto con la fecha y los conteos de registros.
Permite crear, listar, descargar, eliminar y restaurar respaldos desde el
panel administrativo.
"""
import json
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from flask import current_app

from app import db

DB_EN_ZIP = "base_datos.db"
UPLOADS_EN_ZIP = "uploads"
MANIFIESTO = "manifiesto.json"
MOTIVO_AUTOMATICO = "automatico"


def carpeta_respaldos():
    """Devuelve (y crea si hace falta) la carpeta donde se guardan los respaldos."""
    carpeta = Path(current_app.config["BACKUP_FOLDER"])
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta


def _ruta_base_datos():
    return Path(db.engine.url.database).resolve()


def _carpeta_uploads():
    return Path(current_app.config["UPLOAD_FOLDER"]).resolve()


def _conteos_actuales():
    """Resumen rapido del estado de la base de datos para el manifiesto."""
    from app.models import Conductor, Estudiante, MensajeContacto, Ruta, SolicitudConductor

    return {
        "conductores": Conductor.query.count(),
        "rutas": Ruta.query.count(),
        "estudiantes": Estudiante.query.count(),
        "solicitudes": SolicitudConductor.query.count(),
        "mensajes_contacto": MensajeContacto.query.count(),
    }


def _formatear_tamano(bytes_total):
    """Convierte bytes a una etiqueta legible (KB, MB)."""
    if bytes_total is None:
        return "-"
    unidades = ["B", "KB", "MB", "GB"]
    valor = float(bytes_total)
    for unidad in unidades:
        if valor < 1024 or unidad == unidades[-1]:
            return f"{valor:.1f} {unidad}" if unidad != "B" else f"{int(valor)} B"
        valor /= 1024
    return f"{valor:.1f} GB"


def crear_respaldo(motivo="manual"):
    """Empaqueta la base de datos y las fotos en un archivo zip con marca de tiempo."""
    db.session.commit()
    db.session.remove()

    destino = carpeta_respaldos() / f"respaldo-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{motivo}.zip"
    db_path = _ruta_base_datos()
    uploads_dir = _carpeta_uploads()

    manifiesto = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "motivo": motivo,
        "aplicacion": current_app.config.get("APP_NAME", "Ruta Confianza"),
        "base_datos": DB_EN_ZIP,
        "conteos": _conteos_actuales(),
    }

    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as archivo_zip:
        if db_path.exists():
            archivo_zip.write(db_path, DB_EN_ZIP)
        if uploads_dir.exists():
            for archivo in sorted(uploads_dir.rglob("*")):
                if archivo.is_file():
                    relativo = archivo.relative_to(uploads_dir).as_posix()
                    archivo_zip.write(archivo, f"{UPLOADS_EN_ZIP}/{relativo}")
        archivo_zip.writestr(MANIFIESTO, json.dumps(manifiesto, ensure_ascii=False, indent=2))

    return describir_respaldo(destino)


def _leer_manifiesto(archivo):
    try:
        with zipfile.ZipFile(archivo) as archivo_zip:
            if MANIFIESTO in archivo_zip.namelist():
                return json.loads(archivo_zip.read(MANIFIESTO).decode("utf-8"))
    except (zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return None
    return None


def describir_respaldo(archivo):
    """Arma el diccionario de metadatos que consume la plantilla."""
    archivo = Path(archivo)
    estadistica = archivo.stat()
    return {
        "nombre": archivo.name,
        "ruta": str(archivo),
        "tamano": _formatear_tamano(estadistica.st_size),
        "tamano_bytes": estadistica.st_size,
        "fecha": datetime.fromtimestamp(estadistica.st_mtime),
        "manifiesto": _leer_manifiesto(archivo),
    }


def listar_respaldos():
    """Devuelve los respaldos existentes, del mas reciente al mas antiguo."""
    archivos = sorted(
        carpeta_respaldos().glob("*.zip"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return [describir_respaldo(archivo) for archivo in archivos]


def ruta_respaldo(nombre):
    """Resuelve el archivo de respaldo validando que no se salga de la carpeta."""
    nombre_limpio = Path(nombre).name
    if not nombre_limpio.lower().endswith(".zip"):
        raise ValueError("El archivo indicado no es un respaldo valido.")
    archivo = (carpeta_respaldos() / nombre_limpio).resolve()
    if archivo.parent != carpeta_respaldos().resolve() or not archivo.exists():
        raise FileNotFoundError("El respaldo solicitado no existe.")
    return archivo


def eliminar_respaldo(nombre):
    archivo = ruta_respaldo(nombre)
    archivo.unlink()
    return archivo.name


def restaurar_respaldo(nombre):
    """Reemplaza la base de datos y las fotos actuales por las del respaldo indicado."""
    archivo = ruta_respaldo(nombre)
    destino_db = _ruta_base_datos()
    uploads_dir = _carpeta_uploads()

    with zipfile.ZipFile(archivo) as archivo_zip:
        nombres = archivo_zip.namelist()
        if DB_EN_ZIP not in nombres:
            raise ValueError("El respaldo no contiene la base de datos esperada.")

        # Respaldo de seguridad del estado actual antes de sobrescribir.
        crear_respaldo("pre-restauracion")

        db.session.remove()
        db.engine.dispose()

        # Se escribe en un archivo temporal y se reemplaza de forma atomica para
        # que ninguna peticion en curso vea la base de datos a medio escribir.
        destino_db.parent.mkdir(parents=True, exist_ok=True)
        temporal_db = destino_db.with_name(destino_db.name + ".restaurando")
        try:
            temporal_db.write_bytes(archivo_zip.read(DB_EN_ZIP))
            os.replace(temporal_db, destino_db)
        finally:
            temporal_db.unlink(missing_ok=True)

        if uploads_dir.exists():
            shutil.rmtree(uploads_dir, ignore_errors=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        for miembro in nombres:
            if not miembro.startswith(f"{UPLOADS_EN_ZIP}/") or miembro.endswith("/"):
                continue
            relativo = Path(miembro[len(UPLOADS_EN_ZIP) + 1:])
            if not relativo.parts or ".." in relativo.parts:
                continue
            destino_archivo = uploads_dir / relativo
            destino_archivo.parent.mkdir(parents=True, exist_ok=True)
            destino_archivo.write_bytes(archivo_zip.read(miembro))

    # Si el respaldo viene de una version anterior, completa columnas faltantes.
    from app import _ensure_runtime_schema

    db.create_all()
    _ensure_runtime_schema()
    return archivo.name


def respaldos_automaticos():
    """Devuelve los respaldos creados por el programador, del mas nuevo al mas viejo."""
    archivos = carpeta_respaldos().glob(f"respaldo-*-{MOTIVO_AUTOMATICO}.zip")
    return sorted(archivos, key=lambda item: item.stat().st_mtime, reverse=True)


def aplicar_retencion_respaldos():
    """Borra los respaldos automaticos mas antiguos y conserva los ultimos permitidos."""
    retencion = max(1, int(current_app.config.get("BACKUP_RETENCION", 7)))
    eliminados = []
    for archivo in respaldos_automaticos()[retencion:]:
        try:
            archivo.unlink()
        except OSError:
            # Si no se puede borrar (por ejemplo esta en descarga) se deja para
            # el siguiente intento en lugar de interrumpir el respaldo.
            continue
        eliminados.append(archivo.name)
    return eliminados


def respaldo_automatico_si_corresponde():
    """Crea un respaldo si ya paso el intervalo configurado y aplica la retencion.

    Devuelve la informacion del respaldo creado o `None` cuando todavia no toca.
    """
    if not current_app.config.get("BACKUP_AUTOMATICO", True):
        return None

    horas = float(current_app.config.get("BACKUP_INTERVALO_HORAS", 24))
    anteriores = respaldos_automaticos()
    if anteriores:
        ultimo = datetime.fromtimestamp(anteriores[0].stat().st_mtime)
        if (datetime.now() - ultimo).total_seconds() < horas * 3600:
            return None

    info = crear_respaldo(MOTIVO_AUTOMATICO)
    aplicar_retencion_respaldos()
    return info
