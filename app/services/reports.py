"""Generacion de reportes descargables en CSV y PDF para el panel administrativo.

Centraliza las consultas y el formateo de cada reporte para que las rutas solo
tengan que pedir el nombre del reporte y el formato de salida.
"""
import csv
import io
from datetime import date, datetime

from app.models import (
    Asistencia,
    Conductor,
    EstadoAsistencia,
    Estudiante,
    MensajeContacto,
    Ruta,
    SuscripcionConductor,
)

FORMATO_FECHA = "%Y-%m-%d %H:%M"
FORMATO_DIA = "%Y-%m-%d"


def _fecha_hora(valor):
    if isinstance(valor, datetime):
        return valor.strftime(FORMATO_FECHA)
    if isinstance(valor, date):
        return valor.strftime(FORMATO_DIA)
    return ""


def _texto(valor, por_defecto=""):
    if valor is None:
        return por_defecto
    texto = str(valor).strip()
    return texto if texto else por_defecto


def _si_no(valor):
    return "Si" if valor else "No"


def _estado_asistencia(estado):
    etiquetas = {
        EstadoAsistencia.SUBIO: "Subio",
        EstadoAsistencia.BAJO: "Bajo",
        EstadoAsistencia.NO_ASISTIO: "No asistio",
        EstadoAsistencia.PENDIENTE: "Pendiente",
    }
    return etiquetas.get(estado, _texto(estado))


# --- Constructores de cada reporte -------------------------------------------

def _reporte_conductores(desde=None, hasta=None):
    columnas = [
        "Conductor", "Documento", "Telefono", "Correo", "Vehiculo", "Placa",
        "Ciudad", "Estado suscripcion", "Vence", "Activo", "GPS habilitado",
    ]
    filas = []
    for conductor in Conductor.query.order_by(Conductor.nombres.asc()).all():
        filas.append([
            conductor.nombre_completo,
            _texto(conductor.documento),
            _texto(conductor.telefono),
            _texto(conductor.email),
            _texto(conductor.nombre_vehiculo),
            _texto(conductor.placa_vehiculo),
            _texto(conductor.ciudad, "Sin ciudad"),
            conductor.estado_suscripcion.value if conductor.estado_suscripcion else "",
            _fecha_hora(conductor.suscripcion_vence_en),
            _si_no(conductor.activo),
            _si_no(conductor.gps_habilitado),
        ])
    return columnas, filas


def _reporte_rutas(desde=None, hasta=None):
    columnas = [
        "Ruta", "Codigo", "PIN", "Ciudad", "Conductor", "Origen", "Destino",
        "Salida", "Regreso", "Paradas", "Activa", "En recorrido",
    ]
    filas = []
    for ruta in Ruta.query.order_by(Ruta.nombre.asc()).all():
        filas.append([
            ruta.nombre,
            _texto(ruta.codigo),
            _texto(ruta.pin_acceso),
            _texto(ruta.ciudad, "Sin ciudad"),
            ruta.conductor.nombre_completo if ruta.conductor else "Sin asignar",
            _texto(ruta.origen, "Sin definir"),
            _texto(ruta.destino, "Sin definir"),
            _texto(ruta.horario_salida, "-"),
            _texto(ruta.horario_regreso, "-"),
            len(ruta.estudiantes),
            _si_no(ruta.activa),
            _si_no(ruta.recorrido_activo),
        ])
    return columnas, filas


def _reporte_estudiantes(desde=None, hasta=None):
    columnas = [
        "Estudiante", "Documento", "Ruta", "Institucion", "Grado", "Direccion",
        "Barrio", "Ciudad", "Acudiente", "Telefono acudiente", "Orden",
        "Georreferenciado", "Activo",
    ]
    filas = []
    consulta = Estudiante.query.order_by(Estudiante.nombres.asc()).all()
    for estudiante in consulta:
        geolocalizado = estudiante.latitud is not None and estudiante.longitud is not None
        filas.append([
            estudiante.nombre_completo,
            _texto(estudiante.documento),
            estudiante.ruta.nombre if estudiante.ruta else "Sin ruta",
            _texto(estudiante.institucion, "Sin institucion"),
            _texto(estudiante.grado, "Sin grado"),
            _texto(estudiante.direccion, "Sin direccion"),
            _texto(estudiante.barrio, "-"),
            _texto(estudiante.ciudad, "-"),
            _texto(estudiante.acudiente_nombre),
            _texto(estudiante.acudiente_telefono),
            _texto(estudiante.orden_parada, "-"),
            _si_no(geolocalizado),
            _si_no(estudiante.activo),
        ])
    return columnas, filas


