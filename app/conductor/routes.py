"""Rutas del panel del conductor."""
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from app import db, socketio
from app.models import Asistencia, Conductor, Estudiante, MensajeChat, Ruta
from app.services.geocoding import geocode_colombia_address
from app.services.school_catalog import get_municipalities, load_school_catalog
from app.services.rate_limit import (
    BLOQUEO_ACCESO_SEGUNDOS,
    MAX_FALLOS_ACCESO,
    MAX_INTENTOS_RECUPERACION,
    VENTANA_RECUPERACION_SEGUNDOS,
    bloqueo_restante,
    cliente_ip,
    demasiadas_peticiones,
    espera_legible,
    olvidar,
    registrar_fallo,
)
from app.services.security import aplicar_recuperacion, verificar_respuesta

conductor_bp = Blueprint("conductor_bp", __name__, url_prefix="/conductor")


@conductor_bp.route("/ingresar", methods=["GET", "POST"])
def ingresar():
    conductor = _conductor_actual()
    if conductor:
        return redirect(url_for("conductor_bp.dashboard"))

    if request.method == "POST":
        clave = f"acceso-conductor:{cliente_ip()}"
        espera = bloqueo_restante(clave)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("conductor/ingresar.html"), 429

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conductor = Conductor.query.filter_by(email=email).first()
        if (
            conductor is None
            or not conductor.activo
            or not conductor.password_hash
            or not check_password_hash(conductor.password_hash, password)
        ):
            espera = registrar_fallo(clave, MAX_FALLOS_ACCESO, BLOQUEO_ACCESO_SEGUNDOS)
            if espera:
                flash(
                    f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                    "danger",
                )
            else:
                flash("Correo o contraseña incorrectos, o cuenta inactiva.", "danger")
        else:
            olvidar(clave)
            session.clear()
            session["conductor_id"] = conductor.id
            return redirect(url_for("conductor_bp.dashboard"))

    return render_template("conductor/ingresar.html")


@conductor_bp.route("/salir", methods=["POST"])
def salir():
    session.pop("conductor_id", None)
    return redirect(url_for("conductor_bp.ingresar"))


@conductor_bp.route("/recuperar-password", methods=["GET", "POST"])
def recuperar_password():
    """Restablece la contrasena del conductor mediante la pregunta de seguridad."""
    if _conductor_actual() is not None:
        return redirect(url_for("conductor_bp.dashboard"))

    identificador = ""
    if request.method == "POST":
        if demasiadas_peticiones(
            f"recuperacion-conductor:{cliente_ip()}",
            MAX_INTENTOS_RECUPERACION,
            VENTANA_RECUPERACION_SEGUNDOS,
        ):
            flash("Demasiados intentos seguidos. Espera unos minutos y vuelve a intentarlo.", "danger")
            return render_template("conductor/recuperar_password.html", paso="identificar", identificador=""), 429
        accion = request.form.get("accion", "buscar")
        identificador = request.form.get("email", "").strip().lower()
        conductor = Conductor.query.filter_by(email=identificador).first() if identificador else None

        if conductor is None or not conductor.activo or not conductor.tiene_recuperacion:
            flash(
                "No encontramos una cuenta activa con recuperacion configurada para ese correo.",
                "warning",
            )
            return render_template("conductor/recuperar_password.html", paso="identificar", identificador=identificador)

        if accion != "restablecer":
            return _render_verificacion_conductor(conductor, identificador)

        respuesta = request.form.get("respuesta", "")
        password = request.form.get("password", "")
        confirmacion = request.form.get("password_confirmacion", "")

        if not verificar_respuesta(conductor.respuesta_seguridad_hash, respuesta):
            flash("La respuesta de seguridad no coincide. Intenta de nuevo.", "danger")
            return _render_verificacion_conductor(conductor, identificador)
        if len(password) < 8:
            flash("La nueva contrasena debe tener al menos 8 caracteres.", "warning")
            return _render_verificacion_conductor(conductor, identificador)
        if password != confirmacion:
            flash("Las dos contrasenas no coinciden.", "warning")
            return _render_verificacion_conductor(conductor, identificador)

        conductor.password_hash = generate_password_hash(password)
        db.session.commit()
        flash("Contrasena restablecida correctamente. Ya puedes ingresar con tu nueva clave.", "success")
        return redirect(url_for("conductor_bp.ingresar"))

    return render_template("conductor/recuperar_password.html", paso="identificar", identificador=identificador)


