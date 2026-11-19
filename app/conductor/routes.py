"""Rutas del panel del conductor.

Concentra todo el flujo del conductor: ingresar/salir, recuperar contrasena,
el dashboard con su ruta y la asistencia del dia, la edicion de la ruta y de
los estudiantes, el chat de la ruta y los avisos en tiempo real (SocketIO)
que reciben las familias.
"""
from datetime import date, datetime
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash
from app import db, socketio
from app.models import Asistencia, Conductor, Estudiante, MensajeChat, Ruta
from app.services.geocoding import geocode_colombia_address
from app.services.school_catalog import get_municipalities, load_school_catalog
from app.services.uploads import guardar_foto_vehiculo
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

# Blueprint con prefijo /conductor: agrupa todas las vistas del panel del conductor.
conductor_bp = Blueprint("conductor_bp", __name__, url_prefix="/conductor")


# GET muestra el formulario de acceso; POST valida las credenciales y abre sesion.
# Entrada: form email/password. Salida: render_template o redirect al dashboard.
# Acceso publico; si ya hay sesion de conductor se redirige sin pedir credenciales.
@conductor_bp.route("/ingresar", methods=["GET", "POST"])
def ingresar():
    """Login del conductor por correo y contraseña.

    Aplica bloqueo temporal por IP despues de varios fallos para frenar
    fuerza bruta. Al acertar se guarda `conductor_id` en la sesion.
    """
    # Evita pedir credenciales si el conductor ya tiene una sesion valida.
    conductor = _conductor_actual()
    if conductor:
        return redirect(url_for("conductor_bp.dashboard"))

    if request.method == "POST":
        # Clave de rate limit por IP: frena la fuerza bruta contra el login.
        clave = f"acceso-conductor:{cliente_ip()}"
        espera = bloqueo_restante(clave)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("conductor/ingresar.html"), 429

        # Normaliza el correo a minusculas para compararlo de forma uniforme.
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conductor = Conductor.query.filter_by(email=email).first()
        # Falla si la cuenta no existe, esta inactiva, no tiene clave o la
        # contrasena no coincide con el hash guardado.
        if (
            conductor is None
            or not conductor.activo
            or not conductor.password_hash
            or not check_password_hash(conductor.password_hash, password)
        ):
            # Registra el fallo; si supera el maximo queda bloqueado un rato.
            espera = registrar_fallo(clave, MAX_FALLOS_ACCESO, BLOQUEO_ACCESO_SEGUNDOS)
            if espera:
                flash(
                    f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                    "danger",
                )
            else:
                flash("Correo o contraseña incorrectos, o cuenta inactiva.", "danger")
        else:
            # Credenciales correctas: olvida los fallos y crea la sesion.
            # session.clear() descarta una sesion previa (por ejemplo la de familia).
            olvidar(clave)
            session.clear()
            session["conductor_id"] = conductor.id
            return redirect(url_for("conductor_bp.dashboard"))

    return render_template("conductor/ingresar.html")


# POST para cerrar la sesion: borra conductor_id y vuelve al login.
# Solo lo usa el propio conductor autenticado.
@conductor_bp.route("/salir", methods=["POST"])
def salir():
    """Cierra la sesion del conductor."""
    session.pop("conductor_id", None)
    return redirect(url_for("conductor_bp.ingresar"))


# Recuperacion de contrasena en dos pasos: (1) buscar la cuenta por correo y
# mostrar su pregunta de seguridad; (2) validar la respuesta y guardar la clave.
# Entrada: form email, accion, respuesta, password, password_confirmacion.
# Salida: render_template (pasos identificar/verificar) o redirect al login.
# Acceso publico; limitado por rate limit para frenar intentos automatizados.
@conductor_bp.route("/recuperar-password", methods=["GET", "POST"])
def recuperar_password():
    """Restablece la contrasena del conductor mediante la pregunta de seguridad."""
    # Si ya hay sesion activa no tiene sentido recuperar la contrasena.
    if _conductor_actual() is not None:
        return redirect(url_for("conductor_bp.dashboard"))

    identificador = ""
    if request.method == "POST":
        # Limita la cantidad de intentos seguidos desde la misma IP.
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

        # Solo continua si la cuenta existe, esta activa y tiene recuperacion configurada.
        if conductor is None or not conductor.activo or not conductor.tiene_recuperacion:
            flash(
                "No encontramos una cuenta activa con recuperacion configurada para ese correo.",
                "warning",
            )
            return render_template("conductor/recuperar_password.html", paso="identificar", identificador=identificador)

        # Primer paso: aun no se envia la respuesta, solo se muestra la pregunta.
        if accion != "restablecer":
            return _render_verificacion_conductor(conductor, identificador)

        # Segundo paso: valida respuesta de seguridad y nueva contrasena.
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

        # Guarda el nuevo hash (nunca la clave en texto plano) y vuelve al login.
        conductor.password_hash = generate_password_hash(password)
        db.session.commit()
        flash("Contrasena restablecida correctamente. Ya puedes ingresar con tu nueva clave.", "success")
        return redirect(url_for("conductor_bp.ingresar"))

    return render_template("conductor/recuperar_password.html", paso="identificar", identificador=identificador)


