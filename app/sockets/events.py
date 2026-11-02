"""Eventos Socket.IO para GPS, asistencia y chat por ruta."""
from datetime import date, datetime
import math
from flask import request, session
from flask_socketio import emit, join_room, leave_room
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Asistencia, EstadoAsistencia, Estudiante, MensajeChat, Ruta, TipoRemitenteChat, UbicacionRuta


def register_socket_events(socketio):
    """Registra todos los eventos Socket.IO una sola vez."""
    if getattr(socketio, "_ruta_exacta_events_registered", False):
        return
    socketio._ruta_exacta_events_registered = True

    @socketio.on("connect")
    def handle_connect():
        emit(
            "system:connected",
            {
                "sid": request.sid,
                "message": "Conexion establecida con Ruta Confianza.",
                "server_time": datetime.utcnow().isoformat(),
            },
        )

    @socketio.on("room:join")
    def handle_room_join(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No fue posible identificar la ruta para unirse a la sala.")
        join_room(ruta.socket_room)
        emit(
            "room:joined",
            {
                "ruta_id": ruta.id,
                "room": ruta.socket_room,
                "tracking": ruta.to_tracking_payload(),
            },
        )

    @socketio.on("room:leave")
    def handle_room_leave(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No fue posible identificar la ruta para salir de la sala.")
        leave_room(ruta.socket_room)
        emit("room:left", {"ruta_id": ruta.id, "room": ruta.socket_room})

    @socketio.on("route:start")
    def handle_route_start(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("La ruta indicada no existe.")
        if not _require_route_driver(ruta):
            return
        if not ruta.conductor or not ruta.conductor.puede_transmitir_gps:
            return _emit_error("La suscripcion del conductor no permite transmitir GPS.")
        ruta.recorrido_activo = True
        ruta.inicio_recorrido_en = datetime.utcnow()
        ruta.fin_recorrido_en = None
        ruta.latitud_actual = None
        ruta.longitud_actual = None
        ruta.precision_actual = None
        ruta.velocidad_actual = None
        ruta.rumbo_actual = None
        ruta.ultima_ubicacion_en = None
        _commit_changes()
        emit(
            "tracking:route_status",
            {
                "ruta_id": ruta.id,
                "recorrido_activo": True,
                "inicio_recorrido_en": ruta.inicio_recorrido_en.isoformat(),
                "location_reset": True,
            },
            to=ruta.socket_room,
        )

    @socketio.on("route:finish")
    def handle_route_finish(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("La ruta indicada no existe.")
        if not _require_route_driver(ruta):
            return
        ruta.recorrido_activo = False
        ruta.fin_recorrido_en = datetime.utcnow()
        _commit_changes()
        emit(
            "tracking:route_status",
            {
                "ruta_id": ruta.id,
                "recorrido_activo": False,
                "fin_recorrido_en": ruta.fin_recorrido_en.isoformat(),
            },
            to=ruta.socket_room,
        )

    @socketio.on("location:update")
    def handle_location_update(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para actualizar la ubicacion.")
        if not _require_route_driver(ruta):
            return
        if not ruta.conductor or not ruta.conductor.puede_transmitir_gps:
            return _emit_error("El conductor no tiene permiso activo para transmitir ubicacion.")
        if not ruta.recorrido_activo:
            return _emit_error("Primero debes iniciar el recorrido antes de transmitir ubicación.")
        latitud, longitud = _validate_coordinates(data)
        if latitud is None or longitud is None:
            return _emit_error("Las coordenadas enviadas no son validas.")
        precision = _normalize_positive_float(data.get("accuracy"))
        velocidad = _normalize_positive_float(data.get("speed"))
        rumbo = _normalize_heading(data.get("heading"))
        capturada_en = _parse_client_timestamp(data.get("captured_at"))

        if _should_ignore_imprecise_fix(ruta, precision):
            return
        if _is_implausible_jump(ruta, latitud, longitud, capturada_en, precision):
            return

        latitud, longitud = _smooth_location(ruta, latitud, longitud, precision)
        ruta.latitud_actual = latitud
        ruta.longitud_actual = longitud
        ruta.precision_actual = precision
        ruta.velocidad_actual = velocidad
        ruta.rumbo_actual = rumbo
        ruta.ultima_ubicacion_en = datetime.utcnow()
        if _should_store_location(ruta, latitud, longitud):
            db.session.add(
                UbicacionRuta(
                    ruta_id=ruta.id,
                    latitud=latitud,
                    longitud=longitud,
                    precision=precision,
                    velocidad=velocidad,
                    rumbo=rumbo,
                    registrada_en=ruta.ultima_ubicacion_en,
                )
            )
        _commit_changes()
        emit("tracking:location", ruta.to_tracking_payload(), to=ruta.socket_room)

    @socketio.on("attendance:update")
    def handle_attendance_update(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para actualizar la asistencia.")
        if not _require_route_driver(ruta):
            return
        estudiante_id = data.get("estudiante_id")
        if not estudiante_id:
            return _emit_error("Debes indicar el estudiante que cambia de estado.")
        if not Estudiante.query.filter_by(id=estudiante_id, ruta_id=ruta.id).first():
            return _emit_error("El estudiante no pertenece a esta ruta.")
        estado = _parse_attendance_status(data.get("estado"))
        if estado is None:
            return _emit_error("El estado de asistencia no es valido.")
        asistencia = Asistencia.query.filter_by(
            ruta_id=ruta.id,
            estudiante_id=estudiante_id,
            fecha_operacion=date.today(),
        ).first()
        if asistencia is None:
            asistencia = Asistencia(
                ruta_id=ruta.id,
                estudiante_id=estudiante_id,
                conductor_id=ruta.conductor_id,
                fecha_operacion=date.today(),
            )
            db.session.add(asistencia)
        asistencia.estado = estado
        asistencia.observacion = data.get("observacion")
        if estado == EstadoAsistencia.SUBIO:
            asistencia.hora_subida = datetime.utcnow()
        if estado == EstadoAsistencia.BAJO:
            asistencia.hora_bajada = datetime.utcnow()
        _commit_changes()
        emit("tracking:attendance", asistencia.to_socket_payload(), to=ruta.socket_room)

    @socketio.on("chat:message")
    def handle_chat_message(data):
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para enviar el mensaje.")
        contenido = (data.get("contenido") or "").strip()
        remitente_nombre = (data.get("remitente_nombre") or "Invitado").strip()
        remitente_tipo = _parse_sender_type(data.get("remitente_tipo"))
        if not contenido:
            return _emit_error("El mensaje no puede estar vacio.")
        if remitente_tipo is None:
            return _emit_error("El tipo de remitente no es valido.")
        if remitente_tipo == TipoRemitenteChat.CONDUCTOR:
            if not _require_route_driver(ruta):
                return
            remitente_nombre = ruta.conductor.nombre_completo
            conductor_id = ruta.conductor_id
        elif remitente_tipo == TipoRemitenteChat.PADRE:
            conductor_id = None
        else:
            return _emit_error("El tipo de remitente no es valido para este chat.")
        mensaje = MensajeChat(
            ruta_id=ruta.id,
            conductor_id=conductor_id,
            remitente_tipo=remitente_tipo,
            remitente_nombre=remitente_nombre,
            contenido=contenido,
        )
        db.session.add(mensaje)
        _commit_changes()
        emit("chat:new_message", mensaje.to_socket_payload(), to=ruta.socket_room)


def _resolve_route(data):
    if not isinstance(data, dict):
        return None
    ruta_id = data.get("ruta_id")
    pin = data.get("pin")
    if not ruta_id or not pin:
        return None
    return Ruta.query.filter_by(id=ruta_id, pin_acceso=str(pin)).first()


def _require_route_driver(ruta):
    conductor_id = session.get("conductor_id")
    if (
        conductor_id != ruta.conductor_id
        or not ruta.activa
        or not ruta.conductor
        or not ruta.conductor.activo
    ):
        _emit_error("Inicia sesión como el conductor asignado a esta ruta para realizar esta acción.")
        return False
    return True


def _validate_coordinates(data):
    latitud = _to_float(data.get("lat") if isinstance(data, dict) else None)
    longitud = _to_float(data.get("lng") if isinstance(data, dict) else None)
    if latitud is None or longitud is None:
        return None, None
    if not -90 <= latitud <= 90 or not -180 <= longitud <= 180:
        return None, None
    return latitud, longitud


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_positive_float(value):
    number = _to_float(value)
    if number is None or number < 0:
        return None
    return number


def _normalize_heading(value):
    heading = _to_float(value)
    if heading is None:
        return None
    heading = heading % 360
    return heading


def _parse_client_timestamp(value):
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.utcfromtimestamp(timestamp)
    except (OverflowError, OSError, ValueError):
        return None


def _should_ignore_imprecise_fix(ruta, precision):
    if precision is None:
        return False
    if ruta.latitud_actual is None or ruta.longitud_actual is None:
        return False
    return precision > 120


def _smooth_location(ruta, latitud, longitud, precision):
    if (
        ruta.latitud_actual is None
        or ruta.longitud_actual is None
        or precision is None
    ):
        return latitud, longitud

    distance = _haversine_meters(ruta.latitud_actual, ruta.longitud_actual, latitud, longitud)
    if distance is None:
        return latitud, longitud

    if precision > 45 and distance <= min(precision, 35):
        factor = 0.32
    elif precision > 20 and distance <= 18:
        factor = 0.55
    else:
        return latitud, longitud

    blended_lat = ruta.latitud_actual + ((latitud - ruta.latitud_actual) * factor)
    blended_lng = ruta.longitud_actual + ((longitud - ruta.longitud_actual) * factor)
    return blended_lat, blended_lng


def _is_implausible_jump(ruta, latitud, longitud, capturada_en, precision):
    if (
        ruta.latitud_actual is None
        or ruta.longitud_actual is None
        or ruta.ultima_ubicacion_en is None
    ):
        return False

    distance = _haversine_meters(ruta.latitud_actual, ruta.longitud_actual, latitud, longitud)
    if distance is None or distance < 120:
        return False

    reference_time = capturada_en or datetime.utcnow()
    elapsed_seconds = (reference_time - ruta.ultima_ubicacion_en).total_seconds()
    if elapsed_seconds <= 0:
        elapsed_seconds = 1

    estimated_speed_kmh = (distance / elapsed_seconds) * 3.6
    if estimated_speed_kmh <= 130:
        return False

    return precision is None or precision > 25


def _haversine_meters(lat1, lng1, lat2, lng2):
    try:
        lat1_rad = math.radians(float(lat1))
        lng1_rad = math.radians(float(lng1))
        lat2_rad = math.radians(float(lat2))
        lng2_rad = math.radians(float(lng2))
    except (TypeError, ValueError):
        return None

    d_lat = lat2_rad - lat1_rad
    d_lng = lng2_rad - lng1_rad
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lng / 2) ** 2
    )
    return 6371000 * (2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _should_store_location(ruta, latitud, longitud):
    """Evita guardar puntos idénticos, manteniendo una traza útil y liviana."""
    ultima = (
        UbicacionRuta.query.filter_by(ruta_id=ruta.id)
        .order_by(UbicacionRuta.registrada_en.desc())
        .first()
    )
    if ultima is None:
        return True
    distance = _haversine_meters(ultima.latitud, ultima.longitud, latitud, longitud)
    threshold = 12
    if ruta.precision_actual is not None:
        threshold = max(8, min(25, ruta.precision_actual * 0.45))
    if distance is not None and distance >= threshold:
        return True
    if ultima.registrada_en is None or ruta.ultima_ubicacion_en is None:
        return True
    time_threshold = 12 if (ruta.precision_actual or 999) <= 35 else 20
    return (ruta.ultima_ubicacion_en - ultima.registrada_en).total_seconds() >= time_threshold


def _parse_attendance_status(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    for status in EstadoAsistencia:
        if status.value == normalized:
            return status
    return None


def _parse_sender_type(value):
    if value is None:
        return None
    normalized = str(value).strip().lower()
    for sender_type in TipoRemitenteChat:
        if sender_type.value == normalized:
            return sender_type
    return None


def _commit_changes():
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise


def _emit_error(message):
    emit("system:error", {"message": message})
