from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch

from .models import Conversacion, Mensaje, PerfilAsistente
from .views import normalizar_session_id, obtener_perfil_usuario


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