def _reporte_asistencias(desde=None, hasta=None):
    columnas = [
        "Fecha", "Ruta", "Estudiante", "Conductor", "Estado",
        "Hora subida", "Hora bajada", "Observacion",
    ]
    consulta = Asistencia.query
    if desde:
        consulta = consulta.filter(Asistencia.fecha_operacion >= desde)
    if hasta:
        consulta = consulta.filter(Asistencia.fecha_operacion <= hasta)
    consulta = consulta.order_by(Asistencia.fecha_operacion.desc(), Asistencia.id.desc())

    filas = []
    for asistencia in consulta.all():
        filas.append([
            _fecha_hora(asistencia.fecha_operacion),
            asistencia.ruta.nombre if asistencia.ruta else "Sin ruta",
            asistencia.estudiante.nombre_completo if asistencia.estudiante else "Sin estudiante",
            asistencia.ruta.conductor.nombre_completo if asistencia.ruta and asistencia.ruta.conductor else "Sin conductor",
            _estado_asistencia(asistencia.estado),
            asistencia.hora_subida.strftime("%H:%M") if asistencia.hora_subida else "-",
            asistencia.hora_bajada.strftime("%H:%M") if asistencia.hora_bajada else "-",
            _texto(asistencia.observacion, "-"),
        ])
    return columnas, filas


def _reporte_suscripciones(desde=None, hasta=None):
    columnas = [
        "Conductor", "Estado", "Fecha inicio", "Fecha fin", "Valor",
        "Observaciones", "Activado por", "Registrado",
    ]
    filas = []
    registros = SuscripcionConductor.query.order_by(SuscripcionConductor.created_at.desc()).all()
    for registro in registros:
        filas.append([
            registro.conductor.nombre_completo if registro.conductor else "Sin conductor",
            registro.estado.value if registro.estado else "",
            _fecha_hora(registro.fecha_inicio),
            _fecha_hora(registro.fecha_fin),
            f"{registro.valor:.2f}" if registro.valor is not None else "-",
            _texto(registro.observaciones, "-"),
            _texto(registro.activado_por, "-"),
            _fecha_hora(registro.created_at),
        ])
    return columnas, filas


def _reporte_mensajes(desde=None, hasta=None):
    columnas = ["Fecha", "Nombre", "Telefono", "Correo", "Asunto", "Mensaje", "Atendido"]
    filas = []
    for mensaje in MensajeContacto.query.order_by(MensajeContacto.created_at.desc()).all():
        filas.append([
            _fecha_hora(mensaje.created_at),
            mensaje.nombre,
            _texto(mensaje.telefono, "-"),
            _texto(mensaje.email, "-"),
            _texto(mensaje.asunto, "Sin asunto"),
            mensaje.mensaje,
            _si_no(mensaje.atendido),
        ])
    return columnas, filas


def _reporte_resumen(desde=None, hasta=None):
    """Indicadores generales del sistema en formato tabla."""
    from app.models import EstadoSolicitud, EstadoSuscripcion, SolicitudConductor

    hoy = date.today()
    conductores_activos = Conductor.query.filter_by(activo=True).count()
    suscripciones_activas = Conductor.query.filter_by(estado_suscripcion=EstadoSuscripcion.ACTIVA).count()
    rutas_activas = Ruta.query.filter_by(activa=True).count()
    rutas_en_recorrido = Ruta.query.filter_by(recorrido_activo=True).count()
    estudiantes_activos = Estudiante.query.filter_by(activo=True).count()
    sin_geolocalizar = Estudiante.query.filter(Estudiante.latitud.is_(None)).count()
    solicitudes_pendientes = SolicitudConductor.query.filter_by(estado=EstadoSolicitud.PENDIENTE).count()
    mensajes_nuevos = MensajeContacto.query.filter_by(atendido=False).count()
    asistencias_hoy = Asistencia.query.filter_by(fecha_operacion=hoy).count()

    indicadores = [
        ("Total conductores", Conductor.query.count()),
        ("Conductores activos", conductores_activos),
        ("Suscripciones activas", suscripciones_activas),
        ("Total rutas", Ruta.query.count()),
        ("Rutas activas", rutas_activas),
        ("Rutas en recorrido ahora", rutas_en_recorrido),
        ("Total estudiantes", Estudiante.query.count()),
        ("Estudiantes activos", estudiantes_activos),
        ("Estudiantes sin geolocalizar", sin_geolocalizar),
        ("Solicitudes pendientes", solicitudes_pendientes),
        ("Mensajes de contacto sin atender", mensajes_nuevos),
        ("Asistencias registradas hoy", asistencias_hoy),
    ]
    return ["Indicador", "Valor"], [[nombre, valor] for nombre, valor in indicadores]


