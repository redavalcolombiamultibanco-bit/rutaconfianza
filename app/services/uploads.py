"""Guardado de las imagenes que suben los usuarios.

Centraliza la validacion y el nombrado de archivos para que la foto de la
buseta se guarde igual desde el panel del conductor y desde el panel
administrativo.
"""
from pathlib import Path
from uuid import uuid4

from flask import current_app, url_for
from werkzeug.utils import secure_filename


def guardar_foto_vehiculo(archivo):
    """Guarda la imagen del vehiculo en static/uploads y devuelve su ruta publica.

    Lanza ValueError cuando el archivo no es una imagen con extension permitida.
    """
    # Sanitiza el nombre y comprueba que la extension sea una imagen permitida.
    nombre_original = secure_filename(archivo.filename)
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else ""
    if extension not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        raise ValueError("La foto de la buseta debe ser una imagen (jpg, png, webp o gif).")

    # Genera un nombre unico (uuid) para no pisar fotos anteriores.
    carpeta = Path(current_app.config["UPLOAD_FOLDER"])
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_archivo = f"buseta-{uuid4().hex}.{extension}"
    archivo.save(carpeta / nombre_archivo)
    return url_for("static", filename=f"uploads/{nombre_archivo}")
