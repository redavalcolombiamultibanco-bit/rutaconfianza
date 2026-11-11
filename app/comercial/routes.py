"""Rutas del modulo comercial."""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from app import db
from app.models import Conductor, EstadoSuscripcion, MensajeContacto, SolicitudConductor
from app.services.rate_limit import (
    MAX_ENVIOS_FORMULARIO,
    VENTANA_FORMULARIO_SEGUNDOS,
    cliente_ip,
    demasiadas_peticiones,
)

comercial_bp = Blueprint("comercial_bp", __name__)

MESES_EN_ESPANOL = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)

# Municipios de Casanare que se mencionan en la pagina de cobertura. Sirve para
# que las busquedas locales ("ruta escolar Aguazul", "transporte escolar
# Villanueva") encuentren el texto sobre el servicio en el departamento.
MUNICIPIOS_CASANARE = (
    "Yopal", "Aguazul", "Villanueva", "Tauramena", "Monterrey", "Maní",
    "Paz de Ariporo", "Pore", "Nunchía", "Orocué", "San Luis de Palenque",
    "Trinidad", "Támara", "Hato Corozal", "Sabanalarga", "Sácama",
    "Recetor", "Chámeza", "La Salina",
)


@comercial_bp.route("/")
def home():
    conductores = (
        Conductor.query.filter_by(visible_en_directorio=True, activo=True)
        .order_by(Conductor.created_at.desc())
        .all()
    )
    indicadores = {
        "conductores": Conductor.query.filter_by(activo=True).count(),
        "suscripciones_activas": Conductor.query.filter_by(estado_suscripcion=EstadoSuscripcion.ACTIVA).count(),
        "familias_conectadas": max(len(conductores) * 8, 24),
    }
    return render_template(
        "comercial/home.html",
        conductores=conductores,
        indicadores=indicadores,
    )


@comercial_bp.route("/directorio")
def directorio():
    conductores = (
        Conductor.query.filter_by(visible_en_directorio=True, activo=True)
        .order_by(Conductor.nombres.asc())
        .all()
    )
    return render_template("comercial/directorio.html", conductores=conductores)


@comercial_bp.route("/terminos")
def terminos():
    """Publica los terminos y condiciones y la politica de tratamiento de datos."""
    hoy = datetime.utcnow()
    fecha_actualizacion = f"{hoy.day} de {MESES_EN_ESPANOL[hoy.month - 1]} de {hoy.year}"
    return render_template("comercial/terminos.html", fecha_actualizacion=fecha_actualizacion)


@comercial_bp.route("/rutas-escolares-yopal")
def rutas_escolares_yopal():
    """Pagina dedicada al servicio de ruta escolar en Yopal (SEO local)."""
    return render_template("comercial/rutas_escolares_yopal.html")


@comercial_bp.route("/rutas-escolares-casanare")
def rutas_escolares_casanare():
    """Pagina de cobertura del servicio de ruta escolar en Casanare (SEO local)."""
    return render_template(
        "comercial/rutas_escolares_casanare.html",
        municipios=MUNICIPIOS_CASANARE,
    )


@comercial_bp.route("/registro-conductor", methods=["POST"])
def registro_conductor():
    if demasiadas_peticiones(
        f"registro-conductor:{cliente_ip()}", MAX_ENVIOS_FORMULARIO, VENTANA_FORMULARIO_SEGUNDOS
    ):
        flash("Recibimos varios envios seguidos. Espera unos minutos e intenta de nuevo.", "warning")
        return redirect(url_for("comercial_bp.home") + "#contacto")
    if not request.form.get("acepta_terminos"):
        flash("Debes aceptar los términos y condiciones y la política de tratamiento de datos.", "warning")
        return redirect(url_for("comercial_bp.home") + "#contacto")

    solicitud = SolicitudConductor(
        nombres=request.form.get("nombres", "").strip(),
        apellidos=request.form.get("apellidos", "").strip(),
        telefono=request.form.get("telefono", "").strip(),
        email=request.form.get("email", "").strip(),
        nombre_vehiculo=request.form.get("nombre_vehiculo", "").strip(),
        placa_vehiculo=request.form.get("placa_vehiculo", "").strip().upper(),
        capacidad_vehiculo=request.form.get("capacidad_vehiculo", type=int),
        ciudad=request.form.get("ciudad", "").strip(),
        comentario=request.form.get("comentario", "").strip(),
    )
    db.session.add(solicitud)
    db.session.commit()
    flash("Tu solicitud fue registrada. El equipo de Ruta Confianza te contactara pronto.", "success")
    return redirect(url_for("comercial_bp.home"))


@comercial_bp.route("/contacto", methods=["POST"])
def contacto():
    """Recibe las consultas del formulario de contacto de la landing."""
    if demasiadas_peticiones(
        f"contacto:{cliente_ip()}", MAX_ENVIOS_FORMULARIO, VENTANA_FORMULARIO_SEGUNDOS
    ):
        flash("Recibimos varios mensajes seguidos. Espera unos minutos e intenta de nuevo.", "warning")
        return redirect(url_for("comercial_bp.home") + "#contacto")
    nombre = request.form.get("nombre", "").strip()
    email = request.form.get("email", "").strip().lower()
    telefono = request.form.get("telefono", "").strip()
    asunto = request.form.get("asunto", "").strip()
    mensaje = request.form.get("mensaje", "").strip()

    if not nombre or not mensaje or not (email or telefono):
        flash("Completa tu nombre, el mensaje y al menos un dato de contacto.", "warning")
        return redirect(url_for("comercial_bp.home") + "#contacto")

    if not request.form.get("acepta_terminos"):
        flash("Debes aceptar los términos y condiciones y la política de tratamiento de datos.", "warning")
        return redirect(url_for("comercial_bp.home") + "#contacto")

    db.session.add(
        MensajeContacto(
            nombre=nombre,
            email=email or None,
            telefono=telefono or None,
            asunto=asunto or None,
            mensaje=mensaje,
        )
    )
    db.session.commit()
    flash("Mensaje enviado. El equipo de Ruta Confianza te contactara pronto.", "success")
    return redirect(url_for("comercial_bp.home") + "#contacto")