def _render_verificacion_conductor(conductor, identificador):
    return render_template(
        "conductor/recuperar_password.html",
        paso="verificar",
        identificador=identificador,
        pregunta=conductor.pregunta_seguridad,
        nombre=conductor.nombre_completo,
    )


@conductor_bp.route("/")
def dashboard():
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    school_catalog = load_school_catalog()
    rutas = (
        Ruta.query.filter_by(conductor_id=conductor.id, activa=True)
        .order_by(Ruta.ciudad.asc(), Ruta.nombre.asc())
        .all()
    )
    ruta_id = request.args.get("ruta_id", type=int)
    if ruta_id:
        ruta = Ruta.query.filter_by(
            id=ruta_id,
            conductor_id=conductor.id,
            activa=True,
        ).first_or_404()
    else:
        ruta = rutas[0] if rutas else None
    estudiantes = (
        sorted(
            ruta.estudiantes,
            key=lambda estudiante: (
                estudiante.orden_parada is None,
                estudiante.orden_parada if estudiante.orden_parada is not None else 9999,
                estudiante.nombres.lower(),
            ),
        )
        if ruta else []
    )
    asistencias_hoy = {}
    mensajes_recientes = []
    if ruta:
        asistencias_hoy = {
            asistencia.estudiante_id: asistencia
            for asistencia in Asistencia.query.filter_by(
                ruta_id=ruta.id,
                fecha_operacion=date.today(),
            ).all()
        }
        mensajes_recientes = list(
            reversed(
                MensajeChat.query.filter_by(ruta_id=ruta.id)
                .order_by(MensajeChat.created_at.desc())
                .limit(20)
                .all()
            )
        )
    return render_template(
        "conductor/dashboard.html",
        conductor=conductor,
        rutas=rutas,
        ruta=ruta,
        estudiantes=estudiantes,
        asistencias_hoy=asistencias_hoy,
        mensajes_recientes=mensajes_recientes,
        map_defaults={
            "lat": ruta.latitud_actual if ruta and ruta.latitud_actual is not None else current_app.config["MAP_DEFAULT_LAT"],
            "lng": ruta.longitud_actual if ruta and ruta.longitud_actual is not None else current_app.config["MAP_DEFAULT_LNG"],
            "zoom": current_app.config["MAP_DEFAULT_ZOOM"],
        },
        municipios_catalogo=get_municipalities(),
        colegios_catalogo=school_catalog,
    )


@conductor_bp.route("/ruta/<int:ruta_id>/actualizar", methods=["POST"])
def actualizar_ruta(ruta_id):
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    ruta = Ruta.query.filter_by(id=ruta_id, conductor_id=conductor.id).first_or_404()
    nuevo_pin = (request.form.get("pin_acceso", "").strip().upper() or ruta.pin_acceso)
    ruta_con_pin = Ruta.query.filter(Ruta.pin_acceso == nuevo_pin, Ruta.id != ruta.id).first()
    if ruta_con_pin is not None:
        flash("Ese PIN ya esta siendo usado por otra ruta. Elige uno diferente para las familias.", "warning")
        return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta.id))

    ruta.nombre = request.form.get("nombre", "").strip()
    ruta.pin_acceso = nuevo_pin
    ruta.ciudad = request.form.get("ciudad", "").strip() or None
    ruta.descripcion = request.form.get("descripcion", "").strip() or None
    ruta.origen = request.form.get("origen", "").strip() or None
    ruta.destino = _resolve_school_form_value("destino")
    ruta.destino_direccion = request.form.get("destino_direccion", "").strip() or None
    ruta.horario_salida = request.form.get("horario_salida", "").strip() or None
    ruta.horario_regreso = request.form.get("horario_regreso", "").strip() or None
    _hydrate_route_geodata(ruta)
    return _commit_driver_changes(
        "La informacion general de la ruta fue actualizada.",
        "No fue posible actualizar la ruta.",
        ruta.id,
        notify_route_ids=[ruta],
    )


