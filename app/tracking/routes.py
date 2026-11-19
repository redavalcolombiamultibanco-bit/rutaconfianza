"""Rutas del portal de seguimiento para padres.

Permite que las familias entren con el PIN de la ruta y vean en vivo la
ubicacion de la buseta, las paradas, la asistencia del dia y el chat. El
acceso se controla con un PIN validado por rate limit y guardado en sesion.
"""
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


# Se ejecuta tras cada respuesta del portal y anade cabeceras para que el
# navegador no guarde en cache datos sensibles de las familias.
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


# GET muestra el formulario del PIN; POST lo valida y abre la sesion de familias.
# Entrada: form pin. Salida: render_template o redirect a la sala de la ruta.
# Acceso publico, con bloqueo temporal por IP tras varios intentos fallidos.
@tracking_bp.route("/", methods=["GET", "POST"])
def ingreso():
    """Pantalla de acceso de las familias: pide el PIN de la ruta.

    El PIN es el unico requisito para entrar al portal de seguimiento.
    Despues de varios intentos fallidos se bloquea temporalmente la IP
    para evitar fuerza bruta contra el codigo de acceso.
    """
    if request.method == "POST":
        # Clave de rate limit por IP para frenar la fuerza bruta contra el PIN.
        clave = f"pin-entrada:{cliente_ip()}"
        espera = bloqueo_restante(clave)
        if espera:
            flash(
                f"Demasiados intentos fallidos. Vuelve a intentarlo en {espera_legible(espera)}.",
                "danger",
            )
            return render_template("tracking/ingreso.html"), 429

        # Limpia cualquier ruta previa antes de validar el nuevo PIN.
        session.pop("tracking_ruta_id", None)
        pin = request.form.get("pin", "").strip()
        # El PIN es la unica credencial: debe coincidir con una ruta activa.
        ruta = Ruta.query.filter_by(pin_acceso=pin, activa=True).first()
        if ruta:
            # PIN correcto: guarda la ruta en sesion y entra a la sala.
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


# GET de la sala de seguimiento. Toma la ruta de la URL o de la guardada en
# sesion (familias que entraron con PIN). Salida: render_template de la sala.
# Acceso permitido a familias con PIN, al conductor de la ruta y a un admin.
@tracking_bp.route("/ruta", defaults={"ruta_id": None})
@tracking_bp.route("/ruta/<int:ruta_id>")
def sala_ruta(ruta_id):
    """Sala de seguimiento en vivo de una ruta.

    Carga el mapa, las paradas, la asistencia del dia y los mensajes
    recientes del chat de la ruta. Solo acceden las familias que ingresaron
    con el PIN, el conductor asignado y los administradores.
    """
    # Si no llega ruta por URL, se usa la que quedo guardada en la sesion.
    ruta_id = ruta_id or session.get("tracking_ruta_id")
    if ruta_id is None:
        return redirect(url_for("tracking_bp.ingreso"))
    # route_for_session() comprueba que quien pide la ruta tenga permiso sobre ella.
    ruta = route_for_session(ruta_id)
    if ruta is None:
        # Para familias (ruta en sesion): avisa y vuelve al PIN; si no, 404.
        if session.get("tracking_ruta_id") == ruta_id:
            session.pop("tracking_ruta_id", None)
            flash("La ruta ya no está activa. Ingresa nuevamente con el PIN.", "warning")
            return redirect(url_for("tracking_bp.ingreso"))
        abort(404)

    # Asistencia del dia indexada por estudiante para reflejarla en la vista.
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
        "tracking/sala.html",
        ruta=ruta,
        asistencias_hoy=asistencias_hoy,
        mensajes_recientes=mensajes_recientes,
        # Centro inicial del mapa: ultima ubicacion de la buseta o el valor
        # por defecto de la configuracion de la aplicacion.
        map_defaults={
            "lat": ruta.latitud_actual if ruta.latitud_actual is not None else current_app.config["MAP_DEFAULT_LAT"],
            "lng": ruta.longitud_actual if ruta.longitud_actual is not None else current_app.config["MAP_DEFAULT_LNG"],
            "zoom": current_app.config["MAP_DEFAULT_ZOOM"],
},
    )


# POST para cerrar la sesion de familias: borra tracking_ruta_id.
# Salida: redirect a la pantalla del PIN. Lo usa la propia familia.
@tracking_bp.route("/salir", methods=["POST"])
def salir():
    """Cierra la sesion del portal de familias y vuelve a la pantalla de PIN."""
    session.pop("tracking_ruta_id", None)
    flash("Cerraste la sesión del portal de familias.", "success")
    return redirect(url_for("tracking_bp.ingreso"))


# GET (AJAX) con los ultimos puntos GPS de la ruta para dibujar el trayecto real.
# Entrada: ruta_id en la URL. Salida: JSON con los puntos. Requiere permiso de
# acceso a la ruta (PIN/conductor/admin) y esta limitado por rate limit.
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
    # Toma los ultimos 500 puntos y los revierte para dejarlos en orden cronologico.
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


# GET (AJAX) con la configuracion de paradas y destino de la ruta planeada.
# Entrada: ruta_id en la URL. Salida: JSON. Mismo control de acceso y rate limit.
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
