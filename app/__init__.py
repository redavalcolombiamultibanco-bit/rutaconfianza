"""Inicializacion principal de la aplicacion Ruta Confianza."""
from importlib import import_module
from pathlib import Path
import hmac
import json
import mimetypes
import os
import secrets
import threading
import time
from flask import Flask, Response, abort, has_request_context, jsonify, request, send_from_directory, session
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

db = SQLAlchemy()
migrate = Migrate()
socketio = SocketIO(async_mode="threading")


def _configure_database_pool(app):
    """Ajusta el pool para atender solicitudes concurrentes sin alterar SQLite en memoria."""
    database_url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    if database_url.get_backend_name() == "sqlite" and database_url.database in (None, "", ":memory:"):
        return

    options = dict(app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
    options.setdefault("pool_size", int(os.getenv("SQLALCHEMY_POOL_SIZE", "20")))
    options.setdefault("max_overflow", int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "30")))
    options.setdefault("pool_timeout", float(os.getenv("SQLALCHEMY_POOL_TIMEOUT", "30")))
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = options


def _instrument_database_pool(app):
    """Registra temporalmente ocupacion, rutas y antiguedad de conexiones activas."""
    pool = db.engine.pool
    pool_size = getattr(pool, "size", None)
    if pool_size is None:
        return
    max_overflow = getattr(pool, "_max_overflow", 0)
    capacity = pool_size() + max_overflow
    state = {"active": {}, "reported_thresholds": set(), "lock": threading.Lock()}

    def report(hypothesis_id, location, message, data):
        payload = {
            "sessionId": "render-500-error",
            "runId": "pre-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "msg": f"[DEBUG] {message}",
            "data": data,
        }
        app.logger.warning("DB_POOL_DIAGNOSTIC %s", json.dumps(payload, separators=(",", ":")))

    def request_label():
        if not has_request_context():
            return "background"
        label = request.path
        event_info = getattr(request, "event", None)
        if isinstance(event_info, dict) and event_info.get("message"):
            label = f"{label}:{event_info['message']}"
        return label

    report(
        "B",
        "app.create_app.pool",
        "database pool configured",
        {"pool_size": pool_size(), "max_overflow": max_overflow, "capacity": capacity},
    )

    # #region debug-point B:pool-checkout
    @event.listens_for(pool, "checkout")
    def _record_pool_checkout(dbapi_connection, connection_record, connection_proxy):
        now = time.monotonic()
        key = id(connection_record)
        label = request_label()
        thresholds = {max(1, int(capacity * ratio)) for ratio in (0.8, 0.9, 1.0)}
        with state["lock"]:
            state["active"][key] = {"started": now, "path": label}
            checked_out = len(state["active"])
            new_threshold = next(
                (threshold for threshold in sorted(thresholds) if checked_out >= threshold and threshold not in state["reported_thresholds"]),
                None,
            )
            if new_threshold is not None:
                state["reported_thresholds"].add(new_threshold)
                active = sorted(
                    (
                        {"path": item["path"], "age_ms": int((now - item["started"]) * 1000)}
                        for item in state["active"].values()
                    ),
                    key=lambda item: item["age_ms"],
                    reverse=True,
                )[:10]
            else:
                active = []
        if new_threshold is not None:
            report(
                "B",
                "sqlalchemy.pool.checkout",
                "database pool reached pressure threshold",
                {
                    "checked_out": checked_out,
                    "threshold": new_threshold,
                    "pool_size": pool_size(),
                    "max_overflow": max_overflow,
                    "active_oldest": active,
                },
            )
    # #endregion

    # #region debug-point C:pool-checkin
    @event.listens_for(pool, "checkin")
    def _record_pool_checkin(dbapi_connection, connection_record):
        now = time.monotonic()
        with state["lock"]:
            checked_out = len(state["active"])
            item = state["active"].pop(id(connection_record), None)
            if item is None:
                return
            age_ms = int((now - item["started"]) * 1000)
            should_report = age_ms >= 5000
        if should_report:
            hypothesis_id = "D" if item["path"] == "background" or item["path"].startswith("/socket.io") else "C"
            report(
                hypothesis_id,
                "sqlalchemy.pool.checkin",
                "database connection held for an extended period",
                {"path": item["path"], "age_ms": age_ms, "checked_out_before_return": checked_out},
            )
    # #endregion