@conductor_bp.route("/perfil/actualizar", methods=["POST"])
def actualizar_perfil():
    """Permite al conductor editar sus datos, su buseta, su foto y su contraseña."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    ruta_id = request.form.get("ruta_id", type=int)
    nombres = request.form.get("nombres", "").strip()
    apellidos = request.form.get("apellidos", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not nombres or not apellidos or not email:
        flash("Nombres, apellidos y correo son obligatorios.", "warning")
        return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    password_actual = request.form.get("password_actual", "")
    password_nueva = request.form.get("password_nueva", "")
    if password_nueva:
        if len(password_nueva) < 8:
            flash("La nueva contraseña debe tener al menos 8 caracteres.", "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))
        if not conductor.password_hash or not check_password_hash(conductor.password_hash, password_actual):
            flash("La contraseña actual no coincide. No se cambio la contraseña.", "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    error_recuperacion = aplicar_recuperacion(
        conductor,
        request.form.get("pregunta_seguridad"),
        request.form.get("respuesta_seguridad"),
    )
    if error_recuperacion:
        flash(error_recuperacion, "warning")
        return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    foto = request.files.get("foto")
    if foto and foto.filename:
        try:
            conductor.foto_url = _guardar_foto_buseta(foto)
        except ValueError as error:
            flash(str(error), "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))
    else:
        foto_url = request.form.get("foto_url", "").strip()
        if foto_url:
            conductor.foto_url = foto_url

    conductor.nombres = nombres
    conductor.apellidos = apellidos
    conductor.email = email
    conductor.documento = request.form.get("documento", "").strip() or conductor.documento
    conductor.telefono = request.form.get("telefono", "").strip() or conductor.telefono
    conductor.ciudad = request.form.get("ciudad", "").strip() or conductor.ciudad
    conductor.nombre_vehiculo = request.form.get("nombre_vehiculo", "").strip() or conductor.nombre_vehiculo
    conductor.placa_vehiculo = request.form.get("placa_vehiculo", "").strip().upper() or conductor.placa_vehiculo
    conductor.capacidad_vehiculo = request.form.get("capacidad_vehiculo", type=int) or conductor.capacidad_vehiculo
    conductor.color_vehiculo = request.form.get("color_vehiculo", "").strip() or None
    conductor.descripcion_publica = request.form.get("descripcion_publica", "").strip() or None
    if password_nueva:
        conductor.password_hash = generate_password_hash(password_nueva)

    return _commit_driver_changes(
        "Tu perfil y tu buseta fueron actualizados correctamente.",
        "No fue posible actualizar tu perfil. Revisa documento, correo y placa.",
        ruta_id,
    )


@conductor_bp.route("/ruta/<int:ruta_id>/estudiantes/crear", methods=["POST"])
def crear_estudiante(ruta_id):
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    ruta = Ruta.query.filter_by(id=ruta_id, conductor_id=conductor.id, activa=True).first_or_404()
    estudiante = Estudiante(
        ruta_id=ruta.id,
        nombres=request.form.get("nombres", "").strip(),
        apellidos=request.form.get("apellidos", "").strip(),
        documento=request.form.get("documento", "").strip() or None,
        institucion=_resolve_school_form_value("institucion"),
        grado=request.form.get("grado", "").strip() or None,
        direccion=request.form.get("direccion", "").strip() or None,
        barrio=request.form.get("barrio", "").strip() or None,
        ciudad=request.form.get("ciudad", "").strip() or ruta.ciudad,
        referencia_direccion=request.form.get("referencia_direccion", "").strip() or None,
        orden_parada=request.form.get("orden_parada", type=int),
        acudiente_nombre=request.form.get("acudiente_nombre", "").strip(),
        acudiente_telefono=request.form.get("acudiente_telefono", "").strip(),
        acudiente_email=request.form.get("acudiente_email", "").strip().lower() or None,
        activo=_form_bool("activo", default=True),
    )
    estudiante.ruta = ruta
    _hydrate_student_geodata(estudiante)
    db.session.add(estudiante)
    return _commit_driver_changes(
        "Nuevo estudiante agregado correctamente.",
        "No fue posible crear el estudiante.",
        ruta.id,
        notify_route_ids=[ruta],
    )


@conductor_bp.route("/estudiantes/<int:estudiante_id>/actualizar", methods=["POST"])
def actualizar_estudiante(estudiante_id):
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    estudiante = (
        Estudiante.query.join(Ruta, Estudiante.ruta_id == Ruta.id)
        .filter(Estudiante.id == estudiante_id, Ruta.conductor_id == conductor.id)
        .first_or_404()
    )
    estudiante.nombres = request.form.get("nombres", "").strip()
    estudiante.apellidos = request.form.get("apellidos", "").strip()
    estudiante.documento = request.form.get("documento", "").strip() or None
    estudiante.institucion = _resolve_school_form_value("institucion")
    estudiante.grado = request.form.get("grado", "").strip() or None
    estudiante.direccion = request.form.get("direccion", "").strip() or None
    estudiante.barrio = request.form.get("barrio", "").strip() or None
    estudiante.ciudad = request.form.get("ciudad", "").strip() or estudiante.ruta.ciudad
    estudiante.referencia_direccion = request.form.get("referencia_direccion", "").strip() or None
    estudiante.orden_parada = request.form.get("orden_parada", type=int)
    estudiante.acudiente_nombre = request.form.get("acudiente_nombre", "").strip()
    estudiante.acudiente_telefono = request.form.get("acudiente_telefono", "").strip()
    estudiante.acudiente_email = request.form.get("acudiente_email", "").strip().lower() or None
    estudiante.activo = _form_bool("activo", default=True)
    _hydrate_student_geodata(estudiante)
    return _commit_driver_changes(
        f"Parada actualizada para {estudiante.nombre_completo}.",
        "No fue posible actualizar el estudiante.",
        estudiante.ruta_id,
        notify_route_ids=[estudiante.ruta],
    )


@conductor_bp.route("/estudiantes/<int:estudiante_id>/eliminar", methods=["POST"])
def eliminar_estudiante(estudiante_id):
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    estudiante = (
        Estudiante.query.join(Ruta, Estudiante.ruta_id == Ruta.id)
        .filter(Estudiante.id == estudiante_id, Ruta.conductor_id == conductor.id)
        .first_or_404()
    )
    ruta = estudiante.ruta
    ruta_id = estudiante.ruta_id
    nombre_estudiante = estudiante.nombre_completo
    db.session.delete(estudiante)
    return _commit_driver_changes(
        f"Estudiante {nombre_estudiante} eliminado correctamente.",
        "No fue posible eliminar el estudiante.",
        ruta_id,
        notify_route_ids=[ruta],
    )


@conductor_bp.route("/estudiantes/<int:estudiante_id>/ubicacion", methods=["POST"])
def ubicar_estudiante(estudiante_id):
    """Fija o corrige en el mapa la ubicacion de la casa de un estudiante."""
    conductor = _conductor_actual()
    if conductor is None:
        return jsonify({"ok": False, "error": "Tu sesion de conductor no es valida."}), 403

    estudiante = (
        Estudiante.query.join(Ruta, Estudiante.ruta_id == Ruta.id)
        .filter(Estudiante.id == estudiante_id, Ruta.conductor_id == conductor.id)
        .first_or_404()
    )

    latitud = request.form.get("lat", type=float)
    longitud = request.form.get("lng", type=float)
    if (
        latitud is None
        or longitud is None
        or not -90 <= latitud <= 90
        or not -180 <= longitud <= 180
    ):
        return jsonify({"ok": False, "error": "Las coordenadas no son validas."}), 400

    estudiante.latitud = latitud
    estudiante.longitud = longitud
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "No fue posible guardar la ubicacion de la casita."}), 500

    _notify_route_plan_updates([estudiante.ruta])
    return jsonify(
        {
            "ok": True,
            "estudiante_id": estudiante.id,
            "nombre": estudiante.nombre_completo,
            "latitud": latitud,
            "longitud": longitud,
        }
    )


@conductor_bp.route("/ruta/<int:ruta_id>/chat/limpiar", methods=["POST"])
def limpiar_chat(ruta_id):
    """Vacía el chat de la ruta para que el conductor empiece con la bandeja limpia."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    ruta = Ruta.query.filter_by(id=ruta_id, conductor_id=conductor.id).first_or_404()
    try:
        MensajeChat.query.filter_by(ruta_id=ruta.id).delete(synchronize_session=False)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("No fue posible limpiar el chat de la ruta.", "danger")
    else:
        # Se avisa a la sala para que las familias tambien vacien su chat.
        socketio.emit("chat:cleared", {"ruta_id": ruta.id}, to=ruta.socket_room)
        flash("El chat de la ruta quedó limpio.", "success")
    return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta.id))


