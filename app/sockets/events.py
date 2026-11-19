"""Eventos Socket.IO para GPS, asistencia y chat por ruta.

Este modulo concentra toda la comunicacion en tiempo real de la aplicacion.
El flujo general es:

1. Cada cliente (conductor, familia o admin) abre una conexion Socket.IO.
2. El cliente pide unirse a la sala de una ruta con el evento "room:join"
   enviando el ruta_id y, cuando aplica, el PIN de acceso.
3. El servidor valida el acceso y mete al cliente en la sala de esa ruta
   (room). A partir de ahi solo recibira los eventos de ESA ruta.
4. El conductor emite eventos de GPS, asistencia y chat; el servidor los
   valida, los guarda en la base de datos y los reenvia a la sala.

El aislamiento por sala (rooms) es clave: los mensajes de chat y el GPS de una
ruta nunca llegan a clientes de otra ruta.
"""
from datetime import date, datetime
import math
from flask import request, session
from flask_socketio import emit, join_room, leave_room
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import Asistencia, EstadoAsistencia, Estudiante, MensajeChat, Ruta, TipoRemitenteChat, UbicacionRuta
from app.services.rate_limit import (
    BLOQUEO_PIN_SEGUNDOS,
    MAX_FALLOS_PIN,
    MAX_MENSAJES_CHAT_PADRE,
    bloqueo_restante,
    cliente_ip,
    cupo_agotado,
    cupo_restante,
    demasiadas_peticiones,
    espera_legible,
    olvidar,
    registrar_fallo,
)
from app.services.route_access import route_for_session

# Tope de mensajes por minuto para que nadie inunde el chat de una ruta.
MAX_MENSAJES_CHAT = 20
VENTANA_CHAT_SEGUNDOS = 60
# El tope total de mensajes de las familias vive en `rate_limit.py`
# (MAX_MENSAJES_CHAT_PADRE) para compartirlo con el control por IP.


