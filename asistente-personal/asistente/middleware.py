from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.models import User
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse


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
