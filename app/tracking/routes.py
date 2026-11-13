"""Rutas del portal de seguimiento para padres."""
from datetime import date
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
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
from app.services.route_access import route_for_session

tracking_bp = Blueprint("tracking_bp", __name__, url_prefix="/tracking")


@tracking_bp.after_request
def no_cache_tracking_responses(response):
    """Evita conservar datos familiares en la cache del navegador."""
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


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

        session.pop("tracking_ruta_id", None)
        pin = request.form.get("pin", "").strip()
        ruta = Ruta.query.filter_by(pin_acceso=pin, activa=True).first()
        if ruta:
            olvidar(clave)
            session["tracking_ruta_id"] = ruta.id
            return redirect(url_for("tracking_bp.sala_ruta"))

        espera = registrar_fallo(clave, MAX_FALLOS_PIN, BLOQUEO_PIN_SEGUNDOS)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("tracking/ingreso.html"), 429
        flash("El PIN ingresado no corresponde a una ruta activa.", "danger")
    return render_template("tracking/ingreso.html")


@tracking_bp.route("/ruta", defaults={"ruta_id": None})
@tracking_bp.route("/ruta/<int:ruta_id>")
def sala_ruta(ruta_id):
    ruta_id = ruta_id or session.get("tracking_ruta_id")
    if ruta_id is None:
        return redirect(url_for("tracking_bp.ingreso"))
    ruta = route_for_session(ruta_id)
    if ruta is None:
        if session.get("tracking_ruta_id") == ruta_id:
            session.pop("tracking_ruta_id", None)
            flash("La ruta ya no está activa. Ingresa nuevamente con el PIN.", "warning")
            return redirect(url_for("tracking_bp.ingreso"))
        abort(404)

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


@tracking_bp.route("/salir", methods=["POST"])
def salir():
    session.pop("tracking_ruta_id", None)
    flash("Cerraste la sesión del portal de familias.", "success")
    return redirect(url_for("tracking_bp.ingreso"))


@tracking_bp.route("/api/ruta/<int:ruta_id>/recorrido")
def recorrido_real(ruta_id):
    """Entrega los últimos puntos GPS para reconstruir el trayecto real en el mapa."""
    ruta = route_for_session(ruta_id)
    if ruta is None:
        abort(404)
    if demasiadas_peticiones(
        f"seguimiento:{cliente_ip()}", MAX_CONSULTAS_SEGUIMIENTO, VENTANA_SEGUIMIENTO_SEGUNDOS
    ):
        return jsonify({"error": "Demasiadas consultas seguidas. Intenta de nuevo en un momento."}), 429
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
    ruta = route_for_session(ruta_id)
    if ruta is None:
        abort(404)
    if demasiadas_peticiones(
        f"seguimiento:{cliente_ip()}", MAX_CONSULTAS_SEGUIMIENTO, VENTANA_SEGUIMIENTO_SEGUNDOS
    ):
        return jsonify({"error": "Demasiadas consultas seguidas. Intenta de nuevo en un momento."}), 429
    return jsonify(ruta.to_navigation_payload())
