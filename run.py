"""Punto de entrada de Ruta Confianza."""
import os

from app import create_app, socketio
from config import DevelopmentConfig, ProductionConfig

# En Render (o con FLASK_ENV=production) se usa la configuracion de produccion.
IS_PRODUCTION = os.getenv("FLASK_ENV", "").lower() == "production" or bool(os.getenv("RENDER"))

app = create_app(ProductionConfig if IS_PRODUCTION else DevelopmentConfig)


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=app.config.get("DEBUG", False),
    )
