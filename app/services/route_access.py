"""Autorizacion de acceso a datos y eventos de una ruta."""
from flask import session
from app import db
from app.models import AdminUser, Ruta


def route_for_session(ruta_id):
    """Devuelve una ruta activa solo si la sesion actual puede verla."""
    ruta = Ruta.query.filter_by(id=ruta_id, activa=True).first()
    if ruta is None:
        return None

    if session.get("tracking_ruta_id") == ruta.id:
        return ruta

    conductor_id = session.get("conductor_id")
    if (
        conductor_id == ruta.conductor_id
        and ruta.conductor is not None
        and ruta.conductor.activo
    ):
        return ruta

    admin_id = session.get("admin_user_id")
    if admin_id is not None:
        administrador = db.session.get(AdminUser, admin_id)
        if administrador is not None and administrador.activo:
            return ruta

    return None
