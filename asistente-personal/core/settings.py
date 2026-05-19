from pathlib import Path
from dotenv import load_dotenv
import os

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env', override=True)

SECRET_KEY = os.getenv('SECRET_KEY')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1,0.0.0.0').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',
    'asistente',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'asistente.middleware.AuthRequiredMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

LANGUAGE_CODE = 'es-co'
TIME_ZONE = 'America/Bogota'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False') == 'True'

CORS_ALLOW_ALL_ORIGINS = True
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('DATA_UPLOAD_MAX_MEMORY_SIZE', 25 * 1024 * 1024))

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ]
}

# Z.AI / GLM
ZAI_API_KEY = os.getenv('ZAI_API_KEY')
ZAI_BASE_URL = os.getenv('ZAI_BASE_URL')
ZAI_MODEL = os.getenv('ZAI_MODEL', 'glm-4')

# Baileys
BAILEYS_SERVICE_URL = os.getenv('BAILEYS_SERVICE_URL', 'http://localhost:3002')
BAILEYS_WEBHOOK_SECRET = os.getenv('BAILEYS_WEBHOOK_SECRET')
DJANGO_WEBHOOK_URL = os.getenv('DJANGO_WEBHOOK_URL', 'http://localhost:8005/webhook/whatsapp/')
WHATSAPP_BULK_MAX_RECIPIENTS = int(os.getenv('WHATSAPP_BULK_MAX_RECIPIENTS', 50))
WHATSAPP_BULK_DELAY_MIN = float(os.getenv('WHATSAPP_BULK_DELAY_MIN', 1.0))
WHATSAPP_BULK_DELAY_MAX = float(os.getenv('WHATSAPP_BULK_DELAY_MAX', 1.6))
WHATSAPP_DEFAULT_COUNTRY_CODE = os.getenv('WHATSAPP_DEFAULT_COUNTRY_CODE', '57')

# TTS
TTS_LANG = os.getenv('TTS_LANG', 'es')
TTS_TLD = os.getenv('TTS_TLD', 'com')
TTS_FALLBACK_VOICE = os.getenv('TTS_FALLBACK_VOICE', 'piper:es_ES-mls_10246-low')

# Deepgram Aura (TTS premium)
DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')

# Redis
CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
