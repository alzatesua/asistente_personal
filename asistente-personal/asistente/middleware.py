from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth import get_user_model
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from .models import PerfilAsistente


DESKTOP_HEADER_VALUE = 'ventana-flotante'

SECTION_PATHS = (
    ('chat', ('/chat/', '/api/chat/', '/api/mensajes/')),
    ('tareas', ('/tareas/', '/api/tareas/', '/api/alarmas/', '/api/acciones/', '/api/scheduler/')),
    ('citas', ('/citas/', '/api/citas/', '/api/test/cita-detectar/')),
    ('whatsapp', ('/whatsapp/', '/api/whatsapp/')),
    ('facebook', ('/facebook/', '/api/facebook/')),
    ('voz', ('/voz/', '/api/voces/', '/api/voz/')),
    ('configurar', ('/configurar/',)),
    ('usuarios', ('/usuarios/',)),
)


def _es_loopback(request):
    return request.META.get('REMOTE_ADDR') in {'127.0.0.1', '::1'}


def _usuario_para_ventana_local():
    username = getattr(settings, 'DESKTOP_API_USERNAME', '')
    usuarios = get_user_model().objects.filter(is_active=True)
    if username:
        return usuarios.filter(username=username).first()

    perfiles = PerfilAsistente.objects.select_related('usuario').filter(
        usuario__isnull=False,
        usuario__is_active=True,
    )
    if perfiles.count() == 1:
        return perfiles.first().usuario

    if usuarios.count() == 1:
        return usuarios.first()

    return None


def autenticar_ventana_local(request):
    """Permite a la ventana Pygame usar la API local sin cookie de navegador."""
    if request.user.is_authenticated:
        return True

    if not request.path.startswith('/api/'):
        return False

    if request.META.get('HTTP_X_ASISTENTE_DESKTOP') != DESKTOP_HEADER_VALUE:
        return False

    if not _es_loopback(request):
        return False

    usuario = _usuario_para_ventana_local()
    if not usuario:
        return False

    request.user = usuario
    return True


def seccion_para_path(path):
    for seccion, prefijos in SECTION_PATHS:
        if path.startswith(prefijos):
            return seccion
    return ''


def usuario_puede_ver_seccion(user, seccion):
    if not seccion:
        return True

    perfil = getattr(user, 'perfil_asistente', None)
    if not perfil:
        return seccion == 'configurar'

    return perfil.puede_ver_seccion(seccion)


class AuthRequiredMiddleware:
    """Require an active Django user session for the private assistant UI/API."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        login_url = reverse('login')
        logout_url = reverse('logout')
        public_paths = (
            login_url,
            logout_url,
            '/webhook/whatsapp/',
            '/webhook/facebook/',
            '/privacidad/',
            settings.STATIC_URL,
            settings.MEDIA_URL,
        )

        if path.startswith(public_paths):
            return self.get_response(request)

        autenticar_ventana_local(request)

        if request.user.is_authenticated and not request.user.is_active:
            logout(request)
            return redirect(f'{login_url}?inactive=1')

        if request.user.is_authenticated:
            seccion = seccion_para_path(path)
            if not usuario_puede_ver_seccion(request.user, seccion):
                if path.startswith('/api/'):
                    return JsonResponse({'error': 'No tienes permiso para ver esta seccion'}, status=403)
                return HttpResponseForbidden('No tienes permiso para ver esta seccion.')
            return self.get_response(request)

        if path.startswith('/api/'):
            return JsonResponse({'error': 'Autenticacion requerida'}, status=401)

        return redirect(f'{login_url}?next={request.get_full_path()}')
