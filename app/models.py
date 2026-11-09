"""Modelos relacionales principales de Ruta Confianza."""
from datetime import datetime, date
import enum
from app import db


# Los enum centralizan estados validos y evitan cadenas "sueltas" repetidas
# en consultas, formularios y eventos en tiempo real.
class EstadoSuscripcion(enum.Enum):
    PENDIENTE = "pendiente"
    ACTIVA = "activa"
    INACTIVA = "inactiva"
    VENCIDA = "vencida"
    SUSPENDIDA = "suspendida"


class EstadoSolicitud(enum.Enum):
    PENDIENTE = "pendiente"
    APROBADA = "aprobada"
    RECHAZADA = "rechazada"


class EstadoAsistencia(enum.Enum):
    PENDIENTE = "pendiente"
    SUBIO = "subio"
    BAJO = "bajo"
    NO_ASISTIO = "no_asistio"


class TipoRemitenteChat(enum.Enum):
    CONDUCTOR = "conductor"
    PADRE = "padre"
    ADMIN = "admin"
    SISTEMA = "sistema"


class TimestampMixin:
    # Se reutiliza en varios modelos para no repetir las mismas columnas de
    # auditoria en cada tabla.
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AdminUser(TimestampMixin, db.Model):
    __tablename__ = "admin_users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    nombre = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    ultimo_acceso_en = db.Column(db.DateTime, nullable=True)
    pregunta_seguridad = db.Column(db.String(180), nullable=True)
    respuesta_seguridad_hash = db.Column(db.String(255), nullable=True)

    @property
    def nombre_mostrar(self):
        return self.nombre or self.username

    @property
    def tiene_recuperacion(self):
        return bool(self.pregunta_seguridad and self.respuesta_seguridad_hash)