REPORTES = {
    "conductores": {
        "titulo": "Conductores",
        "descripcion": "Datos de contacto, vehiculo y estado de suscripcion.",
        "icono": "bi-person-badge",
        "constructor": _reporte_conductores,
    },
    "rutas": {
        "titulo": "Rutas",
        "descripcion": "Trayectos, PIN de acceso, conductor asignado y paradas.",
        "icono": "bi-signpost-split",
        "constructor": _reporte_rutas,
    },
    "estudiantes": {
        "titulo": "Estudiantes",
        "descripcion": "Matriculados, ubicacion, acudientes y estado de georreferenciacion.",
        "icono": "bi-mortarboard",
        "constructor": _reporte_estudiantes,
    },
    "asistencias": {
        "titulo": "Asistencias",
        "descripcion": "Registro de subidas y bajadas por ruta y fecha.",
        "icono": "bi-clipboard-check",
        "constructor": _reporte_asistencias,
        "filtra_fechas": True,
    },
    "suscripciones": {
        "titulo": "Suscripciones",
        "descripcion": "Historial de activaciones y vencimientos de los conductores.",
        "icono": "bi-credit-card",
        "constructor": _reporte_suscripciones,
    },
    "mensajes": {
        "titulo": "Mensajes de contacto",
        "descripcion": "Consultas recibidas desde el formulario de la landing.",
        "icono": "bi-envelope-paper",
        "constructor": _reporte_mensajes,
    },
    "resumen": {
        "titulo": "Resumen general",
        "descripcion": "Indicadores consolidados de toda la operacion.",
        "icono": "bi-bar-chart-line",
        "constructor": _reporte_resumen,
    },
}


def construir_reporte(nombre, desde=None, hasta=None):
    """Devuelve (definicion, columnas, filas) para el reporte solicitado."""
    definicion = REPORTES.get(nombre)
    if definicion is None:
        raise KeyError(f"El reporte '{nombre}' no existe.")
    columnas, filas = definicion["constructor"](desde=desde, hasta=hasta)
    return definicion, columnas, filas


def generar_csv(columnas, filas):
    """Genera el contenido CSV (con BOM para que Excel respete los acentos)."""
    buffer = io.StringIO()
    escritor = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    escritor.writerow(columnas)
    for fila in filas:
        escritor.writerow(["" if valor is None else valor for valor in fila])
    return "\ufeff" + buffer.getvalue()


def generar_pdf(columnas, filas, titulo, subtitulo=""):
    """Genera un PDF con la tabla del reporte. Devuelve None si falta reportlab."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        return None

    buffer = io.BytesIO()
    ancho_pagina, alto_pagina = landscape(letter)
    documento = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=titulo,
    )

    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle(
        "TituloReporte", parent=estilos["Heading1"], fontSize=16, spaceAfter=2, textColor=colors.HexColor("#0f172a")
    )
    estilo_subtitulo = ParagraphStyle(
        "SubReporte", parent=estilos["Normal"], fontSize=9, textColor=colors.HexColor("#64748b"), spaceAfter=8
    )
    estilo_celda = ParagraphStyle("CeldaReporte", parent=estilos["Normal"], fontSize=7.5, leading=9.5)
    estilo_encabezado = ParagraphStyle(
        "EncabezadoReporte", parent=estilos["Normal"], fontSize=7.5, leading=9.5,
        textColor=colors.white, fontName="Helvetica-Bold",
    )

    elementos = [Paragraph(titulo, estilo_titulo)]
    marca = f"Generado el {datetime.now().strftime('%Y-%m-%d %H:%M')} · {len(filas)} registros"
    if subtitulo:
        marca = f"{subtitulo} · {marca}"
    elementos.append(Paragraph(marca, estilo_subtitulo))

    encabezados = [Paragraph(str(columna), estilo_encabezado) for columna in columnas]
    cuerpo = [encabezados]
    for fila in filas:
        cuerpo.append([Paragraph("" if valor is None else str(valor), estilo_celda) for valor in fila])

    if not filas:
        cuerpo.append([Paragraph("Sin registros para mostrar.", estilo_celda)] + [Paragraph("", estilo_celda)] * (len(columnas) - 1))

    ancho_disponible = ancho_pagina - 24 * mm
    tabla = Table(cuerpo, colWidths=[ancho_disponible / len(columnas)] * len(columnas), repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d4ed8")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    elementos.append(tabla)
    elementos.append(Spacer(1, 6 * mm))

    documento.build(elementos)
    buffer.seek(0)
    return buffer
