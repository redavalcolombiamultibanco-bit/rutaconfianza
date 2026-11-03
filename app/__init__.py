"""Inicializacion principal de la aplicacion Ruta Confianza."""
from importlib import import_module
from pathlib import Path
import mimetypes
import os
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash

db = SQLAlchemy()
migrate = Migrate()
socketio = SocketIO(async_mode="threading", cors_allowed_origins="*")


def create_app(config_object=None):
    """Crea y configura la aplicacion Flask usando Application Factory."""
    app = Flask(__name__, instance_relative_config=False)
    _load_default_config(app)
    if config_object:
        app.config.from_object(config_object)
    _register_health_routes(app)
    _register_pwa_routes(app)
    _register_seo_routes(app)
    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(
        app,
        async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
        cors_allowed_origins=app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "*"),
    )
    _register_blueprints(app)
    _register_template_globals(app)
    from app.sockets.events import register_socket_events
    register_socket_events(socketio)
    with app.app_context():
        db.create_all()
        _ensure_runtime_schema()
        _ensure_default_admin_user()
    return app


def _register_health_routes(app):
    """Expone un endpoint ligero para verificacion y keep-alive."""

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "app": app.config.get("APP_NAME", "Ruta Confianza")})


def _register_pwa_routes(app):
    """Publica el service worker desde la raiz para que controle todo el sitio."""
    mimetypes.add_type("application/manifest+json", ".webmanifest")

    @app.get("/sw.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "js/sw.js")
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response


def _seo_base_url(app):
    """Devuelve la URL publica del sitio: configurada o la del dominio de la peticion."""
    configured = (app.config.get("SITE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


def _register_seo_routes(app):
    """Expone robots.txt y sitemap.xml para que los buscadores encuentren el sitio."""
    # Paginas publicas indexables: (ruta, prioridad).
    public_pages = (
        ("/", "1.0"),
        ("/directorio", "0.8"),
        ("/tracking/", "0.7"),
        ("/conductor/ingresar", "0.7"),
    )

    @app.get("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /tracking/ruta/",
            "",
            f"Sitemap: {_seo_base_url(app)}/sitemap.xml",
            "",
        ]
        return Response("\n".join(lines), mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap_xml():
        base = _seo_base_url(app)
        entries = "\n".join(
            "  <url>\n"
            f"    <loc>{base}{path}</loc>\n"
            "    <changefreq>weekly</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
            for path, priority in public_pages
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{entries}\n"
            "</urlset>\n"
        )
        return Response(xml, mimetype="application/xml")


def _load_default_config(app):
    """Carga una configuracion minima para desarrollo local."""
    base_dir = Path(app.root_path).resolve().parent
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "ruta-confianza-dev-key"),
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", f"sqlite:///{base_dir / 'ruta_exacta.db'}"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        JSON_AS_ASCII=False,
        JSON_SORT_KEYS=False,
        MAX_CONTENT_LENGTH=64 * 1024 * 1024,
        UPLOAD_FOLDER=os.getenv("UPLOAD_FOLDER", str(Path(app.root_path) / "static" / "uploads")),
        ALLOWED_IMAGE_EXTENSIONS={"png", "jpg", "jpeg", "webp", "gif"},
        BACKUP_FOLDER=os.getenv("BACKUP_FOLDER", str(base_dir / "respaldos")),
        SOCKETIO_ASYNC_MODE=os.getenv("SOCKETIO_ASYNC_MODE", "threading"),
        SOCKETIO_CORS_ALLOWED_ORIGINS=os.getenv("SOCKETIO_CORS_ALLOWED_ORIGINS", "*"),
        APP_NAME="Ruta Confianza",
        COMPANY_NAME="Ruta Confianza",
        BRAND_LOGO="images/logo-ruta-confianza.jpg",
        MAP_DEFAULT_LAT=float(os.getenv("MAP_DEFAULT_LAT", "5.33775")),
        MAP_DEFAULT_LNG=float(os.getenv("MAP_DEFAULT_LNG", "-72.39586")),
        MAP_DEFAULT_ZOOM=int(os.getenv("MAP_DEFAULT_ZOOM", "14")),
        CONTACT_LOCATION=os.getenv("CONTACT_LOCATION", "Yopal, Casanare, Colombia"),
        CONTACT_HOURS=os.getenv("CONTACT_HOURS", "Lunes a sabado, 6:00 a.m. a 6:00 p.m."),
        CONTACT_PHONE=os.getenv("CONTACT_PHONE", ""),
        CONTACT_WHATSAPP=os.getenv("CONTACT_WHATSAPP", "3013444105"),
        CONTACT_EMAIL=os.getenv("CONTACT_EMAIL", ""),
        SITE_URL=os.getenv("SITE_URL") or os.getenv("RENDER_EXTERNAL_URL", ""),
        SITE_DESCRIPTION=os.getenv(
            "SITE_DESCRIPTION",
            "Ruta Confianza es la plataforma de transporte escolar con seguimiento GPS en vivo, "
            "control de asistencia y comunicacion directa con las familias en Yopal, Casanare.",
        ),
        SITE_KEYWORDS=os.getenv(
            "SITE_KEYWORDS",
            "transporte escolar Yopal, ruta escolar Casanare, seguimiento GPS escolar, "
            "portal de padres ruta escolar, app conductor ruta escolar",
        ),
        SOCIAL_IMAGE=os.getenv("SOCIAL_IMAGE", "images/portada-ruta-escolar-1.jpg"),
        GOOGLE_SITE_VERIFICATION=os.getenv("GOOGLE_SITE_VERIFICATION", ""),
    )


def _register_blueprints(app):
    """Registra blueprints disponibles sin romper el arranque si aun no existen."""
    blueprints = (
        ("app.comercial.routes", "comercial_bp"),
        ("app.admin.routes", "admin_bp"),
        ("app.conductor.routes", "conductor_bp"),
        ("app.tracking.routes", "tracking_bp"),
    )
    for module_name, blueprint_name in blueprints:
        try:
            module = import_module(module_name)
            blueprint = getattr(module, blueprint_name, None)
            if blueprint is not None:
                app.register_blueprint(blueprint)
        except ImportError:
            app.logger.info("Blueprint pendiente de implementacion: %s", module_name)


def _register_template_globals(app):
    """Expone catalogos reutilizables (preguntas de seguridad) a las plantillas."""
    from app.services.security import PREGUNTAS_SUGERIDAS

    @app.context_processor
    def inject_security_questions():
        return {"preguntas_seguridad": PREGUNTAS_SUGERIDAS}

    @app.context_processor
    def inject_site_base_url():
        return {"site_base_url": _seo_base_url(app)}


def _ensure_default_admin_user():
    """Garantiza un acceso administrativo inicial para desarrollo local."""
    from app.models import AdminUser

    default_username = "julio"
    default_password = "julio21200521"
    existing_user = AdminUser.query.filter_by(username=default_username).first()
    if existing_user is not None:
        return

    db.session.add(
        AdminUser(
            username=default_username,
            nombre="Julio",
            password_hash=generate_password_hash(default_password),
            activo=True,
        )
    )
    db.session.commit()


def _ensure_runtime_schema():
    """Agrega columnas nuevas en SQLite local sin exigir migraciones manuales."""
    if db.engine.dialect.name != "sqlite":
        return

    schema_updates = {
        "rutas": {
            "ciudad": "ALTER TABLE rutas ADD COLUMN ciudad VARCHAR(80)",
            "origen_latitud": "ALTER TABLE rutas ADD COLUMN origen_latitud FLOAT",
            "origen_longitud": "ALTER TABLE rutas ADD COLUMN origen_longitud FLOAT",
            "destino_latitud": "ALTER TABLE rutas ADD COLUMN destino_latitud FLOAT",
            "destino_longitud": "ALTER TABLE rutas ADD COLUMN destino_longitud FLOAT",
        },
        "estudiantes": {
            "direccion": "ALTER TABLE estudiantes ADD COLUMN direccion VARCHAR(180)",
            "barrio": "ALTER TABLE estudiantes ADD COLUMN barrio VARCHAR(80)",
            "ciudad": "ALTER TABLE estudiantes ADD COLUMN ciudad VARCHAR(80)",
            "referencia_direccion": "ALTER TABLE estudiantes ADD COLUMN referencia_direccion VARCHAR(180)",
            "latitud": "ALTER TABLE estudiantes ADD COLUMN latitud FLOAT",
            "longitud": "ALTER TABLE estudiantes ADD COLUMN longitud FLOAT",
            "orden_parada": "ALTER TABLE estudiantes ADD COLUMN orden_parada INTEGER",
        },
        "admin_users": {
            "pregunta_seguridad": "ALTER TABLE admin_users ADD COLUMN pregunta_seguridad VARCHAR(180)",
            "respuesta_seguridad_hash": "ALTER TABLE admin_users ADD COLUMN respuesta_seguridad_hash VARCHAR(255)",
        },
        "conductores": {
            "pregunta_seguridad": "ALTER TABLE conductores ADD COLUMN pregunta_seguridad VARCHAR(180)",
            "respuesta_seguridad_hash": "ALTER TABLE conductores ADD COLUMN respuesta_seguridad_hash VARCHAR(255)",
        },
    }

    inspector = inspect(db.engine)
    with db.engine.begin() as connection:
        for table_name, statements in schema_updates.items():
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, statement in statements.items():
                if column_name not in existing_columns:
                    connection.execute(text(statement))
