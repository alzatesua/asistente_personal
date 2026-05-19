from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class LoginSeguroTests(TestCase):
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