def register_socket_events(socketio):
    """Registra todos los eventos Socket.IO una sola vez.

    Se guarda una marca en el objeto `socketio` para que, si la funcion se
    vuelve a llamar (por ejemplo al recargar el modulo), no se dupliquen los
    manejadores. Registra un @socketio.on(...) por cada tipo de evento.
    """
    if getattr(socketio, "_ruta_exacta_events_registered", False):
        return
    socketio._ruta_exacta_events_registered = True

    @socketio.on("connect")
    def handle_connect():
        # Evento automatico al abrir la conexion. NO requiere autenticacion:
        # solo confirma el enlace y entrega el identificador de sesion (sid),
        # la hora del servidor y un mensaje de bienvenida. Se envia unicamente
        # al cliente que se acaba de conectar.
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
        # Emitido por el cliente (familia, conductor o admin) para entrar a la
        # sala (room) de una ruta. Payload esperado: {"ruta_id": ..., "pin": ...}.
        # Aqui esta la validacion de acceso: la sesion del conductor o el PIN
        # de la ruta deben autorizar la entrada (ver `_resolve_route`).
        #
        # Anti fuerza bruta: si la misma IP acumula varios intentos fallidos de
        # PIN se bloquea temporalmente (el bloqueo se comparte con las rutas
        # HTTP mediante `rate_limit`).
        #
        # Al entrar, el cliente recibe "room:joined" con el estado de tracking
        # actual para pintar el mapa de inmediato y, desde ese momento, queda
        # suscrito a todo lo que se emita en esa sala.
        clave = f"pin-sala:{cliente_ip()}"
        espera = bloqueo_restante(clave)
        if espera:
            return _emit_error(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}."
            )
        ruta = _resolve_route(data)
        if not ruta:
            registrar_fallo(clave, MAX_FALLOS_PIN, BLOQUEO_PIN_SEGUNDOS)
            return _emit_error("No fue posible identificar la ruta para unirse a la sala.")
        olvidar(clave)
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
        # Emitido por el cliente para salir de la sala de su ruta (por ejemplo
        # al cerrar la pantalla de seguimiento). Payload: {"ruta_id": ...}.
        # Deja de recibir eventos de esa ruta hasta volver a unirse.
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No fue posible identificar la ruta para salir de la sala.")
        leave_room(ruta.socket_room)
        emit("room:left", {"ruta_id": ruta.id, "room": ruta.socket_room})

    @socketio.on("route:start")
    def handle_route_start(data):
        # Emitido por el CONDUCTOR al comenzar la jornada. Payload:
        # {"ruta_id": ...}. Solo el conductor autenticado de esa ruta puede
        # hacerlo (ver `_require_route_driver`) y ademas necesita una
        # suscripcion que permita transmitir GPS.
        #
        # Deja la ruta en estado "en recorrido", reinicia contadores/ubicacion y
        # borra la traza anterior del dia. El resultado se publica en la sala de
        # la ruta como "tracking:route_status" para que familias y admin vean
        # que el seguimiento arranco.
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("La ruta indicada no existe.")
        if not _require_route_driver(ruta):
            return
        if not ruta.conductor or not ruta.conductor.puede_transmitir_gps:
            return _emit_error("La suscripcion del conductor no permite transmitir GPS.", scope="gps")
        ruta.recorrido_activo = True
        ruta.inicio_recorrido_en = datetime.utcnow()
        ruta.fin_recorrido_en = None
        ruta.latitud_actual = None
        ruta.longitud_actual = None
        ruta.precision_actual = None
        ruta.velocidad_actual = None
        ruta.rumbo_actual = None
        ruta.ultima_ubicacion_en = None
        # Al iniciar una nueva jornada se limpia el rastro anterior para que el
        # mapa solo muestre el recorrido actual.
        UbicacionRuta.query.filter_by(ruta_id=ruta.id).delete(synchronize_session=False)
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
        # Emitido por el CONDUCTOR al terminar la jornada. Payload:
        # {"ruta_id": ...}. Marca la ruta como "sin recorrido", guarda la hora
        # de fin y avisa a la sala con "tracking:route_status" para que las
        # familias dejen de esperar el GPS en vivo. No borra la traza: solo
        # deja de aceptar nuevas ubicaciones hasta el siguiente inicio.
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
        # Evento mas frecuente del sistema. Emitido por el GPS del telefono del
        # CONDUCTOR. Payload esperado: {"ruta_id", "lat", "lng", "accuracy",
        # "speed", "heading", "captured_at"}.
        #
        # Antes de aceptar el punto se valida: que sea el conductor de la ruta,
        # que tenga permiso de GPS, que el recorrido este activo y que las
        # coordenadas sean coherentes. Luego se aplican filtros de calidad
        # (ignorar lecturas muy imprecisas, descartar saltos imposibles y
        # suavizar el temblor). Finalmente se persiste el estado de la ruta,
        # opcionalmente se guarda el punto en la traza y se difunde
        # "tracking:location" a la sala para que familias/admin vean la buseta.
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para actualizar la ubicacion.")
        if not _require_route_driver(ruta):
            return
        if not ruta.conductor or not ruta.conductor.puede_transmitir_gps:
            return _emit_error("El conductor no tiene permiso activo para transmitir ubicacion.", scope="gps")
        if not ruta.recorrido_activo:
            return _emit_error("Primero debes iniciar el recorrido antes de transmitir ubicación.", scope="gps")
        latitud, longitud = _validate_coordinates(data)
        if latitud is None or longitud is None:
            return _emit_error("Las coordenadas enviadas no son validas.", scope="gps")
        precision = _normalize_positive_float(data.get("accuracy"))
        velocidad = _normalize_positive_float(data.get("speed"))
        rumbo = _normalize_heading(data.get("heading"))
        capturada_en = _parse_client_timestamp(data.get("captured_at"))

        # Estos filtros reducen saltos falsos y lecturas de baja calidad antes
        # de persistir la ubicacion y emitirla a los clientes.
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
        # Emitido por el CONDUCTOR al marcar subida/bajada de un estudiante.
        # Payload: {"ruta_id", "estudiante_id", "estado", "observacion"}.
        # Solo el conductor de la ruta puede hacerlo y el estudiante debe
        # pertenecer a esa ruta (validacion de pertenencia mas abajo).
        #
        # Se busca la asistencia de HOY para ese estudiante; si no existe se
        # crea y si existe se actualiza. Segun el estado se sellan las horas de
        # subida/bajada. El resultado se difunde a la sala como
        # "tracking:attendance" para que las familias vean el cambio en vivo.
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
        observacion = data.get("observacion")
        if observacion is not None:
            observacion = str(observacion).strip()[:255] or None
        asistencia.observacion = observacion
        if estado == EstadoAsistencia.SUBIO:
            asistencia.hora_subida = datetime.utcnow()
        if estado == EstadoAsistencia.BAJO:
            asistencia.hora_bajada = datetime.utcnow()
        _commit_changes()
        emit("tracking:attendance", asistencia.to_socket_payload(), to=ruta.socket_room)

    @socketio.on("chat:message")
    def handle_chat_message(data):
        # Emitido por CONDUCTOR o FAMILIA para enviar un mensaje al chat de la
        # ruta. Payload: {"ruta_id", "contenido", "remitente_nombre",
        # "remitente_tipo"}.
        #
        # El chat esta AISLADO POR RUTA: el mensaje solo se guarda y se emite a
        # la sala de esa ruta (ruta.socket_room), nunca a las demas. Se aplican
        # dos limites de ritmo: uno por IP general (rapido, todo remitente) y
        # otro por IP especifico para familias (mas estricto).
        #
        # Segun el tipo de remitente hay reglas distintas:
        #  - CONDUCTOR: debe estar autenticado y su nombre se toma del sistema.
        #  - PADRE: solo si el conductor no cerro el chat de familias.
        if demasiadas_peticiones(f"chat:{cliente_ip()}", MAX_MENSAJES_CHAT, VENTANA_CHAT_SEGUNDOS):
            return _emit_error("Has enviado demasiados mensajes seguidos. Espera un momento.")
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para enviar el mensaje.")
        contenido = (data.get("contenido") or "").strip()[:1000]
        remitente_nombre = (data.get("remitente_nombre") or "Invitado").strip()[:60]
        remitente_tipo = _parse_sender_type(data.get("remitente_tipo"))
        if not contenido:
            return _emit_error("El mensaje no puede estar vacio.")
        if remitente_tipo is None:
            return _emit_error("El tipo de remitente no es valido.")
        if remitente_tipo == TipoRemitenteChat.CONDUCTOR:
            # Se fuerza el nombre real del conductor para que nadie pueda
            # suplantarlo enviando otro nombre en el payload.
            if not _require_route_driver(ruta):
                return
            remitente_nombre = ruta.conductor.nombre_completo
            conductor_id = ruta.conductor_id
        elif remitente_tipo == TipoRemitenteChat.PADRE:
            conductor_id = None
            # El conductor puede cerrar el chat de familias de su ruta.
            if not ruta.chat_familias_habilitado:
                return _emit_error("El conductor cerró el chat de familias.", scope="chat-cerrado")
            # Cupo por IP para el chat de familias: al llegar al limite se deja
            # de aceptar el mensaje, evitando spam y caidas de la aplicacion.
            clave_cupo = f"chat-padre:{cliente_ip()}"
            if cupo_agotado(clave_cupo, MAX_MENSAJES_CHAT_PADRE):
                return _emit_error(
                    "Alcanzaste el limite de {} mensajes en este chat.".format(MAX_MENSAJES_CHAT_PADRE),
                    scope="chat",
                )
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
        # Difusion a la sala: todos los conectados a ESTA ruta reciben el
        # mensaje nuevo (familias, conductor y admin viendo esa ruta).
        emit("chat:new_message", mensaje.to_socket_payload(), to=ruta.socket_room)
        if remitente_tipo == TipoRemitenteChat.PADRE:
            # Aviso solo al emisor con cuantos mensajes le quedan en su cupo
            # (no se envia a la sala para no exponer el cupo de otros).
            emit(
                "chat:quota",
                {
                    "ruta_id": ruta.id,
                    "maximo": MAX_MENSAJES_CHAT_PADRE,
                    "restantes": cupo_restante(clave_cupo, MAX_MENSAJES_CHAT_PADRE),
                },
            )

    @socketio.on("chat:set_familias")
    def handle_chat_set_familias(data):
        # El CONDUCTOR abre o cierra el chat de familias de su propia ruta.
        # Payload: {"ruta_id", "habilitado": true/false}. Solo el conductor
        # asignado puede cambiarlo. El nuevo estado se publica a la sala como
        # "chat:state" para que las familias vean si el chat esta habilitado.
        data = data if isinstance(data, dict) else {}
        ruta = _resolve_route(data)
        if not ruta:
            return _emit_error("No se encontro la ruta para cambiar el chat.")
        if not _require_route_driver(ruta):
            return
        ruta.chat_familias_habilitado = bool(data.get("habilitado"))
        try:
            _commit_changes()
        except SQLAlchemyError:
            return _emit_error("No se pudo guardar el estado del chat.")
        socketio.emit(
            "chat:state",
            {"ruta_id": ruta.id, "habilitado": ruta.chat_familias_habilitado},
            to=ruta.socket_room,
        )