def _render_verificacion_conductor(conductor, identificador):
    """Muestra el paso de verificacion con la pregunta de seguridad del conductor."""
    return render_template(
        "conductor/recuperar_password.html",
        paso="verificar",
        identificador=identificador,
        pregunta=conductor.pregunta_seguridad,
        nombre=conductor.nombre_completo,
    )


# GET del panel principal del conductor. Carga sus rutas activas, la ruta
# seleccionada (query ?ruta_id=), sus estudiantes ordenados por parada, la
# asistencia del dia y los ultimos 20 mensajes del chat.
# Salida: render_template del dashboard. Solo conductor con sesion.
@conductor_bp.route("/")
def dashboard():
    """Panel principal del conductor: rutas, estudiantes, asistencia y chat.

    Si hay varias rutas se puede seleccionar una por `ruta_id`. Se cargan
    las asistencias del dia y los ultimos mensajes del chat de la ruta.
    """
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # Catalogo de colegios para los formularios (crear/editar estudiante y ruta).
    school_catalog = load_school_catalog()
    rutas = (
        Ruta.query.filter_by(conductor_id=conductor.id, activa=True)
        .order_by(Ruta.ciudad.asc(), Ruta.nombre.asc())
        .all()
    )
    # Ruta elegida por query; si no se envia, usa la primera disponible.
    ruta_id = request.args.get("ruta_id", type=int)
    if ruta_id:
        ruta = Ruta.query.filter_by(
            id=ruta_id,
            conductor_id=conductor.id,
            activa=True,
        ).first_or_404()
    else:
        ruta = rutas[0] if rutas else None
    # Ordena los estudiantes por numero de parada; los que no tienen orden
    # quedan al final (9999) y, a igualdad, se ordenan por nombre.
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
        # Asistencia del dia indexada por estudiante para marcarla en la vista.
        asistencias_hoy = {
            asistencia.estudiante_id: asistencia
            for asistencia in Asistencia.query.filter_by(
                ruta_id=ruta.id,
                fecha_operacion=date.today(),
            ).all()
        }
        # Ultimos 20 mensajes, revertidos para mostrarlos en orden cronologico.
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
        # Centro inicial del mapa: ultima ubicacion conocida de la ruta o el
        # valor por defecto definido en la configuracion de la aplicacion.
        map_defaults={
            "lat": ruta.latitud_actual if ruta and ruta.latitud_actual is not None else current_app.config["MAP_DEFAULT_LAT"],
            "lng": ruta.longitud_actual if ruta and ruta.longitud_actual is not None else current_app.config["MAP_DEFAULT_LNG"],
            "zoom": current_app.config["MAP_DEFAULT_ZOOM"],
        },
        municipios_catalogo=get_municipalities(),
        colegios_catalogo=school_catalog,
    )


# POST para editar la ruta (nombre, PIN de familias, ciudad, origen, destino,
# horarios). Entrada: form + ruta_id en la URL. Salida: redirect al dashboard.
# Solo el conductor dueno de la ruta puede modificarla.
@conductor_bp.route("/ruta/<int:ruta_id>/actualizar", methods=["POST"])
def actualizar_ruta(ruta_id):
    """Edita la informacion general de una ruta (nombre, PIN, origen, destino).

    Solo el conductor asignado a la ruta puede modificarla. Al guardar se
    intenta geocodificar automaticamente el origen y el destino.
    """
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # 404 si la ruta no existe o no pertenece a este conductor.
    ruta = Ruta.query.filter_by(id=ruta_id, conductor_id=conductor.id).first_or_404()
    # El PIN identifica la ruta ante las familias; debe ser unico en el sistema.
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
    # Geocodifica origen/destino para poder dibujarlos en el mapa de las familias.
    _hydrate_route_geodata(ruta)
    return _commit_driver_changes(
        "La informacion general de la ruta fue actualizada.",
        "No fue posible actualizar la ruta.",
        ruta.id,
        notify_route_ids=[ruta],
    )


