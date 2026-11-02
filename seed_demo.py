"""Carga un escenario inicial funcional para probar Ruta Confianza localmente."""
from datetime import date, datetime, timedelta
from app import create_app, db
from werkzeug.security import generate_password_hash
from app.models import (
    Asistencia,
    Conductor,
    EstadoAsistencia,
    EstadoSolicitud,
    EstadoSuscripcion,
    Estudiante,
    MensajeChat,
    Ruta,
    SolicitudConductor,
    SuscripcionConductor,
    TipoRemitenteChat,
)


def seed_demo_data():
    app = create_app()
    with app.app_context():
        if Conductor.query.count() > 0 or Ruta.query.count() > 0:
            print("La base de datos ya tiene informacion. No se cargaron datos demo.")
            return

        conductor = Conductor(
            nombres="Carlos",
            apellidos="Mendoza",
            documento="1099001122",
            telefono="3105550101",
            email="carlos.mendoza@rutaexacta.local",
            password_hash=generate_password_hash("RutaDemo123!"),
            nombre_vehiculo="Buseta Hyundai County",
            placa_vehiculo="YOP321",
            capacidad_vehiculo=18,
            color_vehiculo="Blanco con azul",
            ciudad="Yopal",
            descripcion_publica="Conductor con experiencia en rutas escolares y comunicacion permanente con familias.",
            visible_en_directorio=True,
            activo=True,
            gps_habilitado=True,
            estado_suscripcion=EstadoSuscripcion.ACTIVA,
            suscripcion_inicia_en=datetime.utcnow(),
            suscripcion_vence_en=datetime.utcnow() + timedelta(days=30),
        )
        db.session.add(conductor)
        db.session.flush()

        db.session.add(
            SuscripcionConductor(
                conductor_id=conductor.id,
                estado=EstadoSuscripcion.ACTIVA,
                fecha_inicio=conductor.suscripcion_inicia_en,
                fecha_fin=conductor.suscripcion_vence_en,
                valor=180000,
                observaciones="Plan demo cargado automaticamente.",
                activado_por="Seed",
            )
        )

        ruta = Ruta(
            conductor_id=conductor.id,
            nombre="Ruta Colegio Central",
            codigo="RTC-001",
            ciudad="Yopal",
            descripcion="Recorrido escolar urbano con seguimiento en vivo.",
            origen="Barrio La Campina",
            origen_latitud=5.34628,
            origen_longitud=-72.39153,
            destino="Colegio Central de Yopal",
            destino_latitud=5.33775,
            destino_longitud=-72.39586,
            horario_salida="06:00 a. m.",
            horario_regreso="01:30 p. m.",
            pin_acceso="FAM123",
            activa=True,
            recorrido_activo=False,
            latitud_actual=5.33775,
            longitud_actual=-72.39586,
            precision_actual=12.0,
            velocidad_actual=0.0,
            rumbo_actual=0.0,
            ultima_ubicacion_en=datetime.utcnow(),
        )
        db.session.add(ruta)
        db.session.flush()

        estudiantes = [
            Estudiante(
                ruta_id=ruta.id,
                nombres="Valentina",
                apellidos="Ruiz",
                documento="10010001",
                institucion="Colegio Central de Yopal",
                grado="5A",
                direccion="Carrera 19 # 28-15",
                barrio="La Campina",
                ciudad="Yopal",
                referencia_direccion="Casa esquinera color crema",
                latitud=5.3461,
                longitud=-72.3921,
                orden_parada=1,
                acudiente_nombre="Laura Ruiz",
                acudiente_telefono="3100000001",
                acudiente_email="laura.ruiz@familia.local",
            ),
            Estudiante(
                ruta_id=ruta.id,
                nombres="Samuel",
                apellidos="Perez",
                documento="10010002",
                institucion="Colegio Central de Yopal",
                grado="4B",
                direccion="Calle 24 # 21-40",
                barrio="El Centro",
                ciudad="Yopal",
                referencia_direccion="Frente a la tienda",
                latitud=5.3405,
                longitud=-72.3947,
                orden_parada=2,
                acudiente_nombre="Diego Perez",
                acudiente_telefono="3100000002",
                acudiente_email="diego.perez@familia.local",
            ),
            Estudiante(
                ruta_id=ruta.id,
                nombres="Luciana",
                apellidos="Moreno",
                documento="10010003",
                institucion="Colegio Central de Yopal",
                grado="3A",
                direccion="Transversal 18 # 30-08",
                barrio="Provivienda",
                ciudad="Yopal",
                referencia_direccion="Porton blanco",
                latitud=5.3442,
                longitud=-72.3896,
                orden_parada=3,
                acudiente_nombre="Paola Moreno",
                acudiente_telefono="3100000003",
                acudiente_email="paola.moreno@familia.local",
            ),
        ]
        db.session.add_all(estudiantes)
        db.session.flush()

        db.session.add_all(
            [
                Asistencia(
                    ruta_id=ruta.id,
                    estudiante_id=estudiantes[0].id,
                    conductor_id=conductor.id,
                    estado=EstadoAsistencia.SUBIO,
                    fecha_operacion=date.today(),
                    hora_subida=datetime.utcnow() - timedelta(minutes=25),
                ),
                Asistencia(
                    ruta_id=ruta.id,
                    estudiante_id=estudiantes[1].id,
                    conductor_id=conductor.id,
                    estado=EstadoAsistencia.PENDIENTE,
                    fecha_operacion=date.today(),
                ),
                Asistencia(
                    ruta_id=ruta.id,
                    estudiante_id=estudiantes[2].id,
                    conductor_id=conductor.id,
                    estado=EstadoAsistencia.NO_ASISTIO,
                    fecha_operacion=date.today(),
                    observacion="Reportado por el acudiente",
                ),
            ]
        )

        db.session.add_all(
            [
                MensajeChat(
                    ruta_id=ruta.id,
                    conductor_id=conductor.id,
                    remitente_tipo=TipoRemitenteChat.SISTEMA,
                    remitente_nombre="Sistema",
                    contenido="La ruta demo esta lista para iniciar seguimiento en tiempo real.",
                ),
                MensajeChat(
                    ruta_id=ruta.id,
                    conductor_id=conductor.id,
                    remitente_tipo=TipoRemitenteChat.CONDUCTOR,
                    remitente_nombre=conductor.nombre_completo,
                    contenido="Buenos dias. En unos minutos comenzamos el recorrido.",
                ),
            ]
        )

        db.session.add(
            SolicitudConductor(
                nombres="Andrea",
                apellidos="Benitez",
                telefono="3105550199",
                email="andrea.benitez@conductora.local",
                nombre_vehiculo="Microbus Kia",
                placa_vehiculo="YOP654",
                capacidad_vehiculo=14,
                ciudad="Yopal",
                comentario="Interesada en usar la plataforma para una nueva ruta escolar.",
                estado=EstadoSolicitud.PENDIENTE,
            )
        )

        db.session.commit()
        print("Datos demo cargados correctamente.")
        print("Accesos sugeridos:")
        print("- Admin: http://127.0.0.1:5000/admin/ingresar")
        print("- Usuario admin inicial: julio")
        print("- Clave admin inicial: julio21200521")
        print("- Conductor: http://127.0.0.1:5000/conductor/ingresar")
        print("- Correo demo: carlos.mendoza@rutaexacta.local")
        print("- Clave demo (solo desarrollo): RutaDemo123!")
        print("- Padres: http://127.0.0.1:5000/tracking/ruta/{}".format(ruta.pin_acceso))


if __name__ == "__main__":
    seed_demo_data()