def create_app(config_object=None):
    """Crea y configura la aplicacion Flask usando Application Factory."""
    app = Flask(__name__, instance_relative_config=False)
    # Primero cargamos una base segura para desarrollo y luego, si existe,
    # montamos encima la configuracion especifica del entorno actual.
    _load_default_config(app)
    if config_object:
        app.config.from_object(config_object)
    _configure_database_pool(app)
    # Estas rutas viven en la factory porque deben existir incluso si algun
    # blueprint aun no se ha cargado o se implementa despues.
    _register_health_routes(app)
    _register_pwa_routes(app)
    _register_seo_routes(app)
    _register_security_headers(app)
    _register_csrf(app)
    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(
        app,
        async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
        cors_allowed_origins=app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS"),
    )
    _register_blueprints(app)
    _register_template_globals(app)
    from app.sockets.events import register_socket_events
    register_socket_events(socketio)
    with app.app_context():
        # `create_all` cubre bases nuevas; el ajuste de esquema completa
        # columnas agregadas recientemente en SQLite local sin romper arranques.
        db.create_all()
        _ensure_runtime_schema()
        _ensure_default_admin_user(app)
        _instrument_database_pool(app)
    _programar_respaldo_automatico(app)
    return app


def _programar_respaldo_automatico(app):
    """Lanza un hilo que crea un respaldo cada cierto numero de horas.

    Trabaja en segundo plano para no depender de que alguien entre al panel.
    Cualquier fallo queda en el log: el respaldo nunca afecta a la aplicacion.
    """
    if app.config.get("DEBUG") and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        # Con el recargador de desarrollo activo solo programa el proceso hijo,
        # para no crear respaldos por duplicado.
        return

    from app.services.backups import respaldo_automatico_si_corresponde

    def ciclo():
        while True:
            try:
                with app.app_context():
                    respaldo_automatico_si_corresponde()
            except Exception:
                app.logger.exception("No fue posible crear el respaldo automatico")
            time.sleep(3600)

    threading.Thread(target=ciclo, name="respaldo-automatico", daemon=True).start()


def _register_security_headers(app):
    """Agrega cabeceras defensivas a todas las respuestas."""

    @app.after_request
    def _set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Se permite geolocalizacion (mapa en vivo) pero se bloquea lo demas.
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(self), camera=(), microphone=(), payment=()",
        )
        if not app.config.get("DEBUG"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # El HTML nunca se cachea: asi el navegador siempre carga el JS/CSS
        # versionados mas recientes y no se queda con un app.js viejo.
        content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


# Metodos que modifican datos: solo ellos exigen el token.
CSRF_METODOS_PROTEGIDOS = {"POST", "PUT", "PATCH", "DELETE"}
# El transporte de sondeo de Socket.IO tambien usa POST, pero no son formularios.
CSRF_RUTAS_EXENTAS = ("/socket.io/",)


def _register_csrf(app):
    """Exige un token ligado a la sesion en los envios de formularios.

    El token viaja en el campo oculto `csrf_token` o en la cabecera
    `X-CSRFToken`, y se compara con el que esta guardado en la sesion del
    navegador. Un sitio externo no puede leer ese token, asi que no logra
    reenviar un formulario en nombre del usuario.
    """

    @app.before_request
    def _asegurar_csrf_token():
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)

    @app.before_request
    def _validar_csrf_token():
        if request.method not in CSRF_METODOS_PROTEGIDOS:
            return None
        if request.path.startswith(CSRF_RUTAS_EXENTAS):
            return None
        esperado = session.get("csrf_token") or ""
        recibido = request.headers.get("X-CSRFToken") or request.form.get("csrf_token") or ""
        # Se comparan bytes para evitar fallos con caracteres no ASCII enviados
        # a proposito por un atacante.
        if esperado and hmac.compare_digest(esperado.encode("utf-8"), str(recibido).encode("utf-8")):
            return None
        app.logger.warning("Envio bloqueado por falta de token CSRF: %s %s", request.method, request.path)
        abort(400, description="La sesion del formulario expiro. Recarga la pagina e intenta de nuevo.")

    @app.context_processor
    def _inyectar_csrf():
        return {"csrf_token": session.get("csrf_token", "")}


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
        ("/rutas-escolares-yopal", "0.9"),
        ("/rutas-escolares-casanare", "0.9"),
        ("/tracking/", "0.7"),
        ("/conductor/ingresar", "0.7"),
        ("/terminos", "0.4"),
    )

    @app.get("/robots.txt")
    def robots_txt():
        lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /tracking/ruta",
            "Disallow: /tracking/api/",
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

    verification_html = (app.config.get("GOOGLE_SITE_VERIFICATION_HTML") or "").strip()
    if verification_html:
        verification_path = f"/{verification_html.lstrip('/')}"
        verification_body = f"google-site-verification: {verification_html}"

        def google_site_verification_file():
            return Response(verification_body, mimetype="text/plain")

        endpoint_name = f"google_site_verification_{verification_html.replace('.', '_')}"
        app.add_url_rule(verification_path, endpoint_name, google_site_verification_file)


