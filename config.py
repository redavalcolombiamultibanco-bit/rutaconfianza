"""Configuraciones de entorno para Ruta Confianza."""
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "ruta-confianza-dev-key")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'ruta_exacta.db'}")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JSON_AS_ASCII = False
    JSON_SORT_KEYS = False
    MAX_CONTENT_LENGTH = 64 * 1024 * 1024
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "app" / "static" / "uploads"))
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
    BACKUP_FOLDER = os.getenv("BACKUP_FOLDER", str(BASE_DIR / "respaldos"))
    SOCKETIO_ASYNC_MODE = os.getenv("SOCKETIO_ASYNC_MODE", "threading")
    SOCKETIO_CORS_ALLOWED_ORIGINS = os.getenv("SOCKETIO_CORS_ALLOWED_ORIGINS", "*")
    APP_NAME = "Ruta Confianza"
    COMPANY_NAME = "Ruta Confianza"
    BRAND_LOGO = "images/logo-ruta-confianza.jpg"
    MAP_DEFAULT_LAT = float(os.getenv("MAP_DEFAULT_LAT", "5.33775"))
    MAP_DEFAULT_LNG = float(os.getenv("MAP_DEFAULT_LNG", "-72.39586"))
    MAP_DEFAULT_ZOOM = int(os.getenv("MAP_DEFAULT_ZOOM", "14"))
    CONTACT_LOCATION = os.getenv("CONTACT_LOCATION", "Yopal, Casanare, Colombia")
    CONTACT_HOURS = os.getenv("CONTACT_HOURS", "Lunes a sabado, 6:00 a.m. a 6:00 p.m.")
    CONTACT_PHONE = os.getenv("CONTACT_PHONE", "")
    CONTACT_WHATSAPP = os.getenv("CONTACT_WHATSAPP", "3013444105")
    CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")
    # --- SEO e indexacion en buscadores ---
    # Si esta vacio se deduce del dominio de la peticion (util en Render).
    SITE_URL = os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    SITE_DESCRIPTION = os.getenv(
        "SITE_DESCRIPTION",
        "Ruta Confianza es la plataforma de transporte escolar con seguimiento GPS en vivo, "
        "control de asistencia y comunicacion directa con las familias en Yopal, Casanare.",
    )
    SITE_KEYWORDS = os.getenv(
        "SITE_KEYWORDS",
        "transporte escolar Yopal, ruta escolar Casanare, seguimiento GPS escolar, "
        "portal de padres ruta escolar, app conductor ruta escolar",
    )
    SOCIAL_IMAGE = os.getenv("SOCIAL_IMAGE", "images/portada-ruta-escolar-1.jpg")
    # Codigo de verificacion de Google Search Console (metodo etiqueta HTML).
    GOOGLE_SITE_VERIFICATION = os.getenv("GOOGLE_SITE_VERIFICATION", "")
    # Metodo alterno: archivo HTML de verificacion en la raiz del sitio.
    GOOGLE_SITE_VERIFICATION_HTML = os.getenv("GOOGLE_SITE_VERIFICATION_HTML", "")


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