# POST para editar el perfil del conductor: datos personales, datos y foto de
# la buseta, pregunta de seguridad y cambio opcional de contrasena.
# Entrada: form (multipart por la foto) + ruta_id para volver al mismo panel.
# Salida: redirect al dashboard. Solo conductor con sesion.
@conductor_bp.route("/perfil/actualizar", methods=["POST"])
def actualizar_perfil():
    """Permite al conductor editar sus datos, su buseta, su foto y su contraseña."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # ruta_id solo sirve para regresar al dashboard con la misma ruta abierta.
    ruta_id = request.form.get("ruta_id", type=int)
    nombres = request.form.get("nombres", "").strip()
    apellidos = request.form.get("apellidos", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not nombres or not apellidos or not email:
        flash("Nombres, apellidos y correo son obligatorios.", "warning")
        return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    # El cambio de contrasena es opcional; si se envia, exige la actual correcta.
    password_actual = request.form.get("password_actual", "")
    password_nueva = request.form.get("password_nueva", "")
    if password_nueva:
        if len(password_nueva) < 8:
            flash("La nueva contraseña debe tener al menos 8 caracteres.", "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))
        if not conductor.password_hash or not check_password_hash(conductor.password_hash, password_actual):
            flash("La contraseña actual no coincide. No se cambio la contraseña.", "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    # Actualiza la pregunta/respuesta de seguridad para poder recuperar la clave.
    error_recuperacion = aplicar_recuperacion(
        conductor,
        request.form.get("pregunta_seguridad"),
        request.form.get("respuesta_seguridad"),
    )
    if error_recuperacion:
        flash(error_recuperacion, "warning")
        return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))

    # Sube la foto de la buseta si llega un archivo; si no, acepta una URL directa.
    foto = request.files.get("foto")
    if foto and foto.filename:
        try:
            conductor.foto_url = guardar_foto_vehiculo(foto)
        except ValueError as error:
            flash(str(error), "warning")
            return redirect(url_for("conductor_bp.dashboard", ruta_id=ruta_id))
    else:
        foto_url = request.form.get("foto_url", "").strip()
        if foto_url:
            conductor.foto_url = foto_url

    # En estos campos, si el form viene vacio se conserva el valor anterior.
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
    # Se guarda la nueva contrasena solo como hash, nunca en texto plano.
    if password_nueva:
        conductor.password_hash = generate_password_hash(password_nueva)

    return _commit_driver_changes(
        "Tu perfil y tu buseta fueron actualizados correctamente.",
        "No fue posible actualizar tu perfil. Revisa documento, correo y placa.",
        ruta_id,
    )


# POST para agregar un estudiante (con su parada) a una ruta del conductor.
# Entrada: form del estudiante + ruta_id en la URL. Salida: redirect al panel.
# Solo el conductor dueno de la ruta puede agregar estudiantes.
@conductor_bp.route("/ruta/<int:ruta_id>/estudiantes/crear", methods=["POST"])
def crear_estudiante(ruta_id):
    """Agrega un estudiante (y su parada) a una ruta del conductor."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # Solo se puede crear dentro de una ruta activa del propio conductor.
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
    # Intenta ubicar la direccion en el mapa; si falla se corrige luego a mano.
    _hydrate_student_geodata(estudiante)
    db.session.add(estudiante)
    return _commit_driver_changes(
        "Nuevo estudiante agregado correctamente.",
        "No fue posible crear el estudiante.",
        ruta.id,
        notify_route_ids=[ruta],
    )