def _socket_cors_origins():
    """Origenes permitidos para Socket.IO; sin configurar queda en mismo origen."""
    raw = os.getenv("SOCKETIO_CORS_ALLOWED_ORIGINS", "").strip()
    if not raw or raw in {"*", "self"}:
        return None
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


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
        SOCKETIO_CORS_ALLOWED_ORIGINS=_socket_cors_origins(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
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
        GOOGLE_SITE_VERIFICATION_HTML=os.getenv("GOOGLE_SITE_VERIFICATION_HTML", ""),
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
            # Permite crecer el proyecto por modulos sin bloquear el inicio si
            # un blueprint todavia no fue creado o esta temporalmente ausente.
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


def _ensure_default_admin_user(app):
    """Crea un acceso administrativo inicial de forma controlada.

    - Si existen ADMIN_USERNAME y ADMIN_PASSWORD se usa esa credencial.
    - En produccion NUNCA se crea un usuario con clave conocida; solo avisa.
    - En desarrollo local se mantiene el usuario demo para no perder acceso.
    """
    from app.models import AdminUser

    env_username = os.getenv("ADMIN_USERNAME", "").strip()
    env_password = os.getenv("ADMIN_PASSWORD", "")

    if env_username and env_password:
        if AdminUser.query.filter_by(username=env_username).first() is None:
            db.session.add(
                AdminUser(
                    username=env_username,
                    nombre=env_username,
                    password_hash=generate_password_hash(env_password),
                    activo=True,
                )
            )
            db.session.commit()
        return

    es_produccion = not app.config.get("DEBUG", False)
    if es_produccion:
        if AdminUser.query.count() == 0:
            app.logger.warning(
                "No hay administradores y no se definieron ADMIN_USERNAME/ADMIN_PASSWORD. "
                "Crea un administrador para poder entrar al panel."
            )
        return

    default_username = "julio"
    default_password = "julio21200521"
    existing_user = AdminUser.query.filter_by(username=default_username).first()
    if existing_user is not None:
        return

    # Solo se crea si no existe; asi evitamos duplicados o sobreescribir claves
    # cuando la base ya fue inicializada manualmente.
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

    # Este mapa funciona como una migracion ligera para entornos locales donde
    # la base ya existe pero aun no se ejecutaron migraciones formales.
    schema_updates = {
        "rutas": {
            "ciudad": "ALTER TABLE rutas ADD COLUMN ciudad VARCHAR(80)",
            "origen_latitud": "ALTER TABLE rutas ADD COLUMN origen_latitud FLOAT",
            "origen_longitud": "ALTER TABLE rutas ADD COLUMN origen_longitud FLOAT",
            "destino_direccion": "ALTER TABLE rutas ADD COLUMN destino_direccion VARCHAR(180)",
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
                    # Solo se agrega la columna faltante; si ya existe no se toca
                    # nada para mantener el arranque idempotente.
                    connection.execute(text(statement))