def _conductor_actual():
    conductor_id = session.get("conductor_id")
    if conductor_id is None:
        return None
    conductor = db.session.get(Conductor, conductor_id)
    if conductor is None or not conductor.activo:
        session.pop("conductor_id", None)
        return None
    return conductor


def _form_bool(field_name, default=False):
    raw_value = request.form.get(field_name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "on", "yes", "si"}


def _guardar_foto_buseta(archivo):
    """Guarda la foto de la buseta en static/uploads y devuelve su ruta publica."""
    nombre_original = secure_filename(archivo.filename)
    extension = nombre_original.rsplit(".", 1)[-1].lower() if "." in nombre_original else ""
    if extension not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        raise ValueError("La foto de la buseta debe ser una imagen (jpg, png, webp o gif).")

    carpeta = Path(current_app.config["UPLOAD_FOLDER"])
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre_archivo = f"buseta-{uuid4().hex}.{extension}"
    archivo.save(carpeta / nombre_archivo)
    return url_for("static", filename=f"uploads/{nombre_archivo}")


def _resolve_school_form_value(field_name):
    custom_value = request.form.get(f"{field_name}_custom", "").strip()
    if custom_value:
        return custom_value
    selected_value = request.form.get(field_name, "").strip()
    return selected_value or None


def _commit_driver_changes(success_message, error_message, ruta_id, notify_route_ids=None):
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(error_message, "danger")
    else:
        _notify_route_plan_updates(notify_route_ids)
        flash(success_message, "success")
    return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))