# POST para editar los datos de un estudiante y su parada.
# Entrada: form del estudiante + estudiante_id en la URL. Salida: redirect al panel.
# Solo si el estudiante pertenece a una ruta del conductor autenticado.
@conductor_bp.route("/estudiantes/<int:estudiante_id>/actualizar", methods=["POST"])
def actualizar_estudiante(estudiante_id):
    """Edita los datos de un estudiante y su parada dentro de la ruta."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # El join con Ruta comprueba que el estudiante pertenece a este conductor.
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
    # Vuelve a geocodificar la direccion por si cambio.
    _hydrate_student_geodata(estudiante)
    return _commit_driver_changes(
        f"Parada actualizada para {estudiante.nombre_completo}.",
        "No fue posible actualizar el estudiante.",
        estudiante.ruta_id,
        notify_route_ids=[estudiante.ruta],
    )


# POST para eliminar un estudiante de una ruta.
# Entrada: estudiante_id en la URL. Salida: redirect al dashboard.
# Solo si el estudiante pertenece a una ruta del conductor autenticado.
@conductor_bp.route("/estudiantes/<int:estudiante_id>/eliminar", methods=["POST"])
def eliminar_estudiante(estudiante_id):
    """Elimina un estudiante de la ruta del conductor autenticado."""
    conductor = _conductor_actual()
    if conductor is None:
        return redirect(url_for("conductor_bp.ingresar"))

    # El join con Ruta comprueba que el estudiante pertenece a este conductor.
    estudiante = (
        Estudiante.query.join(Ruta, Estudiante.ruta_id == Ruta.id)
        .filter(Estudiante.id == estudiante_id, Ruta.conductor_id == conductor.id)
        .first_or_404()
    )
    # Se guardan estos datos antes de borrar porque el objeto deja de existir.
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


# POST (AJAX) para fijar/corregir la ubicacion de la casa de un estudiante.
# Entrada: form lat/lng + estudiante_id en la URL. Salida: JSON.
# Solo el conductor dueno de la ruta; sin sesion valida responde 403 en JSON.
@conductor_bp.route("/estudiantes/<int:estudiante_id>/ubicacion", methods=["POST"])
def ubicar_estudiante(estudiante_id):
    """Fija o corrige en el mapa la ubicacion de la casa de un estudiante."""
    conductor = _conductor_actual()
    if conductor is None:
        return jsonify({"ok": False, "error": "Tu sesion de conductor no es valida."}), 403

    # El join con Ruta comprueba que el estudiante pertenece a este conductor.
    estudiante = (
        Estudiante.query.join(Ruta, Estudiante.ruta_id == Ruta.id)
        .filter(Estudiante.id == estudiante_id, Ruta.conductor_id == conductor.id)
        .first_or_404()
    )

    # Valida que las coordenadas existan y esten dentro de los rangos geograficos.
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

    # Avisa por SocketIO a las familias que cambio el plan de la ruta.
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


# POST para vaciar el chat de una ruta.
# Entrada: ruta_id en la URL. Salida: redirect al dashboard.
# Solo el conductor dueno de la ruta; ademas avisa a las familias por SocketIO.
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
    """Devuelve el Conductor de la sesion actual, o None si no hay sesion valida.

    Si el conductor dejo de existir o esta inactivo, limpia su id de la sesion.
    """
    conductor_id = session.get("conductor_id")
    if conductor_id is None:
        return None
    conductor = db.session.get(Conductor, conductor_id)
    if conductor is None or not conductor.activo:
        session.pop("conductor_id", None)
        return None
    return conductor


def _form_bool(field_name, default=False):
    """Interpreta un campo de formulario como booleano (checkbox/select).

    Ausente usa `default`; en otro caso es verdadero si su valor es
    1, true, on, yes o si.
    """
    raw_value = request.form.get(field_name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "on", "yes", "si"}


def _resolve_school_form_value(field_name):
    """Resuelve el valor de un colegio segun el formulario.

    Prioriza el texto libre ("<campo>_custom") para permitir colegios que no
    estan en el catalogo; si no, usa el que se eligio del listado.
    Devuelve None cuando no se selecciono ni escribio nada.
    """
    custom_value = request.form.get(f"{field_name}_custom", "").strip()
    if custom_value:
        return custom_value
    selected_value = request.form.get(field_name, "").strip()
    return selected_value or None


def _commit_driver_changes(success_message, error_message, ruta_id, notify_route_ids=None):
    """Confirma los cambios y vuelve al dashboard con el mensaje adecuado.

    Ante IntegrityError (por ejemplo documento o placa duplicados) deshace la
    transaccion y muestra el error. Al exito avisa a las familias de las rutas
    indicadas que el plan cambio.
    """
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
    """Avisa por SocketIO a las familias que el plan de la ruta cambio.

    Acepta una mezcla de objetos Ruta y de ids, los normaliza a ids unicos y
    emite a la sala de cada ruta afectada.
    """
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
    """Resuelve por geocodificacion las coordenadas del origen y destino de la ruta.

    Si no se logra ubicar alguno, deja sus coordenadas en None y avisa por flash.
    """
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
    """Intenta ubicar en el mapa la direccion del estudiante por geocodificacion.

    Si no hay direccion o el geocodificador falla, deja las coordenadas en None
    y sugiere usar 'Ubicar casita en el mapa' para fijarla manualmente.
    """
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
