"""Punto de entrada de Ruta Confianza."""
import os

from app import create_app, socketio
from config import DevelopmentConfig, ProductionConfig

# Este archivo lo usan tanto `python run.py` en local como Gunicorn en Render.
# La decision de entorno se resuelve una sola vez para que el resto de la app
# siempre reciba la configuracion correcta desde el arranque.
# En Render (o con FLASK_ENV=production) se usa la configuracion de produccion.
IS_PRODUCTION = os.getenv("FLASK_ENV", "").lower() == "production" or bool(os.getenv("RENDER"))

# `app` queda expuesta a nivel de modulo porque Gunicorn importa `run:app`.
app = create_app(ProductionConfig if IS_PRODUCTION else DevelopmentConfig)


if __name__ == "__main__":
    # En desarrollo levantamos Flask-SocketIO directamente para conservar los
    # eventos en tiempo real sin depender de Gunicorn.
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=app.config.get("DEBUG", False),
    )
