"""Utilidades compartidas para la recuperacion de contrasenas por pregunta de seguridad.

La respuesta se normaliza (minusculas, sin tildes ni espacios sobrantes) antes de
guardarse o compararse, para que el usuario no falle por diferencias de escritura.
"""
import unicodedata
from werkzeug.security import check_password_hash, generate_password_hash

PREGUNTAS_SUGERIDAS = (
    "Cual es el nombre de tu primera mascota",
    "En que ciudad nacio tu madre",
    "Cual es el nombre de tu mejor amigo de la infancia",
    "Cual fue el nombre de tu primer colegio",
    "Cual es tu comida favorita",
    "Cual es el nombre de tu primer vehiculo",
)


def normalizar_respuesta(valor):
    """Devuelve la respuesta en minusculas, sin tildes y con espacios simples."""
    texto = unicodedata.normalize("NFKD", (valor or "").strip().lower())
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    return " ".join(texto.split())


def hash_respuesta(valor):
    """Genera el hash de la respuesta normalizada (o None si queda vacia)."""
    normalizada = normalizar_respuesta(valor)
    return generate_password_hash(normalizada) if normalizada else None


def verificar_respuesta(hash_guardado, valor):
    """Compara la respuesta del usuario contra el hash almacenado."""
    normalizada = normalizar_respuesta(valor)
    if not hash_guardado or not normalizada:
        return False
    return check_password_hash(hash_guardado, normalizada)


def aplicar_recuperacion(cuenta, pregunta, respuesta):
    """Actualiza pregunta y respuesta de una cuenta.

    Devuelve un mensaje de error cuando la combinacion enviada es incompleta,
    o None cuando los datos se aplicaron correctamente.
    """
    pregunta = (pregunta or "").strip() or None
    if respuesta:
        if not pregunta:
            return "Indica la pregunta de seguridad junto con la respuesta."
        cuenta.pregunta_seguridad = pregunta
        cuenta.respuesta_seguridad_hash = hash_respuesta(respuesta)
        return None
    if pregunta:
        cuenta.pregunta_seguridad = pregunta
        if not cuenta.respuesta_seguridad_hash:
            return "Para activar la recuperacion debes registrar tambien la respuesta."
    return None
