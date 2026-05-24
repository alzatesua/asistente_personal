from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse
from datetime import timedelta
from unittest.mock import patch

from .models import Cita, Conversacion, Mensaje, PerfilAsistente
from .services import CitaService, GLMService
from .views import normalizar_session_id, obtener_perfil_usuario


class CitaServiceDisponibilidadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='agenda', password='clave')
        self.perfil = PerfilAsistente.objects.create(
            usuario=self.user,
            nombre_usuario='Agenda',
            nombre_asistente='Asistente Agenda',
        )
        self.conversacion = Conversacion.objects.create(
            perfil=self.perfil,
            numero_whatsapp='ventas:573001234567',
            nombre_contacto='Cliente',
        )
        self.service = CitaService()

    def test_no_crea_dos_citas_a_la_misma_hora(self):
        fecha_hora = timezone.now() + timedelta(days=2)
        fecha_hora = fecha_hora.replace(hour=10, minute=0, second=0, microsecond=0)

        with patch.object(Cita, 'crear_recordatorio', return_value=None):
            cita, resultado = self.service.crear_cita(self.conversacion, {
                'titulo': 'Primera cita',
                'fecha_hora': fecha_hora,
                'duracion_minutos': 60,
                'descripcion': 'Primera cita',
            })
            cita_duplicada, resultado_duplicado = self.service.crear_cita(self.conversacion, {
                'titulo': 'Segunda cita',
                'fecha_hora': fecha_hora,
                'duracion_minutos': 60,
                'descripcion': 'Segunda cita',
            })

        self.assertTrue(resultado['exito'])
        self.assertIsNotNone(cita)
        self.assertFalse(resultado_duplicado['exito'])
        self.assertIsNone(cita_duplicada)
        self.assertTrue(resultado_duplicado['conflicto']['tiene_conflicto'])
        self.assertEqual(Cita.objects.filter(perfil=self.perfil).count(), 1)

    def test_no_crea_cita_solapada_con_otra(self):
        fecha_hora = timezone.now() + timedelta(days=3)
        fecha_hora = fecha_hora.replace(hour=14, minute=0, second=0, microsecond=0)

        with patch.object(Cita, 'crear_recordatorio', return_value=None):
            self.service.crear_cita(self.conversacion, {
                'titulo': 'Cita larga',
                'fecha_hora': fecha_hora,
                'duracion_minutos': 60,
                'descripcion': 'Cita larga',
            })
            cita_solapada, resultado_solapado = self.service.crear_cita(self.conversacion, {
                'titulo': 'Cita solapada',
                'fecha_hora': fecha_hora + timedelta(minutes=30),
                'duracion_minutos': 60,
                'descripcion': 'Cita solapada',
            })

        self.assertFalse(resultado_solapado['exito'])
        self.assertIsNone(cita_solapada)
        self.assertTrue(resultado_solapado['conflicto']['tiene_conflicto'])
        self.assertEqual(Cita.objects.filter(perfil=self.perfil).count(), 1)

    def test_no_crea_dos_citas_a_la_misma_hora_local(self):
        fecha_hora = timezone.make_aware(
            timezone.datetime(2026, 5, 24, 17, 0, 0),
            timezone.get_current_timezone(),
        )

        with patch.object(Cita, 'crear_recordatorio', return_value=None):
            cita, resultado = self.service.crear_cita(self.conversacion, {
                'titulo': 'Primera cita',
                'fecha_hora': fecha_hora,
                'duracion_minutos': 60,
                'descripcion': 'Primera cita',
            })
            cita_duplicada, resultado_duplicado = self.service.crear_cita(self.conversacion, {
                'titulo': 'Segunda cita',
                'fecha_hora': fecha_hora.replace(second=30, microsecond=999),
                'duracion_minutos': 60,
                'descripcion': 'Segunda cita',
            })

        self.assertTrue(resultado['exito'])
        self.assertIsNotNone(cita)
        self.assertFalse(resultado_duplicado['exito'])
        self.assertIsNone(cita_duplicada)
        self.assertEqual(Cita.objects.filter(perfil=self.perfil).count(), 1)

    def test_hora_ambigua_temprana_no_se_agenda_como_madrugada(self):
        datos = self.service._extraccion_fallback('mañana a las 5', timezone.datetime(2026, 5, 24, 10, 0))

        self.assertFalse(datos['completo'])
        self.assertIsNone(datos['fecha_hora'])
        self.assertEqual(datos['motivo_incompleto'], 'Hora ambigua: falta aclarar si es de la mañana o de la tarde.')

    def test_hora_ambigua_con_tarde_se_convierte_a_24_horas(self):
        datos = self.service._extraccion_fallback('mañana a las 5 de la tarde', timezone.datetime(2026, 5, 24, 10, 0))

        self.assertTrue(datos['completo'])
        self.assertEqual(datos['hora'], '17:00')


class CitaServiceIntencionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='intencion', password='clave')
        self.perfil = PerfilAsistente.objects.create(
            usuario=self.user,
            nombre_usuario='Dagi',
            nombre_asistente='Asistente',
        )
        self.conversacion = Conversacion.objects.create(
            perfil=self.perfil,
            numero_whatsapp='ventas:573001234567',
            nombre_contacto='Cliente',
        )
        self.service = CitaService()

    def test_mensajes_normales_no_activan_agendamiento(self):
        mensajes = [
            'perfecto gracias',
            '14:30',
            'mañana te escribo',
            'que servicios ofrecen?',
            'hola, cuanto cuesta?',
        ]

        for mensaje in mensajes:
            with self.subTest(mensaje=mensaje):
                self.assertFalse(self.service.detectar_intencion_agendamiento(mensaje, self.conversacion))

    def test_fragmento_si_solo_activa_si_el_bot_espera_dato_de_cita(self):
        self.assertFalse(self.service.detectar_intencion_agendamiento('sí', self.conversacion))

        Mensaje.objects.create(
            conversacion=self.conversacion,
            origen='saliente',
            contenido='Para agendar necesito que me indiques el día y la hora exactos.',
        )

        self.assertTrue(self.service.detectar_intencion_agendamiento('sí', self.conversacion))

    def test_solicitud_explicita_de_cita_si_activa_agendamiento(self):
        self.assertTrue(
            self.service.detectar_intencion_agendamiento(
                'quiero agendar una cita para mañana',
                self.conversacion,
            )
        )


class GLMServiceRoutingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='modelos', password='clave')
        self.perfil = PerfilAsistente.objects.create(
            usuario=self.user,
            nombre_usuario='Dagi',
            nombre_asistente='Asistente',
            usar_groq_respuestas_normales=True,
            usar_groq_lexico_complejo=True,
        )

    def test_audio_usa_groq_primero_y_escala_a_zai_si_respuesta_debil(self):
        service = GLMService()
        mensaje = 'Explica la arquitectura de Docker para este caso.'

        with patch.object(service, '_chat_groq', return_value='No estoy seguro.') as groq_mock, \
                patch.object(service, '_chat_zai', return_value='Respuesta segura desde Z.AI.') as zai_mock:
            respuesta = service.chat(
                mensaje,
                self.perfil,
                canal='whatsapp',
                contacto={'forzar_groq_primero': True},
            )

        self.assertEqual(respuesta, 'Respuesta segura desde Z.AI.')
        groq_mock.assert_called_once()
        zai_mock.assert_called_once()

    def test_texto_complejo_sigue_yendo_directo_a_zai(self):
        service = GLMService()
        mensaje = 'Explica la arquitectura de Docker para este caso.'

        with patch.object(service, '_chat_groq', return_value='Groq') as groq_mock, \
                patch.object(service, '_chat_zai', return_value='Respuesta desde Z.AI.') as zai_mock:
            respuesta = service.chat(mensaje, self.perfil, canal='whatsapp')

        self.assertEqual(respuesta, 'Respuesta desde Z.AI.')
        groq_mock.assert_not_called()
        zai_mock.assert_called_once()


class GLMServicePromptTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='prompt', password='clave')
        self.perfil = PerfilAsistente.objects.create(
            usuario=self.user,
            nombre_usuario='Dagi',
            nombre_asistente='Asistente',
            cv_texto='Servicios: desarrollo de software, automatizacion y asistentes para WhatsApp.',
        )

    def test_prompt_whatsapp_guia_respuesta_consultiva(self):
        prompt = GLMService().construir_prompt_sistema(
            self.perfil,
            canal='whatsapp',
            contacto={'nombre': 'Cliente', 'numero': '573001234567'},
            consulta='Necesito mejorar mi negocio',
        )

        self.assertIn('responde de forma consultiva', prompt)
        self.assertIn('pregunta por su nicho', prompt)
        self.assertIn('NO menciones productos por nombre todavia', prompt)
        self.assertIn('primero valida la idea del cliente', prompt)
        self.assertIn('que quiere crear, para quien seria y que problema quiere resolver', prompt)
        self.assertIn('Solo despues de entender esa idea puedes sugerir 2 o 3 caminos concretos', prompt)
        self.assertIn('No menciones un producto especifico por nombre si el cliente no lo conoce', prompt)
        self.assertIn('Si el cliente solo menciona su tipo de negocio o nicho', prompt)
        self.assertIn('Antes de recomendar un producto por nombre necesitas al menos dos señales concretas', prompt)
        self.assertIn('Ejemplo correcto si dice "Es para un almacen"', prompt)
        self.assertIn('Ejemplo incorrecto: "Necesitas NOVA..."', prompt)
        self.assertIn('Prioriza indagar antes que explicar', prompt)
        self.assertIn('ofrece opciones de contacto/agendamiento', prompt)
        self.assertIn('llamada telefonica, seguir por WhatsApp o reunion por Meet', prompt)
        self.assertIn('Maneja cierres formales, breves y variados', prompt)
        self.assertIn('Fecha y hora local de referencia', prompt)
        self.assertIn('No presiones la venta', prompt)
        self.assertIn('Haz maximo una pregunta clara', prompt)


class LoginSeguroTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_dashboard_requiere_login(self):
        response = self.client.get('/')

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/login/'))

    def test_api_sin_login_responde_401_json(self):
        response = self.client.get('/api/chat/historial/')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error'], 'Autenticacion requerida')

    def test_api_ventana_local_autentica_usuario_unico(self):
        user = User.objects.create_user(username='dagi', password='clave-segura')
        PerfilAsistente.objects.create(usuario=user, nombre_usuario='Dagi')

        response = self.client.get(
            '/api/chat/historial/',
            HTTP_X_ASISTENTE_DESKTOP='ventana-flotante',
            REMOTE_ADDR='127.0.0.1',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['session_memory'], True)

    def test_api_ventana_no_local_sigue_bloqueada(self):
        User.objects.create_user(username='dagi', password='clave-segura')

        response = self.client.get(
            '/api/chat/historial/',
            HTTP_X_ASISTENTE_DESKTOP='ventana-flotante',
            REMOTE_ADDR='192.168.1.10',
        )

        self.assertEqual(response.status_code, 401)

    def test_login_usuario_activo(self):
        User.objects.create_user(username='dagi', password='clave-segura')

        response = self.client.post(reverse('login'), {
            'username': 'dagi',
            'password': 'clave-segura',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_login_usuario_inactivo_no_entra(self):
        User.objects.create_user(username='dagi', password='clave-segura', is_active=False)

        response = self.client.post(reverse('login'), {
            'username': 'dagi',
            'password': 'clave-segura',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Cuenta suspendida por pago pendiente')

    def test_login_bloquea_despues_de_cinco_intentos_fallidos(self):
        User.objects.create_user(username='dagi', password='clave-segura')

        with patch('asistente.views.time.time', return_value=1000):
            for _ in range(4):
                response = self.client.post(reverse('login'), {
                    'username': 'dagi',
                    'password': 'incorrecta',
                })
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Usuario o contrasena incorrectos')

            response = self.client.post(reverse('login'), {
                'username': 'dagi',
                'password': 'incorrecta',
            })
            self.assertContains(response, 'Espera 30 segundos')

            response = self.client.post(reverse('login'), {
                'username': 'dagi',
                'password': 'clave-segura',
            })
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Demasiados intentos fallidos')

        with patch('asistente.views.time.time', return_value=1031):
            response = self.client.post(reverse('login'), {
                'username': 'dagi',
                'password': 'clave-segura',
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_login_aumenta_el_bloqueo_progresivamente(self):
        User.objects.create_user(username='dagi', password='clave-segura')

        with patch('asistente.views.time.time', return_value=2000):
            for _ in range(5):
                response = self.client.post(reverse('login'), {
                    'username': 'dagi',
                    'password': 'incorrecta',
                })
            self.assertContains(response, 'Espera 30 segundos')

        with patch('asistente.views.time.time', return_value=2031):
            for _ in range(5):
                response = self.client.post(reverse('login'), {
                    'username': 'dagi',
                    'password': 'incorrecta',
                })

        self.assertContains(response, 'Espera 60 segundos')

    def test_pantalla_usuarios_requiere_superusuario(self):
        user = User.objects.create_user(username='empresa', password='clave', is_staff=True)
        self.client.force_login(user)

        response = self.client.get(reverse('usuarios_page'))

        self.assertEqual(response.status_code, 403)


class AislamientoUsuariosTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user(username='empresa_a', password='clave-a')
        self.user_b = User.objects.create_user(username='empresa_b', password='clave-b')
        self.perfil_a = PerfilAsistente.objects.create(
            usuario=self.user_a,
            nombre_usuario='Empresa A',
            nombre_asistente='Asistente A',
        )
        self.perfil_b = PerfilAsistente.objects.create(
            usuario=self.user_b,
            nombre_usuario='Empresa B',
            nombre_asistente='Asistente B',
        )
        self.conversacion_a = Conversacion.objects.create(
            perfil=self.perfil_a,
            numero_whatsapp='ventas:573001111111',
            nombre_contacto='Cliente A',
        )
        self.conversacion_b = Conversacion.objects.create(
            perfil=self.perfil_b,
            numero_whatsapp='ventas:573002222222',
            nombre_contacto='Cliente B',
        )
        Mensaje.objects.create(
            conversacion=self.conversacion_a,
            origen='entrante',
            contenido='mensaje privado A',
        )
        Mensaje.objects.create(
            conversacion=self.conversacion_b,
            origen='entrante',
            contenido='mensaje privado B',
        )

    def test_usuario_solo_ve_sus_mensajes_recientes(self):
        self.client.force_login(self.user_a)

        response = self.client.get(reverse('mensajes_recientes'))

        self.assertEqual(response.status_code, 200)
        contenidos = [item['contenido'] for item in response.json()['mensajes']]
        self.assertIn('mensaje privado A', contenidos)
        self.assertNotIn('mensaje privado B', contenidos)

    def test_usuario_no_puede_abrir_conversacion_de_otro_perfil(self):
        self.client.force_login(self.user_a)

        response = self.client.get(
            reverse('whatsapp_conversacion_detalle', args=[self.conversacion_b.id])
        )

        self.assertEqual(response.status_code, 404)

    def test_usuario_sin_perfil_no_adopta_perfil_huerfano(self):
        huerfano = PerfilAsistente.objects.create(
            usuario=None,
            nombre_usuario='Datos antiguos',
            nombre_asistente='Legacy',
        )
        usuario_nuevo = User.objects.create_user(username='empresa_c', password='clave-c')
        request = type('Request', (), {'user': usuario_nuevo})()

        perfil = obtener_perfil_usuario(request)

        self.assertNotEqual(perfil.id, huerfano.id)
        self.assertEqual(perfil.usuario, usuario_nuevo)
        huerfano.refresh_from_db()
        self.assertIsNone(huerfano.usuario)

    def test_mismo_session_id_no_mezcla_historial_chat(self):
        session_id = 'misma-sesion-browser'
        numero = f"web:{normalizar_session_id(session_id)}"
        conversacion = Conversacion.objects.create(
            perfil=self.perfil_a,
            numero_whatsapp=numero,
            nombre_contacto='Dashboard A',
        )
        Mensaje.objects.create(
            conversacion=conversacion,
            origen='entrante',
            contenido='hola A',
        )

        self.client.force_login(self.user_b)
        response = self.client.get(
            reverse('chat_historial'),
            {'session_id': session_id, 'canal': 'web'},
        )

        contenidos = [item['contenido'] for item in response.json()['mensajes']]
        self.assertNotIn('hola A', contenidos)
