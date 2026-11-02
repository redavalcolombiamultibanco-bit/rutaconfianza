"""Rutas del modulo comercial."""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from app import db
from app.models import Conductor, EstadoSuscripcion, MensajeContacto, SolicitudConductor

comercial_bp = Blueprint("comercial_bp", __name__)


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


@comercial_bp.route("/registro-conductor", methods=["POST"])
def registro_conductor():
    solicitud = SolicitudConductor(
        nombres=request.form.get("nombres", "").strip(),
        apellidos=request.form.get("apellidos", "").strip(),
        telefono=request.form.get("telefono", "").strip(),
        email=request.form.get("email", "").strip(),
        nombre_vehiculo=request.form.get("nombre_vehiculo", "").strip(),
        placa_vehiculo=request.form.get("placa_vehiculo", "").strip().upper(),
        capacidad_vehiculo=request.form.get("capacidad_vehiculo") or None,
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
    nombre = request.form.get("nombre", "").strip()
    email = request.form.get("email", "").strip().lower()
    telefono = request.form.get("telefono", "").strip()
    asunto = request.form.get("asunto", "").strip()
    mensaje = request.form.get("mensaje", "").strip()

    if not nombre or not mensaje or not (email or telefono):
        flash("Completa tu nombre, el mensaje y al menos un dato de contacto.", "warning")
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
