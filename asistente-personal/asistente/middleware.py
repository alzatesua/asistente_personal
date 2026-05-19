import logging
from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

logger = logging.getLogger(__name__)


class WildcardAllowedHostMiddleware:
    """Middleware para soportar wildcards en ALLOWED_HOSTS (*.dominio.com)
    Se debe ejecutar ANTES de CsrfViewMiddleware para inyectar el host en CSRF_TRUSTED_ORIGINS
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.wildcards = getattr(settings, 'ALLOWED_HOST_WILDCARDS', [])

    def __call__(self, request):
        # Obtener el host desde META, removiendo el puerto si existe
        host = request.META.get('HTTP_HOST', '')
        if ':' in host:
            host = host.split(':')[0]

        # Determinar el esquema (http o https) - revisar múltiples headers para proxy
        scheme = 'https'
        if request.META.get('HTTPS', '') != 'on' and request.META.get('HTTP_X_FORWARDED_PROTO', '') != 'https':
            scheme = 'http'

        logger.info(f"WildcardAllowedHost: host={host}, scheme={scheme}, wildcards={self.wildcards}")

        # Si el host coincide con un wildcard, inyectarlo en ALLOWED_HOSTS y CSRF_TRUSTED_ORIGINS
        if host and self.wildcards:
            for wildcard in self.wildcards:
                if host == wildcard or host.endswith('.' + wildcard):
                    if host not in settings.ALLOWED_HOSTS:
                        settings.ALLOWED_HOSTS.append(host)
                    # Agregar a CSRF_TRUSTED_ORIGINS (ambos esquemas para estar seguros)
                    for s in ['https', 'http']:
                        origin = f'{s}://{host}'
                        if origin not in settings.CSRF_TRUSTED_ORIGINS:
                            settings.CSRF_TRUSTED_ORIGINS.append(origin)
                            logger.info(f"WildcardAllowedHost: agregado {origin} a CSRF_TRUSTED_ORIGINS")
                    logger.info(f"WildcardAllowedHost: CSRF_TRUSTED_ORIGINS={settings.CSRF_TRUSTED_ORIGINS}")
                    break

        return self.get_response(request)


class SingleUserAuthRequiredMiddleware:
    """Require one active Django user session for the private assistant UI/API."""

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
            settings.STATIC_URL,
            settings.MEDIA_URL,
        )

        if path.startswith(public_paths):
            return self.get_response(request)

        allowed_user_id = User.objects.filter(is_active=True).order_by('id').values_list('id', flat=True).first()
        if request.user.is_authenticated and (
            not request.user.is_active or request.user.id != allowed_user_id
        ):
            logout(request)
            return redirect(f'{login_url}?inactive=1')

        if request.user.is_authenticated:
            return self.get_response(request)

        if path.startswith('/api/'):
            return JsonResponse({'error': 'Autenticacion requerida'}, status=401)

        return redirect(f'{login_url}?next={request.get_full_path()}')