class Conductor(TimestampMixin, db.Model):
    __tablename__ = "conductores"
    id = db.Column(db.Integer, primary_key=True)
    nombres = db.Column(db.String(80), nullable=False)
    apellidos = db.Column(db.String(80), nullable=False)
    documento = db.Column(db.String(30), unique=True, nullable=False)
    telefono = db.Column(db.String(25), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    pregunta_seguridad = db.Column(db.String(180), nullable=True)
    respuesta_seguridad_hash = db.Column(db.String(255), nullable=True)
    nombre_vehiculo = db.Column(db.String(100), nullable=False)
    placa_vehiculo = db.Column(db.String(15), unique=True, nullable=False)
    capacidad_vehiculo = db.Column(db.Integer, nullable=True)
    color_vehiculo = db.Column(db.String(40), nullable=True)
    ciudad = db.Column(db.String(80), nullable=True)
    descripcion_publica = db.Column(db.Text, nullable=True)
    foto_url = db.Column(db.String(255), nullable=True)
    visible_en_directorio = db.Column(db.Boolean, nullable=False, default=False)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    gps_habilitado = db.Column(db.Boolean, nullable=False, default=False)
    estado_suscripcion = db.Column(
        db.Enum(EstadoSuscripcion),
        nullable=False,
        default=EstadoSuscripcion.PENDIENTE,
    )
    suscripcion_inicia_en = db.Column(db.DateTime, nullable=True)
    suscripcion_vence_en = db.Column(db.DateTime, nullable=True)
    rutas = db.relationship("Ruta", backref="conductor", lazy=True, cascade="all, delete-orphan")
    suscripciones = db.relationship("SuscripcionConductor", backref="conductor", lazy=True, cascade="all, delete-orphan")
    mensajes = db.relationship("MensajeChat", backref="conductor_autor", lazy=True)

    @property
    def nombre_completo(self):
        return f"{self.nombres} {self.apellidos}".strip()

    @property
    def tiene_recuperacion(self):
        return bool(self.pregunta_seguridad and self.respuesta_seguridad_hash)

    @property
    def foto_publica_url(self):
        """Devuelve una URL usable tanto para fotos subidas como para enlaces externos."""
        if not self.foto_url:
            return None
        if self.foto_url.startswith(("http://", "https://", "/")):
            return self.foto_url
        return f"/static/{self.foto_url}"

    @property
    def suscripcion_vigente(self):
        if self.estado_suscripcion != EstadoSuscripcion.ACTIVA:
            return False
        if self.suscripcion_vence_en is None:
            return True
        return self.suscripcion_vence_en >= datetime.utcnow()

    @property
    def puede_transmitir_gps(self):
        # El GPS solo se habilita si la cuenta esta activa, el permiso fue
        # habilitado desde administracion y la suscripcion sigue vigente.
        return self.activo and self.gps_habilitado and self.suscripcion_vigente


class SolicitudConductor(TimestampMixin, db.Model):
    __tablename__ = "solicitudes_conductor"
    id = db.Column(db.Integer, primary_key=True)
    nombres = db.Column(db.String(80), nullable=False)
    apellidos = db.Column(db.String(80), nullable=False)
    telefono = db.Column(db.String(25), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    nombre_vehiculo = db.Column(db.String(100), nullable=False)
    placa_vehiculo = db.Column(db.String(15), nullable=False)
    capacidad_vehiculo = db.Column(db.Integer, nullable=True)
    ciudad = db.Column(db.String(80), nullable=True)
    comentario = db.Column(db.Text, nullable=True)
    estado = db.Column(db.Enum(EstadoSolicitud), nullable=False, default=EstadoSolicitud.PENDIENTE)
    revisado_por = db.Column(db.String(120), nullable=True)
    revisado_en = db.Column(db.DateTime, nullable=True)


class SuscripcionConductor(TimestampMixin, db.Model):
    __tablename__ = "suscripciones_conductor"
    id = db.Column(db.Integer, primary_key=True)
    conductor_id = db.Column(db.Integer, db.ForeignKey("conductores.id"), nullable=False, index=True)
    estado = db.Column(db.Enum(EstadoSuscripcion), nullable=False, default=EstadoSuscripcion.PENDIENTE)
    fecha_inicio = db.Column(db.DateTime, nullable=True)
    fecha_fin = db.Column(db.DateTime, nullable=True)
    valor = db.Column(db.Numeric(10, 2), nullable=True)
    observaciones = db.Column(db.Text, nullable=True)
    activado_por = db.Column(db.String(120), nullable=True)


class Ruta(TimestampMixin, db.Model):
    __tablename__ = "rutas"
    id = db.Column(db.Integer, primary_key=True)
    conductor_id = db.Column(db.Integer, db.ForeignKey("conductores.id"), nullable=False, index=True)
    nombre = db.Column(db.String(120), nullable=False)
    codigo = db.Column(db.String(30), unique=True, nullable=False)
    ciudad = db.Column(db.String(80), nullable=True)
    descripcion = db.Column(db.Text, nullable=True)
    origen = db.Column(db.String(150), nullable=True)
    origen_latitud = db.Column(db.Float, nullable=True)
    origen_longitud = db.Column(db.Float, nullable=True)
    destino = db.Column(db.String(150), nullable=True)
    destino_latitud = db.Column(db.Float, nullable=True)
    destino_longitud = db.Column(db.Float, nullable=True)
    horario_salida = db.Column(db.String(30), nullable=True)
    horario_regreso = db.Column(db.String(30), nullable=True)
    pin_acceso = db.Column(db.String(10), unique=True, nullable=False, index=True)
    activa = db.Column(db.Boolean, nullable=False, default=True)
    recorrido_activo = db.Column(db.Boolean, nullable=False, default=False)
    inicio_recorrido_en = db.Column(db.DateTime, nullable=True)
    fin_recorrido_en = db.Column(db.DateTime, nullable=True)
    latitud_actual = db.Column(db.Float, nullable=True)
    longitud_actual = db.Column(db.Float, nullable=True)
    precision_actual = db.Column(db.Float, nullable=True)
    velocidad_actual = db.Column(db.Float, nullable=True)
    rumbo_actual = db.Column(db.Float, nullable=True)
    ultima_ubicacion_en = db.Column(db.DateTime, nullable=True)
    estudiantes = db.relationship("Estudiante", backref="ruta", lazy=True, cascade="all, delete-orphan")
    asistencias = db.relationship("Asistencia", backref="ruta", lazy=True, cascade="all, delete-orphan")
    mensajes = db.relationship("MensajeChat", backref="ruta", lazy=True, cascade="all, delete-orphan")
    ubicaciones = db.relationship("UbicacionRuta", backref="ruta", lazy=True, cascade="all, delete-orphan")

    @property
    def socket_room(self):
        # Cada ruta tiene una sala propia para que familias, panel admin y
        # conductor escuchen solo los eventos que les corresponden.
        return f"ruta:{self.id}"

    def to_tracking_payload(self):
        # Payload corto para eventos frecuentes de Socket.IO.
        return {
            "ruta_id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "ciudad": self.ciudad,
            "pin_acceso": self.pin_acceso,
            "recorrido_activo": self.recorrido_activo,
            "latitud": self.latitud_actual,
            "longitud": self.longitud_actual,
            "precision": self.precision_actual,
            "velocidad": self.velocidad_actual,
            "rumbo": self.rumbo_actual,
            "ultima_ubicacion_en": self.ultima_ubicacion_en.isoformat() if self.ultima_ubicacion_en else None,
            "conductor": self.conductor.nombre_completo if self.conductor else None,
        }

    def to_navigation_payload(self):
        # Payload mas rico para dibujar el plan completo de la ruta en el mapa.
        return {
            "ruta_id": self.id,
            "nombre": self.nombre,
            "codigo": self.codigo,
            "ciudad": self.ciudad,
            "origen": self.origen,
            "destino": self.destino,
            "origen_coords": (
                {
                    "latitud": self.origen_latitud,
                    "longitud": self.origen_longitud,
                }
                if self.origen_latitud is not None and self.origen_longitud is not None
                else None
            ),
            "destino_coords": (
                {
                    "latitud": self.destino_latitud,
                    "longitud": self.destino_longitud,
                }
                if self.destino_latitud is not None and self.destino_longitud is not None
                else None
            ),
            "paradas": [
                estudiante.to_stop_payload()
                for estudiante in sorted(
                    [estudiante for estudiante in self.estudiantes if estudiante.activo],
                    key=lambda item: (
                        item.orden_parada is None,
                        item.orden_parada if item.orden_parada is not None else 9999,
                        item.nombres.lower(),
                    ),
                )
            ],
        }


class UbicacionRuta(db.Model):
    """Punto GPS de una ruta usado para dibujar su recorrido real."""

    __tablename__ = "ubicaciones_ruta"
    id = db.Column(db.Integer, primary_key=True)
    ruta_id = db.Column(db.Integer, db.ForeignKey("rutas.id"), nullable=False, index=True)
    latitud = db.Column(db.Float, nullable=False)
    longitud = db.Column(db.Float, nullable=False)
    precision = db.Column(db.Float, nullable=True)
    velocidad = db.Column(db.Float, nullable=True)
    rumbo = db.Column(db.Float, nullable=True)
    registrada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)

    def to_map_payload(self):
        return {
            "latitud": self.latitud,
            "longitud": self.longitud,
            "precision": self.precision,
            "velocidad": self.velocidad,
            "rumbo": self.rumbo,
            "registrada_en": self.registrada_en.isoformat() if self.registrada_en else None,
        }


class Estudiante(TimestampMixin, db.Model):
    __tablename__ = "estudiantes"
    id = db.Column(db.Integer, primary_key=True)
    ruta_id = db.Column(db.Integer, db.ForeignKey("rutas.id"), nullable=False, index=True)
    nombres = db.Column(db.String(80), nullable=False)
    apellidos = db.Column(db.String(80), nullable=False)
    documento = db.Column(db.String(30), unique=True, nullable=True)
    institucion = db.Column(db.String(120), nullable=True)
    grado = db.Column(db.String(40), nullable=True)
    direccion = db.Column(db.String(180), nullable=True)
    barrio = db.Column(db.String(80), nullable=True)
    ciudad = db.Column(db.String(80), nullable=True)
    referencia_direccion = db.Column(db.String(180), nullable=True)
    latitud = db.Column(db.Float, nullable=True)
    longitud = db.Column(db.Float, nullable=True)
    orden_parada = db.Column(db.Integer, nullable=True)
    acudiente_nombre = db.Column(db.String(120), nullable=False)
    acudiente_telefono = db.Column(db.String(25), nullable=False)
    acudiente_email = db.Column(db.String(120), nullable=True)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    asistencias = db.relationship("Asistencia", backref="estudiante", lazy=True, cascade="all, delete-orphan")

    @property
    def nombre_completo(self):
        return f"{self.nombres} {self.apellidos}".strip()

    def to_stop_payload(self):
        # Esta estructura alimenta tanto la interfaz del conductor como el
        # portal de familias al renderizar las paradas en el mapa.
        return {
            "id": self.id,
            "nombre": self.nombre_completo,
            "institucion": self.institucion,
            "grado": self.grado,
            "direccion": self.direccion,
            "barrio": self.barrio,
            "ciudad": self.ciudad or (self.ruta.ciudad if self.ruta else None),
            "referencia": self.referencia_direccion,
            "latitud": self.latitud,
            "longitud": self.longitud,
            "orden_parada": self.orden_parada,
            "acudiente_nombre": self.acudiente_nombre,
            "acudiente_telefono": self.acudiente_telefono,
            "activo": self.activo,
        }


class Asistencia(TimestampMixin, db.Model):
    __tablename__ = "asistencias"
    __table_args__ = (
        db.UniqueConstraint("estudiante_id", "fecha_operacion", name="uq_asistencia_estudiante_fecha"),
    )
    id = db.Column(db.Integer, primary_key=True)
    ruta_id = db.Column(db.Integer, db.ForeignKey("rutas.id"), nullable=False, index=True)
    estudiante_id = db.Column(db.Integer, db.ForeignKey("estudiantes.id"), nullable=False, index=True)
    conductor_id = db.Column(db.Integer, db.ForeignKey("conductores.id"), nullable=False, index=True)
    estado = db.Column(db.Enum(EstadoAsistencia), nullable=False, default=EstadoAsistencia.PENDIENTE)
    fecha_operacion = db.Column(db.Date, nullable=False, default=date.today, index=True)
    hora_subida = db.Column(db.DateTime, nullable=True)
    hora_bajada = db.Column(db.DateTime, nullable=True)
    observacion = db.Column(db.String(255), nullable=True)

    def to_socket_payload(self):
        # Se emite por socket para actualizar tableros sin recargar la pagina.
        return {
            "asistencia_id": self.id,
            "ruta_id": self.ruta_id,
            "estudiante_id": self.estudiante_id,
            "estudiante": self.estudiante.nombre_completo if self.estudiante else None,
            "estado": self.estado.value,
            "fecha_operacion": self.fecha_operacion.isoformat() if self.fecha_operacion else None,
            "hora_subida": self.hora_subida.isoformat() if self.hora_subida else None,
            "hora_bajada": self.hora_bajada.isoformat() if self.hora_bajada else None,
            "observacion": self.observacion,
        }


class MensajeChat(TimestampMixin, db.Model):
    __tablename__ = "mensajes_chat"
    id = db.Column(db.Integer, primary_key=True)
    ruta_id = db.Column(db.Integer, db.ForeignKey("rutas.id"), nullable=False, index=True)
    conductor_id = db.Column(db.Integer, db.ForeignKey("conductores.id"), nullable=True, index=True)
    remitente_tipo = db.Column(db.Enum(TipoRemitenteChat), nullable=False)
    remitente_nombre = db.Column(db.String(120), nullable=False)
    contenido = db.Column(db.Text, nullable=False)

    def to_socket_payload(self):
        # El chat comparte un formato simple y consistente para todos los
        # clientes conectados a la misma ruta.
        return {
            "mensaje_id": self.id,
            "ruta_id": self.ruta_id,
            "remitente_tipo": self.remitente_tipo.value,
            "remitente_nombre": self.remitente_nombre,
            "contenido": self.contenido,
            "enviado_en": self.created_at.isoformat() if self.created_at else None,
        }


class MensajeContacto(TimestampMixin, db.Model):
    """Consulta enviada desde el formulario de contacto de la landing."""
    __tablename__ = "mensajes_contacto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), nullable=True)
    telefono = db.Column(db.String(40), nullable=True)
    asunto = db.Column(db.String(120), nullable=True)
    mensaje = db.Column(db.Text, nullable=False)
    atendido = db.Column(db.Boolean, nullable=False, default=False)
    atendido_en = db.Column(db.DateTime, nullable=True)