def _resolve_route(data):
    """Resuelve la ruta del payload validando acceso por sesion o PIN.

    Toma el `ruta_id` del payload, lo normaliza a entero y delega en
    `route_for_session`, que comprueba si el usuario actual (conductor
    autenticado o sesion con el PIN de la ruta) tiene permiso sobre ella.
    Devuelve el objeto Ruta autorizado o None si no hay acceso o el id no es
    valido. Es el filtro central de autorizacion para casi todos los eventos.
    """
    if not isinstance(data, dict):
        return None
    ruta_id = data.get("ruta_id")
    if not ruta_id:
        return None
    try:
        ruta_id = int(ruta_id)
    except (TypeError, ValueError):
        return None
    return route_for_session(ruta_id)


def _require_route_driver(ruta):
    """Verifica que la sesion sea el conductor activo asignado a la ruta.

    Devuelve True solo si el conductor de la sesion coincide con el de la ruta,
    la ruta esta activa y el conductor esta activo. Si no, emite un error de
    permisos y devuelve False. Protege los eventos sensibles (GPS, asistencia,
    inicio/fin de recorrido y control del chat).
    """
    # Solo el conductor autenticado y asignado puede emitir eventos sensibles
    # como GPS, cambios de asistencia o inicio/fin del recorrido.
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
    """Extrae y valida lat/lng del payload; devuelve (None, None) si son invalidas."""
    latitud = _to_float(data.get("lat") if isinstance(data, dict) else None)
    longitud = _to_float(data.get("lng") if isinstance(data, dict) else None)
    if latitud is None or longitud is None:
        return None, None
    if not -90 <= latitud <= 90 or not -180 <= longitud <= 180:
        return None, None
    return latitud, longitud


