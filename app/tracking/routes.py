"""Rutas del portal de seguimiento para padres."""
from datetime import date
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from app.models import Asistencia, MensajeChat, Ruta, UbicacionRuta
from app.services.rate_limit import (
    BLOQUEO_PIN_SEGUNDOS,
    MAX_FALLOS_PIN,
    bloqueo_restante,
    cliente_ip,
    demasiadas_peticiones,
    espera_legible,
    olvidar,
    registrar_fallo,
)

tracking_bp = Blueprint("tracking_bp", __name__, url_prefix="/tracking")

# El portal de familias consulta el mapa cada 8 segundos: el tope deja margen
# de sobra para el uso normal y solo frena el abuso automatizado.
MAX_CONSULTAS_SEGUIMIENTO = 180
VENTANA_SEGUIMIENTO_SEGUNDOS = 60


@tracking_bp.route("/", methods=["GET", "POST"])
def ingreso():
    if request.method == "POST":
        clave = f"pin-entrada:{cliente_ip()}"
        espera = bloqueo_restante(clave)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("tracking/ingreso.html"), 429

        pin = request.form.get("pin", "").strip()
        ruta = Ruta.query.filter_by(pin_acceso=pin).first()
        if ruta:
            olvidar(clave)
            return redirect(url_for("tracking_bp.sala_ruta", pin=pin))

        espera = registrar_fallo(clave, MAX_FALLOS_PIN, BLOQUEO_PIN_SEGUNDOS)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("tracking/ingreso.html"), 429
        flash("El PIN ingresado no corresponde a una ruta activa.", "danger")
    return render_template("tracking/ingreso.html")


@tracking_bp.route("/ruta/<pin>")
def sala_ruta(pin):
    ruta = Ruta.query.filter_by(pin_acceso=pin, activa=True).first_or_404()
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
        "tracking/sala.html",
        ruta=ruta,
        asistencias_hoy=asistencias_hoy,
        mensajes_recientes=mensajes_recientes,
        map_defaults={
            "lat": ruta.latitud_actual if ruta.latitud_actual is not None else current_app.config["MAP_DEFAULT_LAT"],
            "lng": ruta.longitud_actual if ruta.longitud_actual is not None else current_app.config["MAP_DEFAULT_LNG"],
            "zoom": current_app.config["MAP_DEFAULT_ZOOM"],
        },
    )


@tracking_bp.route("/api/ruta/<int:ruta_id>/recorrido")
def recorrido_real(ruta_id):
    """Entrega los últimos puntos GPS para reconstruir el trayecto real en el mapa."""
    if demasiadas_peticiones(
        f"seguimiento:{cliente_ip()}", MAX_CONSULTAS_SEGUIMIENTO, VENTANA_SEGUIMIENTO_SEGUNDOS
    ):
        return jsonify({"error": "Demasiadas consultas seguidas. Intenta de nuevo en un momento."}), 429
    pin = request.args.get("pin", "").strip()
    ruta = Ruta.query.filter_by(id=ruta_id, pin_acceso=pin, activa=True).first_or_404()
    puntos = list(
        reversed(
            UbicacionRuta.query.filter_by(ruta_id=ruta.id)
            .order_by(UbicacionRuta.registrada_en.desc())
            .limit(500)
            .all()
        )
    )
    return jsonify({
        "ruta_id": ruta.id,
        "recorrido_activo": ruta.recorrido_activo,
        "puntos": [punto.to_map_payload() for punto in puntos],
    })


@tracking_bp.route("/api/ruta/<int:ruta_id>/plan")
def plan_ruta(ruta_id):
    """Entrega la configuracion de paradas y destino para dibujar la ruta planeada."""
    if demasiadas_peticiones(
        f"seguimiento:{cliente_ip()}", MAX_CONSULTAS_SEGUIMIENTO, VENTANA_SEGUIMIENTO_SEGUNDOS
    ):
        return jsonify({"error": "Demasiadas consultas seguidas. Intenta de nuevo en un momento."}), 429
    pin = request.args.get("pin", "").strip()
    ruta = Ruta.query.filter_by(id=ruta_id, pin_acceso=pin, activa=True).first_or_404()
    return jsonify(ruta.to_navigation_payload())
