"""Rutas del panel administrativo.

El panel esta dividido en secciones independientes (resumen, conductores,
rutas, estudiantes, solicitudes, mensajes, usuarios, reportes y respaldos)
que comparten una barra lateral de navegacion comun.
"""
from datetime import date, datetime, timedelta
import secrets
import string
import zipfile

from flask import (
    Blueprint,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app import db, socketio
from app.models import (
    AdminUser,
    Asistencia,
    Conductor,
    EstadoAsistencia,
    EstadoSolicitud,
    EstadoSuscripcion,
    Estudiante,
    MensajeContacto,
    Ruta,
    SolicitudConductor,
    SuscripcionConductor,
    UbicacionRuta,
)
from app.services.backups import (
    carpeta_respaldos,
    crear_respaldo,
    eliminar_respaldo,
    listar_respaldos,
    restaurar_respaldo,
    ruta_respaldo,
)
from app.services.geocoding import geocode_colombia_address
from app.services.reports import REPORTES, construir_reporte, generar_csv, generar_pdf
from app.services.school_catalog import get_municipalities, load_school_catalog
from app.services.security import aplicar_recuperacion, verificar_respuesta

admin_bp = Blueprint("admin_bp", __name__, url_prefix="/admin")

# Endpoints accesibles sin sesion administrativa (ingreso y recuperacion).
RUTAS_PUBLICAS_ADMIN = {"admin_bp.ingresar", "admin_bp.recuperar_password"}


@admin_bp.before_request
def proteger_panel():
    if request.endpoint in RUTAS_PUBLICAS_ADMIN:
        return None
    if _admin_actual() is None:
        flash("Debes iniciar sesion para entrar al panel administrativo.", "warning")
        return redirect(url_for("admin_bp.ingresar"))
    return None


@admin_bp.route("/ingresar", methods=["GET", "POST"])
def ingresar():
    admin = _admin_actual()
    if admin:
        return redirect(url_for("admin_bp.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        admin = AdminUser.query.filter_by(username=username).first()
        if (
            admin is None
            or not admin.activo
            or not check_password_hash(admin.password_hash, password)
        ):
            flash("Usuario o contrasena incorrectos, o cuenta inactiva.", "danger")
        else:
            admin.ultimo_acceso_en = datetime.utcnow()
            db.session.commit()
            session.clear()
            session["admin_user_id"] = admin.id
            return redirect(url_for("admin_bp.dashboard"))

    return render_template("admin/ingresar.html")


@admin_bp.route("/salir", methods=["POST"])
def salir():
    session.pop("admin_user_id", None)
    flash("Sesion administrativa cerrada correctamente.", "success")
    return redirect(url_for("admin_bp.ingresar"))


@admin_bp.route("/recuperar-password", methods=["GET", "POST"])
def recuperar_password():
    """Restablece la contrasena administrativa mediante la pregunta de seguridad."""
    if _admin_actual() is not None:
        return redirect(url_for("admin_bp.dashboard"))

    identificador = ""
    if request.method == "POST":
        accion = request.form.get("accion", "buscar")
        identificador = request.form.get("usuario", "").strip().lower()
        admin = AdminUser.query.filter_by(username=identificador).first() if identificador else None

        if admin is None or not admin.activo or not admin.tiene_recuperacion:
            flash(
                "No encontramos una cuenta activa con recuperacion configurada para ese usuario.",
                "warning",
            )
            return render_template("admin/recuperar_password.html", paso="identificar", identificador=identificador)

        if accion != "restablecer":
            return _render_verificacion_admin(admin, identificador)

        respuesta = request.form.get("respuesta", "")
        password = request.form.get("password", "")
        confirmacion = request.form.get("password_confirmacion", "")

        if not verificar_respuesta(admin.respuesta_seguridad_hash, respuesta):
            flash("La respuesta de seguridad no coincide. Intenta de nuevo.", "danger")
            return _render_verificacion_admin(admin, identificador)
        if len(password) < 8:
            flash("La nueva contrasena debe tener al menos 8 caracteres.", "warning")
            return _render_verificacion_admin(admin, identificador)
        if password != confirmacion:
            flash("Las dos contrasenas no coinciden.", "warning")
            return _render_verificacion_admin(admin, identificador)

        admin.password_hash = generate_password_hash(password)
        db.session.commit()
        flash("Contrasena restablecida correctamente. Ya puedes ingresar con tu nueva clave.", "success")
        return redirect(url_for("admin_bp.ingresar"))

    return render_template("admin/recuperar_password.html", paso="identificar", identificador=identificador)


def _render_verificacion_admin(admin, identificador):
    return render_template(
        "admin/recuperar_password.html",
        paso="verificar",
        identificador=identificador,
        pregunta=admin.pregunta_seguridad,
        nombre=admin.nombre_mostrar,
    )


# --- Secciones ----------------------------------------------------------------

@admin_bp.route("/")
def dashboard():
    """Resumen general con indicadores y graficas."""
    admin_actual = _admin_actual()
    metricas = _metricas_generales()
    estadisticas = _estadisticas_dashboard()
    solicitudes_pendientes = (
        SolicitudConductor.query.filter_by(estado=EstadoSolicitud.PENDIENTE)
        .order_by(SolicitudConductor.created_at.desc())
        .limit(5)
        .all()
    )
    mensajes_recientes = MensajeContacto.query.order_by(MensajeContacto.created_at.desc()).limit(5).all()
    rutas_activas = (
        Ruta.query.filter_by(recorrido_activo=True)
        .order_by(Ruta.nombre.asc())
        .all()
    )
    return render_template(
        "admin/resumen.html",
        seccion="resumen",
        admin_actual=admin_actual,
        metricas=metricas,
        estadisticas=estadisticas,
        solicitudes_pendientes=solicitudes_pendientes,
        mensajes_recientes=mensajes_recientes,
        rutas_activas=rutas_activas,
    )


@admin_bp.route("/conductores")
def conductores():
    admin_actual = _admin_actual()
    return render_template(
        "admin/conductores.html",
        seccion="conductores",
        admin_actual=admin_actual,
        conductores=Conductor.query.order_by(Conductor.nombres.asc()).all(),
        metricas=_metricas_generales(),
        municipios_catalogo=get_municipalities(),
        colegios_catalogo=load_school_catalog(),
    )


@admin_bp.route("/rutas")
def rutas():
    admin_actual = _admin_actual()
    ruta_seleccionada_id = request.args.get("ruta_id", type=int)
    lista_rutas = Ruta.query.order_by(Ruta.ciudad.asc(), Ruta.nombre.asc()).all()
    ruta_seleccionada = next(
        (ruta for ruta in lista_rutas if ruta.id == ruta_seleccionada_id),
        lista_rutas[0] if lista_rutas else None,
    )
    estudiantes_ruta = _estudiantes_de_ruta(ruta_seleccionada)
    return render_template(
        "admin/rutas.html",
        seccion="rutas",
        admin_actual=admin_actual,
        rutas=lista_rutas,
        conductores=Conductor.query.order_by(Conductor.nombres.asc()).all(),
        ruta_seleccionada=ruta_seleccionada,
        estudiantes_ruta=estudiantes_ruta,
        map_payload=_ruta_mapa_payload(ruta_seleccionada),
        metricas=_metricas_generales(),
        municipios_catalogo=get_municipalities(),
        colegios_catalogo=load_school_catalog(),
    )


@admin_bp.route("/api/ruta/<int:ruta_id>/seguimiento")
def api_ruta_seguimiento(ruta_id):
    """Datos de seguimiento (plan, ubicacion y recorrido) para el mapa del panel."""
    ruta = Ruta.query.get_or_404(ruta_id)
    return jsonify(_ruta_mapa_payload(ruta))


@admin_bp.route("/estudiantes")
def estudiantes():
    admin_actual = _admin_actual()
    lista_rutas = Ruta.query.order_by(Ruta.nombre.asc()).all()
    ruta_seleccionada_id = request.args.get("ruta_id", type=int)
    ruta_seleccionada = next(
        (ruta for ruta in lista_rutas if ruta.id == ruta_seleccionada_id),
        lista_rutas[0] if lista_rutas else None,
    )
    return render_template(
        "admin/estudiantes.html",
        seccion="estudiantes",
        admin_actual=admin_actual,
        rutas=lista_rutas,
        ruta_seleccionada=ruta_seleccionada,
        estudiantes_ruta=_estudiantes_de_ruta(ruta_seleccionada),
        total_estudiantes=Estudiante.query.count(),
        sin_geolocalizar=Estudiante.query.filter(Estudiante.latitud.is_(None)).count(),
        municipios_catalogo=get_municipalities(),
        colegios_catalogo=load_school_catalog(),
    )


@admin_bp.route("/solicitudes")
def solicitudes():
    admin_actual = _admin_actual()
    return render_template(
        "admin/solicitudes.html",
        seccion="solicitudes",
        admin_actual=admin_actual,
        solicitudes=SolicitudConductor.query.order_by(SolicitudConductor.created_at.desc()).all(),
        total_pendientes=SolicitudConductor.query.filter_by(estado=EstadoSolicitud.PENDIENTE).count(),
    )


@admin_bp.route("/mensajes")
def mensajes():
    admin_actual = _admin_actual()
    return render_template(
        "admin/mensajes.html",
        seccion="mensajes",
        admin_actual=admin_actual,
        mensajes=MensajeContacto.query.order_by(MensajeContacto.created_at.desc()).all(),
        total_nuevos=MensajeContacto.query.filter_by(atendido=False).count(),
    )


@admin_bp.route("/usuarios")
def usuarios():
    admin_actual = _admin_actual()
    return render_template(
        "admin/usuarios.html",
        seccion="usuarios",
        admin_actual=admin_actual,
        admin_users=AdminUser.query.order_by(AdminUser.nombre.asc(), AdminUser.username.asc()).all(),
    )


# --- Reportes -----------------------------------------------------------------

@admin_bp.route("/reportes")
def reportes():
    admin_actual = _admin_actual()
    return render_template(
        "admin/reportes.html",
        seccion="reportes",
        admin_actual=admin_actual,
        reportes=REPORTES,
        hoy=date.today(),
    )


@admin_bp.route("/reportes/descargar/<nombre>")
def descargar_reporte(nombre):
    formato = request.args.get("formato", "csv").lower()
    desde = _parse_fecha(request.args.get("desde"))
    hasta = _parse_fecha(request.args.get("hasta"))
    try:
        definicion, columnas, filas = construir_reporte(nombre, desde=desde, hasta=hasta)
    except KeyError:
        flash("El reporte solicitado no existe.", "warning")
        return redirect(url_for("admin_bp.reportes"))

    marca = datetime.now().strftime("%Y%m%d-%H%M")
    base_archivo = f"{nombre}-{marca}"
    subtitulo = _subtitulo_fechas(desde, hasta)

    if formato == "pdf":
        buffer = generar_pdf(columnas, filas, definicion["titulo"], subtitulo)
        if buffer is None:
            flash("No se pudo generar el PDF porque falta la libreria reportlab.", "danger")
            return redirect(url_for("admin_bp.reportes"))
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{base_archivo}.pdf",
        )

    contenido = generar_csv(columnas, filas)
    return Response(
        contenido,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={base_archivo}.csv"},
    )


# --- Respaldos ----------------------------------------------------------------

@admin_bp.route("/respaldos")
def respaldos():
    admin_actual = _admin_actual()
    return render_template(
        "admin/respaldos.html",
        seccion="respaldos",
        admin_actual=admin_actual,
        respaldos=listar_respaldos(),
        carpeta=str(carpeta_respaldos()),
    )


@admin_bp.route("/respaldos/crear", methods=["POST"])
def crear_respaldo_admin():
    try:
        info = crear_respaldo(request.form.get("motivo", "manual").strip() or "manual")
    except OSError as exc:
        flash(f"No fue posible crear el respaldo: {exc}", "danger")
    else:
        flash(f"Respaldo creado correctamente: {info['nombre']} ({info['tamano']}).", "success")
    return redirect(url_for("admin_bp.respaldos"))


@admin_bp.route("/respaldos/<path:nombre>/descargar")
def descargar_respaldo(nombre):
    try:
        archivo = ruta_respaldo(nombre)
    except (ValueError, FileNotFoundError):
        flash("El respaldo solicitado no existe.", "warning")
        return redirect(url_for("admin_bp.respaldos"))
    return send_file(archivo, as_attachment=True, download_name=archivo.name)


@admin_bp.route("/respaldos/<path:nombre>/eliminar", methods=["POST"])
def eliminar_respaldo_admin(nombre):
    try:
        eliminado = eliminar_respaldo(nombre)
    except (ValueError, FileNotFoundError):
        flash("El respaldo indicado no existe.", "warning")
    else:
        flash(f"Respaldo eliminado: {eliminado}.", "success")
    return redirect(url_for("admin_bp.respaldos"))


@admin_bp.route("/respaldos/restaurar", methods=["POST"])
def restaurar_respaldo_admin():
    nombre = request.form.get("nombre", "").strip()
    archivo_subido = request.files.get("archivo")
    try:
        if archivo_subido and archivo_subido.filename:
            if not archivo_subido.filename.lower().endswith(".zip"):
                raise ValueError("Solo se aceptan archivos de respaldo .zip.")
            destino = carpeta_respaldos() / f"respaldo-subido-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
            archivo_subido.save(destino)
            nombre = destino.name
        if not nombre:
            raise ValueError("Selecciona un respaldo existente o sube un archivo .zip.")
        restaurar_respaldo(nombre)
    except (ValueError, FileNotFoundError, zipfile.BadZipFile, OSError) as exc:
        flash(f"No fue posible restaurar el respaldo: {exc}", "danger")
    else:
        flash("Respaldo restaurado. La informacion fue reemplazada por la copia seleccionada.", "success")
    return redirect(url_for("admin_bp.respaldos"))


# --- Conductores --------------------------------------------------------------

@admin_bp.route("/conductores/crear", methods=["POST"])
def crear_conductor():
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("La contrasena del conductor debe tener al menos 8 caracteres.", "warning")
        return redirect(url_for("admin_bp.conductores"))

    estado_suscripcion = EstadoSuscripcion(request.form.get("estado_suscripcion", EstadoSuscripcion.PENDIENTE.value))
    conductor = Conductor(
        nombres=request.form.get("nombres", "").strip(),
        apellidos=request.form.get("apellidos", "").strip(),
        documento=request.form.get("documento", "").strip(),
        telefono=request.form.get("telefono", "").strip(),
        email=request.form.get("email", "").strip().lower(),
        password_hash=generate_password_hash(password),
        nombre_vehiculo=request.form.get("nombre_vehiculo", "").strip(),
        placa_vehiculo=request.form.get("placa_vehiculo", "").strip().upper(),
        capacidad_vehiculo=request.form.get("capacidad_vehiculo") or None,
        color_vehiculo=request.form.get("color_vehiculo", "").strip() or None,
        ciudad=request.form.get("ciudad", "").strip() or None,
        descripcion_publica=request.form.get("descripcion_publica", "").strip() or None,
        foto_url=request.form.get("foto_url", "").strip() or None,
        visible_en_directorio=_form_bool("visible_en_directorio"),
        activo=_form_bool("activo", default=True),
        gps_habilitado=estado_suscripcion == EstadoSuscripcion.ACTIVA,
        estado_suscripcion=estado_suscripcion,
    )
    if estado_suscripcion == EstadoSuscripcion.ACTIVA:
        conductor.suscripcion_inicia_en = datetime.utcnow()
        conductor.suscripcion_vence_en = datetime.utcnow() + timedelta(days=30)
    db.session.add(conductor)
    if estado_suscripcion == EstadoSuscripcion.ACTIVA:
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash("No fue posible crear el conductor. Verifica documento, correo y placa.", "danger")
            return redirect(url_for("admin_bp.conductores"))
        db.session.add(
            SuscripcionConductor(
                conductor_id=conductor.id,
                estado=estado_suscripcion,
                fecha_inicio=conductor.suscripcion_inicia_en,
                fecha_fin=conductor.suscripcion_vence_en,
                observaciones="Alta inicial desde el panel administrativo.",
                activado_por="Administrador",
            )
        )
    return _commit_with_feedback(
        success_message="Conductor creado correctamente.",
        error_message="No fue posible crear el conductor. Verifica documento, correo y placa.",
        redirect_to=url_for("admin_bp.conductores"),
    )


@admin_bp.route("/conductores/<int:conductor_id>/actualizar", methods=["POST"])
def actualizar_conductor(conductor_id):
    conductor = Conductor.query.get_or_404(conductor_id)
    conductor.nombres = request.form.get("nombres", "").strip()
    conductor.apellidos = request.form.get("apellidos", "").strip()
    conductor.telefono = request.form.get("telefono", "").strip()
    conductor.email = request.form.get("email", "").strip().lower()
    conductor.nombre_vehiculo = request.form.get("nombre_vehiculo", "").strip()
    conductor.placa_vehiculo = request.form.get("placa_vehiculo", "").strip().upper()
    conductor.capacidad_vehiculo = request.form.get("capacidad_vehiculo") or None
    conductor.color_vehiculo = request.form.get("color_vehiculo", "").strip() or None
    conductor.ciudad = request.form.get("ciudad", "").strip() or None
    conductor.descripcion_publica = request.form.get("descripcion_publica", "").strip() or None
    conductor.foto_url = request.form.get("foto_url", "").strip() or None
    conductor.visible_en_directorio = _form_bool("visible_en_directorio")
    conductor.activo = _form_bool("activo", default=True)
    return _commit_with_feedback(
        success_message=f"Conductor {conductor.nombre_completo} actualizado correctamente.",
        error_message="No fue posible actualizar el conductor. Verifica correo y placa.",
        redirect_to=url_for("admin_bp.conductores"),
    )


@admin_bp.route("/conductores/<int:conductor_id>/credenciales", methods=["POST"])
def actualizar_credenciales_conductor(conductor_id):
    password = request.form.get("password", "")
    if len(password) < 8:
        flash("La contrasena del conductor debe tener al menos 8 caracteres.", "warning")
        return redirect(url_for("admin_bp.conductores"))

    conductor = Conductor.query.get_or_404(conductor_id)
    conductor.password_hash = generate_password_hash(password)
    return _commit_with_feedback(
        success_message="La contrasena del conductor fue actualizada.",
        error_message="No fue posible actualizar la contrasena del conductor.",
        redirect_to=url_for("admin_bp.conductores"),
    )


@admin_bp.route("/conductores/<int:conductor_id>/eliminar", methods=["POST"])
def eliminar_conductor(conductor_id):
    conductor = Conductor.query.get_or_404(conductor_id)
    nombre = conductor.nombre_completo
    db.session.delete(conductor)
    return _commit_with_feedback(
        success_message=f"Conductor {nombre} eliminado junto con sus rutas.",
        error_message="No fue posible eliminar el conductor.",
        redirect_to=url_for("admin_bp.conductores"),
    )


@admin_bp.route("/suscripcion/<int:conductor_id>", methods=["POST"])
def actualizar_suscripcion(conductor_id):
    conductor = Conductor.query.get_or_404(conductor_id)
    estado = request.form.get("estado", EstadoSuscripcion.ACTIVA.value)
    estado_enum = EstadoSuscripcion(estado)
    conductor.estado_suscripcion = estado_enum
    conductor.gps_habilitado = estado_enum == EstadoSuscripcion.ACTIVA
    conductor.suscripcion_inicia_en = datetime.utcnow()
    conductor.suscripcion_vence_en = datetime.utcnow() + timedelta(days=30) if conductor.gps_habilitado else None
    registro = SuscripcionConductor(
        conductor_id=conductor.id,
        estado=estado_enum,
        fecha_inicio=conductor.suscripcion_inicia_en,
        fecha_fin=conductor.suscripcion_vence_en,
        observaciones="Actualizacion manual desde el panel maestro.",
        activado_por="Administrador",
    )
    db.session.add(registro)
    return _commit_with_feedback(
        success_message="La suscripcion del conductor fue actualizada.",
        error_message="No fue posible actualizar la suscripcion del conductor.",
        redirect_to=url_for("admin_bp.conductores"),
    )


# --- Rutas --------------------------------------------------------------------

@admin_bp.route("/rutas/crear", methods=["POST"])
def crear_ruta():
    conductor_id = request.form.get("conductor_id", type=int)
    if conductor_id is None:
        flash("Debes seleccionar un conductor para la ruta.", "warning")
        return redirect(url_for("admin_bp.rutas"))
    conductor = Conductor.query.get_or_404(conductor_id)
    ruta = Ruta(
        conductor_id=conductor_id,
        conductor=conductor,
        nombre=request.form.get("nombre", "").strip(),
        codigo=(request.form.get("codigo", "").strip().upper() or _generate_unique_code()),
        ciudad=request.form.get("ciudad", "").strip() or None,
        descripcion=request.form.get("descripcion", "").strip() or None,
        origen=request.form.get("origen", "").strip() or None,
        destino=request.form.get("destino", "").strip() or None,
        horario_salida=request.form.get("horario_salida", "").strip() or None,
        horario_regreso=request.form.get("horario_regreso", "").strip() or None,
        pin_acceso=(request.form.get("pin_acceso", "").strip().upper() or _generate_unique_pin()),
        activa=_form_bool("activa", default=True),
    )
    _hydrate_route_geodata(ruta)
    db.session.add(ruta)
    return _commit_with_feedback(
        success_message="Ruta creada correctamente.",
        error_message="No fue posible crear la ruta. Revisa que el codigo y PIN no esten repetidos.",
        redirect_to=url_for("admin_bp.rutas"),
        notify_route_ids=[ruta],
    )


@admin_bp.route("/rutas/<int:ruta_id>/actualizar", methods=["POST"])
def actualizar_ruta(ruta_id):
    ruta = Ruta.query.get_or_404(ruta_id)
    conductor_id = request.form.get("conductor_id", type=int)
    if conductor_id is None:
        flash("Debes seleccionar un conductor para la ruta.", "warning")
        return redirect(url_for("admin_bp.rutas", ruta_id=ruta_id))

    conductor = Conductor.query.get_or_404(conductor_id)
    ruta.conductor_id = conductor_id
    ruta.conductor = conductor
    ruta.nombre = request.form.get("nombre", "").strip()
    ruta.codigo = request.form.get("codigo", "").strip().upper() or ruta.codigo
    ruta.pin_acceso = request.form.get("pin_acceso", "").strip().upper() or ruta.pin_acceso
    ruta.ciudad = request.form.get("ciudad", "").strip() or None
    ruta.descripcion = request.form.get("descripcion", "").strip() or None
    ruta.origen = request.form.get("origen", "").strip() or None
    ruta.destino = request.form.get("destino", "").strip() or None
    ruta.horario_salida = request.form.get("horario_salida", "").strip() or None
    ruta.horario_regreso = request.form.get("horario_regreso", "").strip() or None
    ruta.activa = _form_bool("activa")
    _hydrate_route_geodata(ruta, conductor)
    return _commit_with_feedback(
        success_message="Ruta actualizada correctamente.",
        error_message="No fue posible actualizar la ruta. Revisa codigo, PIN y conductor asignado.",
        redirect_to=url_for("admin_bp.rutas", ruta_id=ruta_id),
        notify_route_ids=[ruta],
    )


@admin_bp.route("/rutas/<int:ruta_id>/eliminar", methods=["POST"])
def eliminar_ruta(ruta_id):
    ruta = Ruta.query.get_or_404(ruta_id)
    nombre = ruta.nombre
    db.session.delete(ruta)
    return _commit_with_feedback(
        success_message=f"Ruta {nombre} eliminada correctamente.",
        error_message="No fue posible eliminar la ruta.",
        redirect_to=url_for("admin_bp.rutas"),
    )


# --- Estudiantes --------------------------------------------------------------

@admin_bp.route("/estudiantes/crear", methods=["POST"])
def crear_estudiante():
    ruta_id = request.form.get("ruta_id", type=int)
    if ruta_id is None:
        flash("Debes seleccionar una ruta para matricular al estudiante.", "warning")
        return redirect(url_for("admin_bp.estudiantes"))
    ruta = Ruta.query.get_or_404(ruta_id)
    estudiante = Estudiante(
        ruta_id=ruta_id,
        nombres=request.form.get("nombres", "").strip(),
        apellidos=request.form.get("apellidos", "").strip(),
        documento=request.form.get("documento", "").strip() or None,
        institucion=request.form.get("institucion", "").strip() or None,
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
    _hydrate_student_geodata(estudiante, ruta)
    db.session.add(estudiante)
    return _commit_with_feedback(
        success_message="Estudiante agregado a la ruta.",
        error_message="No fue posible registrar al estudiante. Revisa el documento y los datos obligatorios.",
        redirect_to=url_for("admin_bp.estudiantes", ruta_id=ruta_id),
        notify_route_ids=[ruta],
    )


@admin_bp.route("/estudiantes/<int:estudiante_id>/actualizar", methods=["POST"])
def actualizar_estudiante(estudiante_id):
    estudiante = Estudiante.query.get_or_404(estudiante_id)
    ruta_anterior_id = estudiante.ruta_id
    ruta_id = request.form.get("ruta_id", type=int)
    if ruta_id is None:
        flash("Debes seleccionar una ruta para el estudiante.", "warning")
        return redirect(url_for("admin_bp.estudiantes", ruta_id=estudiante.ruta_id))

    ruta = Ruta.query.get_or_404(ruta_id)
    estudiante.ruta_id = ruta_id
    estudiante.nombres = request.form.get("nombres", "").strip()
    estudiante.apellidos = request.form.get("apellidos", "").strip()
    estudiante.documento = request.form.get("documento", "").strip() or None
    estudiante.institucion = request.form.get("institucion", "").strip() or None
    estudiante.grado = request.form.get("grado", "").strip() or None
    estudiante.direccion = request.form.get("direccion", "").strip() or None
    estudiante.barrio = request.form.get("barrio", "").strip() or None
    estudiante.ciudad = request.form.get("ciudad", "").strip() or ruta.ciudad
    estudiante.referencia_direccion = request.form.get("referencia_direccion", "").strip() or None
    estudiante.orden_parada = request.form.get("orden_parada", type=int)
    estudiante.acudiente_nombre = request.form.get("acudiente_nombre", "").strip()
    estudiante.acudiente_telefono = request.form.get("acudiente_telefono", "").strip()
    estudiante.acudiente_email = request.form.get("acudiente_email", "").strip().lower() or None
    estudiante.activo = _form_bool("activo", default=True)
    _hydrate_student_geodata(estudiante, ruta)
    return _commit_with_feedback(
        success_message=f"Estudiante {estudiante.nombre_completo} actualizado correctamente.",
        error_message="No fue posible actualizar el estudiante. Revisa documento y ruta asignada.",
        redirect_to=url_for("admin_bp.estudiantes", ruta_id=ruta_id),
        notify_route_ids=[ruta_anterior_id, ruta],
    )


@admin_bp.route("/estudiantes/<int:estudiante_id>/eliminar", methods=["POST"])
def eliminar_estudiante(estudiante_id):
    estudiante = Estudiante.query.get_or_404(estudiante_id)
    ruta_id = estudiante.ruta_id
    nombre_estudiante = estudiante.nombre_completo
    db.session.delete(estudiante)
    return _commit_with_feedback(
        success_message=f"Estudiante {nombre_estudiante} eliminado correctamente.",
        error_message="No fue posible eliminar el estudiante.",
        redirect_to=url_for("admin_bp.estudiantes", ruta_id=ruta_id),
        notify_route_ids=[ruta_id],
    )


# --- Mensajes y solicitudes ---------------------------------------------------

@admin_bp.route("/mensajes/<int:mensaje_id>/atender", methods=["POST"])
def marcar_mensaje_atendido(mensaje_id):
    mensaje = MensajeContacto.query.get_or_404(mensaje_id)
    mensaje.atendido = True
    mensaje.atendido_en = datetime.utcnow()
    return _commit_with_feedback(
        success_message="Mensaje marcado como atendido.",
        error_message="No fue posible actualizar el mensaje de contacto.",
        redirect_to=url_for("admin_bp.mensajes"),
    )


@admin_bp.route("/solicitudes/<int:solicitud_id>/gestionar", methods=["POST"])
def gestionar_solicitud(solicitud_id):
    solicitud = SolicitudConductor.query.get_or_404(solicitud_id)
    accion = request.form.get("accion")
    if accion not in {"aprobar", "rechazar"}:
        flash("La accion solicitada no es valida.", "warning")
        return redirect(url_for("admin_bp.solicitudes"))
    solicitud.estado = EstadoSolicitud.APROBADA if accion == "aprobar" else EstadoSolicitud.RECHAZADA
    solicitud.revisado_por = "Administrador"
    solicitud.revisado_en = datetime.utcnow()
    return _commit_with_feedback(
        success_message=f"Solicitud {solicitud.estado.value} correctamente.",
        error_message="No fue posible actualizar la solicitud.",
        redirect_to=url_for("admin_bp.solicitudes"),
    )


# --- Usuarios administrativos -------------------------------------------------

@admin_bp.route("/usuarios/crear", methods=["POST"])
def crear_usuario_admin():
    username = request.form.get("username", "").strip().lower()
    nombre = request.form.get("nombre", "").strip()
    password = request.form.get("password", "")

    if not username or not nombre:
        flash("Debes completar nombre y usuario para crear el acceso administrativo.", "warning")
        return redirect(url_for("admin_bp.usuarios"))
    if len(password) < 8:
        flash("La contrasena del administrador debe tener al menos 8 caracteres.", "warning")
        return redirect(url_for("admin_bp.usuarios"))

    admin_user = AdminUser(
        username=username,
        nombre=nombre,
        password_hash=generate_password_hash(password),
        activo=_form_bool("activo", default=True),
    )
    error_recuperacion = aplicar_recuperacion(
        admin_user,
        request.form.get("pregunta_seguridad"),
        request.form.get("respuesta_seguridad"),
    )
    if error_recuperacion:
        flash(error_recuperacion, "warning")
        return redirect(url_for("admin_bp.usuarios"))
    db.session.add(admin_user)
    return _commit_with_feedback(
        success_message="Usuario administrativo creado correctamente.",
        error_message="No fue posible crear el usuario administrativo. Verifica que el usuario no este repetido.",
        redirect_to=url_for("admin_bp.usuarios"),
    )


@admin_bp.route("/usuarios/<int:admin_user_id>/actualizar", methods=["POST"])
def actualizar_usuario_admin(admin_user_id):
    admin_user = AdminUser.query.get_or_404(admin_user_id)
    username = request.form.get("username", "").strip().lower()
    nombre = request.form.get("nombre", "").strip()
    password = request.form.get("password", "")

    if not username or not nombre:
        flash("Debes completar nombre y usuario para actualizar el acceso administrativo.", "warning")
        return redirect(url_for("admin_bp.usuarios"))

    admin_user.username = username
    admin_user.nombre = nombre
    admin_user.activo = _form_bool("activo")
    error_recuperacion = aplicar_recuperacion(
        admin_user,
        request.form.get("pregunta_seguridad"),
        request.form.get("respuesta_seguridad"),
    )
    if error_recuperacion:
        flash(error_recuperacion, "warning")
        return redirect(url_for("admin_bp.usuarios"))
    if password:
        if len(password) < 8:
            flash("La nueva contrasena del administrador debe tener al menos 8 caracteres.", "warning")
            return redirect(url_for("admin_bp.usuarios"))
        admin_user.password_hash = generate_password_hash(password)

    current_admin = _admin_actual()
    if current_admin and current_admin.id == admin_user.id and not admin_user.activo:
        flash("No puedes desactivar la sesion administrativa que estas usando en este momento.", "warning")
        return redirect(url_for("admin_bp.usuarios"))

    return _commit_with_feedback(
        success_message="Usuario administrativo actualizado correctamente.",
        error_message="No fue posible actualizar el usuario administrativo. Verifica que el usuario no este repetido.",
        redirect_to=url_for("admin_bp.usuarios"),
    )


# --- Utilidades ---------------------------------------------------------------

def _admin_actual():
    admin_user_id = session.get("admin_user_id")
    if admin_user_id is None:
        return None
    admin_user = db.session.get(AdminUser, admin_user_id)
    if admin_user is None or not admin_user.activo:
        session.pop("admin_user_id", None)
        return None
    return admin_user


def _ruta_mapa_payload(ruta):
    """Empaqueta plan, ubicacion vigente y traza GPS de una ruta para el mapa del panel."""
    if ruta is None:
        return None
    puntos = list(
        reversed(
            UbicacionRuta.query.filter_by(ruta_id=ruta.id)
            .order_by(UbicacionRuta.registrada_en.desc())
            .limit(500)
            .all()
        )
    )
    return {
        "plan": ruta.to_navigation_payload(),
        "tracking": ruta.to_tracking_payload(),
        "puntos": [punto.to_map_payload() for punto in puntos],
    }


def _estudiantes_de_ruta(ruta):
    if ruta is None:
        return []
    return sorted(
        ruta.estudiantes,
        key=lambda estudiante: (
            estudiante.orden_parada is None,
            estudiante.orden_parada if estudiante.orden_parada is not None else 9999,
            estudiante.nombres.lower(),
        ),
    )


def _metricas_generales():
    return {
        "conductores": Conductor.query.count(),
        "rutas": Ruta.query.count(),
        "estudiantes": Estudiante.query.count(),
        "suscripciones_activas": Conductor.query.filter_by(estado_suscripcion=EstadoSuscripcion.ACTIVA).count(),
        "solicitudes_pendientes": SolicitudConductor.query.filter_by(estado=EstadoSolicitud.PENDIENTE).count(),
        "mensajes_contacto_nuevos": MensajeContacto.query.filter_by(atendido=False).count(),
        "usuarios_admin": AdminUser.query.filter_by(activo=True).count(),
    }


def _estadisticas_dashboard():
    """Datos listos para graficar en el resumen."""
    hoy = date.today()

    suscripciones_por_estado = {estado.value: 0 for estado in EstadoSuscripcion}
    for estado, total in (
        db.session.query(Conductor.estado_suscripcion, func.count(Conductor.id))
        .group_by(Conductor.estado_suscripcion)
        .all()
    ):
        if estado is not None:
            suscripciones_por_estado[estado.value] = total

    estudiantes_por_ruta = [
        {"etiqueta": ruta.nombre, "valor": len(ruta.estudiantes)}
        for ruta in Ruta.query.order_by(Ruta.nombre.asc()).all()
    ]

    asistencias_hoy = {estado.value: 0 for estado in EstadoAsistencia}
    for estado, total in (
        db.session.query(Asistencia.estado, func.count(Asistencia.id))
        .filter(Asistencia.fecha_operacion == hoy)
        .group_by(Asistencia.estado)
        .all()
    ):
        if estado is not None:
            asistencias_hoy[estado.value] = total

    return {
        "suscripciones_por_estado": suscripciones_por_estado,
        "estudiantes_por_ruta": estudiantes_por_ruta,
        "asistencias_hoy": asistencias_hoy,
    }


def _parse_fecha(valor):
    if not valor:
        return None
    try:
        return datetime.strptime(valor.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _subtitulo_fechas(desde, hasta):
    if desde and hasta:
        return f"Periodo del {desde.isoformat()} al {hasta.isoformat()}"
    if desde:
        return f"Desde el {desde.isoformat()}"
    if hasta:
        return f"Hasta el {hasta.isoformat()}"
    return ""


def _commit_with_feedback(success_message, error_message, redirect_to=None, notify_route_ids=None):
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(error_message, "danger")
    else:
        _notify_route_plan_updates(notify_route_ids)
        flash(success_message, "success")
    return redirect(redirect_to or url_for("admin_bp.dashboard"))


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


def _form_bool(field_name, default=False):
    raw_value = request.form.get(field_name)
    if raw_value is None:
        return default
    return str(raw_value).strip().lower() in {"1", "true", "on", "yes", "si"}


def _generate_unique_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = f"RT-{''.join(secrets.choice(alphabet) for _ in range(5))}"
        if Ruta.query.filter_by(codigo=candidate).first() is None:
            return candidate


def _generate_unique_pin():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(6))
        if Ruta.query.filter_by(pin_acceso=candidate).first() is None:
            return candidate


def _hydrate_route_geodata(ruta, conductor=None):
    route_driver = conductor or ruta.conductor
    if ruta.ciudad is None and route_driver and route_driver.ciudad:
        ruta.ciudad = route_driver.ciudad

    if ruta.origen:
        origen = geocode_colombia_address(ruta.origen, ruta.ciudad, "Casanare", "Colombia")
        if origen:
            ruta.origen_latitud = origen["latitud"]
            ruta.origen_longitud = origen["longitud"]
        else:
            ruta.origen_latitud = None
            ruta.origen_longitud = None
            flash("No se pudo ubicar automaticamente el origen de la ruta. Puedes corregirlo luego.", "warning")
    else:
        ruta.origen_latitud = None
        ruta.origen_longitud = None

    if ruta.destino:
        destino = geocode_colombia_address(ruta.destino, ruta.ciudad, "Casanare", "Colombia")
        if destino:
            ruta.destino_latitud = destino["latitud"]
            ruta.destino_longitud = destino["longitud"]
        else:
            ruta.destino_latitud = None
            ruta.destino_longitud = None
            flash("No se pudo ubicar automaticamente el destino de la ruta. Puedes corregirlo luego.", "warning")
    else:
        ruta.destino_latitud = None
        ruta.destino_longitud = None


def _hydrate_student_geodata(estudiante, ruta=None):
    route = ruta or estudiante.ruta
    if estudiante.ciudad is None and route and route.ciudad:
        estudiante.ciudad = route.ciudad

    if not estudiante.direccion:
        estudiante.latitud = None
        estudiante.longitud = None
        return

    geodata = geocode_colombia_address(
        estudiante.direccion,
        estudiante.barrio,
        estudiante.ciudad or (route.ciudad if route else None),
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
        f"No se pudo ubicar la direccion de {estudiante.nombre_completo}. Revisa la direccion o el barrio.",
        "warning",
    )