def _to_float(value):
    """Convierte a float de forma segura; devuelve None si no es numerico."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_positive_float(value):
    """Normaliza magnitudes no negativas (precision, velocidad); None si es negativa."""
    number = _to_float(value)
    if number is None or number < 0:
        return None
    return number


def _normalize_heading(value):
    """Normaliza el rumbo al rango [0, 360) grados; None si falta o no es numerico."""
    heading = _to_float(value)
    if heading is None:
        return None
    heading = heading % 360
    return heading


def _parse_client_timestamp(value):
    """Interpreta la marca de tiempo del cliente (segundos o milisegundos) a datetime UTC."""
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
    """Descarta lecturas GPS demasiado imprecisas (>120 m) cuando ya hay posicion previa.

    Evita que una lectura de baja calidad desplace bruscamente la ubicacion
    mostrada. La primera lectura siempre se acepta para tener una referencia.
    """
    if precision is None:
        return False
    if ruta.latitud_actual is None or ruta.longitud_actual is None:
        return False
    return precision > 120


def _smooth_location(ruta, latitud, longitud, precision):
    """Suaviza la posicion mezclando la lectura nueva con la anterior.

    Cuando el GPS reporta poca precision y el desplazamiento es pequeno, se
    devuelve un punto intermedio (no el crudo) para que el marcador en el mapa
    no "tiemble". El factor de mezcla sube cuanto mayor sea el error de
    precision. Si el movimiento es grande o la precision es buena, se devuelve
    la lectura original sin cambios.
    """
    # Mezcla ligeramente la lectura nueva con la anterior cuando el GPS reporta
    # poca precision y el cambio es pequeno. Asi se reduce el "temblor" visual.
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
    """Detecta y descarta saltos de ubicacion fisicamente imposibles.

    Calcula la velocidad implicita entre la posicion nueva y la anterior. Si
    supera un umbral absurdo para una buseta escolar (130 km/h) y la lectura
    tiene mala precision, se considera un salto falso (ruido del GPS) y se
    ignora. Si no hay posicion previa o el movimiento es pequeno, se acepta.
    """
    # Descarta saltos imposibles para una buseta escolar: por ejemplo cambios de
    # cientos de metros en pocos segundos con mala precision.
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
    """Distancia en metros entre dos coordenadas usando la formula de Haversine.

    Devuelve None si alguna coordenada no es numerica. Se usa para medir
    movimientos y compararlos con umbrales (saltos, suavizado, guardado).
    """
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
    """Convierte el texto recibido en un valor del enum EstadoAsistencia (o None)."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    for status in EstadoAsistencia:
        if status.value == normalized:
            return status
    return None


def _parse_sender_type(value):
    """Convierte el texto recibido en un valor del enum TipoRemitenteChat (o None)."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    for sender_type in TipoRemitenteChat:
        if sender_type.value == normalized:
            return sender_type
    return None


def _commit_changes():
    """Hace commit de la sesion; si falla, revierte y propaga el error."""
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        raise


def _emit_error(message, scope=None):
    # Envia un aviso de error unicamente al cliente que lo provoco (emit sin
    # `to=`). `scope` permite al cliente distinguir si el aviso es del GPS del
    # telefono ("gps") o de otra funcion (chat, asistencia, PIN, etc.). Asi el
    # indicador de GPS no se marca como error por problemas que no son del GPS.
    payload = {"message": message}
    if scope is not None:
        payload["scope"] = scope
    emit("system:error", payload)