def _notify_route_plan_updates(route_ids):
    if not route_ids:
        return
    unique_route_ids = set()
    for route_ref in route_ids:
        if isinstance(route_ref, Ruta):
            if route_ref.id:
                unique_route_ids.add(route_ref.id)
            continue
        if route_ref:
            unique_route_ids.add(route_ref)
    if not unique_route_ids:
        return
    rutas = Ruta.query.filter(Ruta.id.in_(unique_route_ids)).all()
    for ruta in rutas:
        socketio.emit(
            "tracking:plan_updated",
            {
                "ruta_id": ruta.id,
                "updated_at": datetime.utcnow().isoformat(),
            },
            to=ruta.socket_room,
        )


def _hydrate_route_geodata(ruta):
    if ruta.origen:
        origen = geocode_colombia_address(ruta.origen, ruta.ciudad, "Casanare", "Colombia")
        if origen:
            ruta.origen_latitud = origen["latitud"]
            ruta.origen_longitud = origen["longitud"]
        else:
            ruta.origen_latitud = None
            ruta.origen_longitud = None
            flash("No se pudo ubicar automaticamente el origen de la ruta.", "warning")
    else:
        ruta.origen_latitud = None
        ruta.origen_longitud = None

    destino_texto = ruta.destino_direccion or ruta.destino
    if destino_texto:
        destino = geocode_colombia_address(destino_texto, ruta.ciudad, "Casanare", "Colombia")
        if destino:
            ruta.destino_latitud = destino["latitud"]
            ruta.destino_longitud = destino["longitud"]
        else:
            ruta.destino_latitud = None
            ruta.destino_longitud = None
            flash(
                "No se pudo ubicar automaticamente el destino de la ruta. "
                "Revisa la direccion exacta del colegio.",
                "warning",
            )
    else:
        ruta.destino_latitud = None
        ruta.destino_longitud = None


def _hydrate_student_geodata(estudiante):
    if not estudiante.direccion:
        estudiante.latitud = None
        estudiante.longitud = None
        return

    geodata = geocode_colombia_address(
        estudiante.direccion,
        estudiante.barrio,
        estudiante.ciudad or (estudiante.ruta.ciudad if estudiante.ruta else None),
        "Casanare",
        "Colombia",
    )
    if geodata:
        estudiante.latitud = geodata["latitud"]
        estudiante.longitud = geodata["longitud"]
        return

    estudiante.latitud = None
    estudiante.longitud = None
    flash(
        f"No se pudo ubicar sola la direccion de {estudiante.nombre_completo}. "
        "Usa 'Ubicar casita en el mapa' para ponerla manualmente.",
        "warning",
    )
