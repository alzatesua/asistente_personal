import json
import os
import hashlib
import hmac
import subprocess
import time
import threading
import shutil
import base64
import requests
import random
import re
import numpy as np
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.contrib import messages
from django.utils import timezone
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from .models import (
    PerfilAsistente,
    Conversacion,
    Mensaje,
    ComandoEjecutado,
    TareaProgramada,
    SECCIONES_DISPONIBLES,
    SECCIONES_PERMITIDAS_DEFAULT,
)
from .services import GLMService, TTSService, PCActionService, BackgroundTaskManager, WebResearchService, SchedulerService
from audio_visual_state import notify_audio_start, notify_audio_stop
import PyPDF2
import io


BAILEYS_START_TIMEOUT_SECONDS = 8
WHATSAPP_NOTIFICACION_MAX_CHARS = 260
LOGIN_MAX_INTENTOS = 5
LOGIN_BLOQUEO_INICIAL_SEGUNDOS = 30
LOGIN_BLOQUEO_MAX_SEGUNDOS = 30 * 60
WHATSAPP_LINE_DEFAULTS = {
    'responder_chats': True,
    'responder_grupos': True,
    'leer_chats': True,
    'leer_grupos': True,
    'responder_voz': False,
}
FACEBOOK_CONFIG_DEFAULTS = {
    'auto_mensajes': True,
    'auto_comentarios': False,
    'leer_mensajes': False,
    'leer_comentarios': False,
    'responder_comentarios_publicamente': True,
}


def obtener_ip_cliente(request):
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def login_intentos_key(username, ip_cliente):
    identidad = f'{(username or "").strip().lower()}|{ip_cliente or ""}'
    digest = hashlib.sha256(identidad.encode('utf-8')).hexdigest()
    return f'login_intentos:{digest}'


def login_estado_bloqueo(username, ip_cliente):
    estado = cache.get(login_intentos_key(username, ip_cliente)) or {}
    bloqueo_hasta = float(estado.get('bloqueo_hasta') or 0)
    restante = int(max(0, bloqueo_hasta - time.time()))
    if restante <= 0:
        return estado, 0
    return estado, restante


def registrar_login_fallido(username, ip_cliente):
    key = login_intentos_key(username, ip_cliente)
    estado = cache.get(key) or {}
    intentos = int(estado.get('intentos') or 0) + 1
    bloqueos = int(estado.get('bloqueos') or 0)

    if intentos >= LOGIN_MAX_INTENTOS:
        espera = min(
            LOGIN_BLOQUEO_INICIAL_SEGUNDOS * (2 ** bloqueos),
            LOGIN_BLOQUEO_MAX_SEGUNDOS,
        )
        estado = {
            'intentos': 0,
            'bloqueos': bloqueos + 1,
            'bloqueo_hasta': time.time() + espera,
        }
        cache.set(key, estado, timeout=LOGIN_BLOQUEO_MAX_SEGUNDOS * 2)
        return espera

    estado.update({'intentos': intentos, 'bloqueo_hasta': 0, 'bloqueos': bloqueos})
    cache.set(key, estado, timeout=LOGIN_BLOQUEO_MAX_SEGUNDOS * 2)
    return 0


def limpiar_login_fallidos(username, ip_cliente):
    cache.delete(login_intentos_key(username, ip_cliente))


def obtener_perfil_usuario(request, crear=True):
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return None

    try:
        return request.user.perfil_asistente
    except PerfilAsistente.DoesNotExist:
        if not crear:
            return None

        return PerfilAsistente.objects.create(
            usuario=request.user,
            nombre_usuario=request.user.get_full_name() or request.user.username,
            nombre_asistente='Asistente',
        )


def prefijo_whatsapp_perfil(perfil):
    return f"u{perfil.id}-" if perfil and perfil.id else ""


def linea_whatsapp_publica(linea):
    linea = normalizar_linea_whatsapp(linea)
    match = re.match(r'^u\d+-(.+)$', linea)
    return normalizar_linea_whatsapp(match.group(1)) if match else linea


def linea_whatsapp_interna(perfil, linea):
    linea = linea_whatsapp_publica(linea)
    return f"{prefijo_whatsapp_perfil(perfil)}{linea}" if perfil else linea


def perfil_desde_linea_whatsapp(linea):
    linea = normalizar_linea_whatsapp(linea)
    match = re.match(r'^u(\d+)-', linea)
    if match:
        return PerfilAsistente.objects.filter(id=match.group(1)).first(), linea_whatsapp_publica(linea)
    # Buscar cualquier perfil disponible (con o sin usuario)
    if PerfilAsistente.objects.count() == 1:
        return PerfilAsistente.objects.first(), linea
    # Prioridad: perfiles con usuario
    perfil_con_usuario = PerfilAsistente.objects.filter(usuario__isnull=False).first()
    if perfil_con_usuario:
        return perfil_con_usuario, linea
    # Si no hay perfiles con usuario, usar el primero disponible
    perfil = PerfilAsistente.objects.first()
    return perfil, linea


def obtener_session_key(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def normalizar_session_id(session_id):
    session_id = (session_id or '').strip()
    if not session_id:
        return ''
    return hashlib.sha1(session_id.encode('utf-8')).hexdigest()[:24]


def obtener_conversacion_sesion(request, perfil, session_id=None, canal='web'):
    session_id = normalizar_session_id(session_id) or normalizar_session_id(obtener_session_key(request))
    numero = f"{canal}:{session_id}"[:30]
    nombre = 'Dashboard' if canal == 'web' else canal.title()
    conversacion, creada = Conversacion.objects.get_or_create(
        perfil=perfil,
        numero_whatsapp=numero,
        defaults={'nombre_contacto': f"{nombre} {session_id[-6:]}"},
    )
    if not creada and not conversacion.nombre_contacto:
        conversacion.nombre_contacto = f"{nombre} {session_id[-6:]}"
        conversacion.save(update_fields=['nombre_contacto'])
    return conversacion


def baileys_service_url():
    return getattr(settings, 'BAILEYS_SERVICE_URL', None) or 'http://localhost:3002'


def baileys_esta_activo():
    try:
        requests.get(f"{baileys_service_url()}/status", timeout=1)
        return True
    except requests.RequestException:
        return False


def iniciar_baileys_si_hace_falta():
    if baileys_esta_activo():
        return True, 'Baileys ya estaba iniciado'

    baileys_dir = settings.BASE_DIR / 'baileys-service'
    if not baileys_dir.exists():
        return False, f'No existe la carpeta {baileys_dir}'
    if not (baileys_dir / 'package.json').exists():
        return False, f'No existe package.json en {baileys_dir}'

    npm_bin = shutil.which('npm')
    if not npm_bin:
        return False, 'No encontré npm en el PATH del proceso Django'

    log_path = baileys_dir / 'baileys-autostart.log'
    log_file = open(log_path, 'ab')
    baileys_url = urlparse(baileys_service_url())
    baileys_port = str(baileys_url.port or 3002)
    baileys_host = baileys_url.hostname or '127.0.0.1'
    proceso_env = os.environ.copy()
    proceso_env.update({
        'PORT': baileys_port,
        'HOST': '127.0.0.1' if baileys_host in ('localhost', '127.0.0.1') else baileys_host,
        'DJANGO_WEBHOOK_URL': getattr(settings, 'DJANGO_WEBHOOK_URL', 'http://localhost:8005/webhook/whatsapp/'),
        'WEBHOOK_SECRET': settings.BAILEYS_WEBHOOK_SECRET or '',
    })
    proceso = subprocess.Popen(
        [npm_bin, 'start'],
        cwd=baileys_dir,
        stdout=log_file,
        stderr=log_file,
        env=proceso_env,
        start_new_session=True,
    )

    limite = time.time() + BAILEYS_START_TIMEOUT_SECONDS
    while time.time() < limite:
        if proceso.poll() is not None:
            try:
                ultimas_lineas = log_path.read_text(errors='ignore').splitlines()[-8:]
                detalle = ' | '.join(ultimas_lineas)
            except OSError:
                detalle = ''
            return False, f'Baileys se cerró al iniciar npm start. {detalle}'.strip()

        if baileys_esta_activo():
            return True, f'Baileys iniciado pid={proceso.pid}'
        time.sleep(0.5)

    return False, f'Baileys no respondió después de iniciar npm start. Revisa {log_path}'


def respuesta_json_o_texto(response):
    try:
        return response.json()
    except ValueError:
        texto = (response.text or '').strip()
        if len(texto) > 500:
            texto = texto[:500] + '...'
        return {'raw_response': texto}


def pedir_estado_baileys(linea, timeout=3):
    response = requests.get(
        f"{baileys_service_url()}/status/{linea}",
        timeout=timeout,
    )
    return respuesta_json_o_texto(response) if response.content else {}


def pedir_conexion_baileys(linea, timeout=5):
    response = requests.post(
        f"{baileys_service_url()}/connect/{linea}",
        timeout=timeout,
    )
    return response, respuesta_json_o_texto(response) if response.content else {}


def borrar_sesion_baileys_remota(linea, timeout=8):
    response = requests.post(
        f"{baileys_service_url()}/delete-session/{linea}",
        timeout=timeout,
    )
    return response, respuesta_json_o_texto(response) if response.content else {}


def esperar_qr_o_conexion_baileys(linea, segundos=12):
    limite = time.time() + segundos
    ultimo_estado = {}
    while time.time() < limite:
        try:
            ultimo_estado = pedir_estado_baileys(linea, timeout=2)
        except requests.RequestException:
            time.sleep(0.75)
            continue

        if ultimo_estado.get('hasQR') or ultimo_estado.get('status') == 'connected':
            return ultimo_estado
        time.sleep(0.75)
    return ultimo_estado


def normalizar_linea_whatsapp(linea):
    import re

    texto = (linea or 'principal').strip().lower()
    texto = re.sub(r'[^a-z0-9_-]+', '-', texto)
    texto = re.sub(r'-+', '-', texto).strip('-')
    return texto or 'principal'


def whatsapp_config_path():
    return settings.BASE_DIR / 'whatsapp_line_settings.json'


def baileys_auth_folder(linea):
    return settings.BASE_DIR / 'baileys-service' / 'baileys_sessions' / normalizar_linea_whatsapp(linea)


def baileys_sessions_root():
    return settings.BASE_DIR / 'baileys-service' / 'baileys_sessions'


def linea_tiene_sesion_baileys(linea):
    return (baileys_auth_folder(linea) / 'creds.json').exists()


def facebook_config_path():
    return settings.BASE_DIR / 'facebook_page_settings.json'


def cargar_config_facebook():
    ruta = facebook_config_path()
    if not ruta.exists():
        return {}
    try:
        with open(ruta, 'r', encoding='utf-8') as archivo:
            data = json.load(archivo)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[Facebook Config] No pude leer configuracion: {exc}")
        return {}


def guardar_config_facebook(config):
    ruta = facebook_config_path()
    with open(ruta, 'w', encoding='utf-8') as archivo:
        json.dump(config, archivo, ensure_ascii=False, indent=2)


def facebook_key_perfil(perfil):
    return f"u{perfil.id}" if perfil and perfil.id else "default"


def obtener_config_facebook(perfil):
    config = cargar_config_facebook()
    datos = config.get(facebook_key_perfil(perfil), {})
    return {
        'auto_mensajes': bool(datos.get('auto_mensajes', FACEBOOK_CONFIG_DEFAULTS['auto_mensajes'])),
        'auto_comentarios': bool(datos.get('auto_comentarios', FACEBOOK_CONFIG_DEFAULTS['auto_comentarios'])),
        'leer_mensajes': bool(datos.get('leer_mensajes', FACEBOOK_CONFIG_DEFAULTS['leer_mensajes'])),
        'leer_comentarios': bool(datos.get('leer_comentarios', FACEBOOK_CONFIG_DEFAULTS['leer_comentarios'])),
        'responder_comentarios_publicamente': bool(datos.get(
            'responder_comentarios_publicamente',
            FACEBOOK_CONFIG_DEFAULTS['responder_comentarios_publicamente'],
        )),
    }


def perfil_facebook_destino():
    return None


def perfil_facebook_por_page_id(page_id):
    page_id = (page_id or '').strip()
    if page_id:
        perfil = PerfilAsistente.objects.filter(meta_page_id=page_id, usuario__isnull=False).first()
        if perfil:
            return perfil
        conv = Conversacion.objects.filter(numero_whatsapp__startswith=f'facebook:{page_id}:').select_related('perfil').first()
        if conv:
            return conv.perfil
    return None


def perfil_facebook_por_verify_token(token):
    token = (token or '').strip()
    if not token:
        return None
    return PerfilAsistente.objects.filter(meta_verify_token=token, usuario__isnull=False).first()


def perfiles_facebook_configurados():
    return PerfilAsistente.objects.filter(
        usuario__isnull=False,
        meta_page_id__gt='',
        meta_page_access_token__gt='',
    )


def meta_graph_url(perfil, path):
    version = ((getattr(perfil, 'meta_graph_api_version', '') or 'v25.0')).strip().lstrip('/')
    path = str(path or '').strip().lstrip('/')
    return f"https://graph.facebook.com/{version}/{path}"


def meta_page_token(perfil):
    return (getattr(perfil, 'meta_page_access_token', '') or '').strip()


def verificar_firma_meta(request, perfil):
    app_secret = (getattr(perfil, 'meta_app_secret', '') or '').strip()
    if not app_secret:
        return True
    firma = request.headers.get('X-Hub-Signature-256', '')
    print(f"[FIRMA DEBUG] firma recibida: {firma}", flush=True)
    print(f"[FIRMA DEBUG] app_secret guardado: {app_secret}", flush=True)
    if not firma.startswith('sha256='):
        print("[FIRMA DEBUG] firma no empieza con sha256=", flush=True)
        return False
    digest = hmac.new(app_secret.encode('utf-8'), request.body, hashlib.sha256).hexdigest()
    resultado = hmac.compare_digest(firma, f'sha256={digest}')
    print(f"[FIRMA DEBUG] resultado verificacion: {resultado}", flush=True)
    return resultado


def enviar_mensaje_facebook(perfil, psid, texto):
    token = meta_page_token(perfil)
    if not token:
        return False, {'error': 'Falta META_PAGE_ACCESS_TOKEN'}
    response = requests.post(
        meta_graph_url(perfil, 'me/messages'),
        params={'access_token': token},
        json={'recipient': {'id': psid}, 'message': {'text': texto}},
        timeout=15,
    )
    payload = respuesta_json_o_texto(response) if response.content else {}
    return response.status_code < 400, payload


def responder_comentario_facebook(perfil, comment_id, texto):
    token = meta_page_token(perfil)
    if not token:
        return False, {'error': 'Falta META_PAGE_ACCESS_TOKEN'}
    response = requests.post(
        meta_graph_url(perfil, f'{comment_id}/comments'),
        params={'access_token': token, 'message': texto},
        timeout=15,
    )
    payload = respuesta_json_o_texto(response) if response.content else {}
    return response.status_code < 400, payload


def conversacion_facebook(perfil, page_id, tipo, externo_id, nombre=''):
    page_id = (page_id or getattr(perfil, 'meta_page_id', '') or 'page').strip()
    externo_id = (externo_id or 'desconocido').strip()
    numero = f"facebook:{page_id}:{tipo}:{externo_id}"[:80]
    conversacion, _ = Conversacion.objects.get_or_create(
        perfil=perfil,
        numero_whatsapp=numero,
        defaults={'nombre_contacto': nombre or ('Comentario Facebook' if tipo == 'comment' else 'Messenger')},
    )
    if nombre and conversacion.nombre_contacto != nombre:
        conversacion.nombre_contacto = nombre
        conversacion.save(update_fields=['nombre_contacto'])
    return conversacion


def datos_destino_facebook(conversacion):
    partes = (conversacion.numero_whatsapp or '').split(':', 3)
    if len(partes) == 4 and partes[0] == 'facebook':
        return {'page_id': partes[1], 'tipo': partes[2], 'externo_id': partes[3]}
    return {'page_id': '', 'tipo': '', 'externo_id': ''}


def listar_lineas_whatsapp_guardadas(perfil=None):
    lineas = set()
    prefijo_perfil = prefijo_whatsapp_perfil(perfil)

    sessions_root = baileys_sessions_root()
    if sessions_root.exists():
        for carpeta in sessions_root.iterdir():
            if not carpeta.is_dir():
                continue
            nombre = carpeta.name
            if '.backup-' in nombre or nombre.endswith('.bak'):
                continue
            nombre = normalizar_linea_whatsapp(nombre)
            if not (carpeta / 'creds.json').exists():
                continue
            if perfil:
                if nombre.startswith(prefijo_perfil):
                    lineas.add(linea_whatsapp_publica(nombre))
            else:
                lineas.add(linea_whatsapp_publica(nombre))

    if perfil:
        prefijos_excluidos = ('web:', 'desktop:')
        numeros = Conversacion.objects.filter(perfil=perfil).values_list('numero_whatsapp', flat=True)
        for numero in numeros:
            numero = numero or ''
            if ':' not in numero or numero.startswith(prefijos_excluidos):
                continue
            linea = numero.split(':', 1)[0]
            lineas.add(linea_whatsapp_publica(linea))

    return sorted(linea for linea in lineas if linea)


def cargar_config_whatsapp():
    ruta = whatsapp_config_path()
    if not ruta.exists():
        return {}
    try:
        with open(ruta, 'r', encoding='utf-8') as archivo:
            data = json.load(archivo)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[WhatsApp Config] No pude leer configuracion: {exc}")
        return {}


def guardar_config_whatsapp(config):
    ruta = whatsapp_config_path()
    with open(ruta, 'w', encoding='utf-8') as archivo:
        json.dump(config, archivo, ensure_ascii=False, indent=2)


def eliminar_config_linea_whatsapp(linea):
    linea = normalizar_linea_whatsapp(linea)
    config = cargar_config_whatsapp()
    if linea not in config:
        return False
    del config[linea]
    guardar_config_whatsapp(config)
    return True


def obtener_config_linea(linea):
    linea = normalizar_linea_whatsapp(linea)
    config = cargar_config_whatsapp()
    datos = config.get(linea, {})
    return {
        'linea': linea,
        'responder_chats': bool(datos.get('responder_chats', WHATSAPP_LINE_DEFAULTS['responder_chats'])),
        'responder_grupos': bool(datos.get('responder_grupos', WHATSAPP_LINE_DEFAULTS['responder_grupos'])),
        'leer_chats': bool(datos.get('leer_chats', WHATSAPP_LINE_DEFAULTS['leer_chats'])),
        'leer_grupos': bool(datos.get('leer_grupos', WHATSAPP_LINE_DEFAULTS['leer_grupos'])),
        'responder_voz': bool(datos.get('responder_voz', WHATSAPP_LINE_DEFAULTS['responder_voz'])),
    }


def debe_responder_whatsapp(linea, es_grupo):
    config = obtener_config_linea(linea)
    return config['responder_grupos'] if es_grupo else config['responder_chats']


def debe_leer_whatsapp(linea, es_grupo):
    config = obtener_config_linea(linea)
    return config['leer_grupos'] if es_grupo else config['leer_chats']


def debe_responder_voz_whatsapp(linea):
    return obtener_config_linea(linea)['responder_voz']


def debe_responder_audio_por_mensaje(linea, es_audio_entrante=False):
    return debe_responder_voz_whatsapp(linea) and bool(es_audio_entrante)


def borrar_sesion_baileys_local(linea):
    carpeta = baileys_auth_folder(linea)
    if carpeta.exists():
        shutil.rmtree(carpeta)
        return True, str(carpeta)
    return False, str(carpeta)


def texto_notificacion_whatsapp(nombre_contacto, numero, contenido, tipo):
    contacto = nombre_contacto or numero or 'un contacto'
    if (tipo == 'voz' or contenido == '[Audio]') and contenido == '[Audio]':
        return f'Te llego un mensaje de voz de {contacto}.'

    texto = (contenido or '').strip()
    if len(texto) > WHATSAPP_NOTIFICACION_MAX_CHARS:
        texto = texto[:WHATSAPP_NOTIFICACION_MAX_CHARS].rsplit(' ', 1)[0] + '...'
    if tipo == 'voz':
        return f'Te llego una nota de voz de {contacto}. Dice: {texto}'
    return f'Te llego un mensaje de WhatsApp de {contacto}. Dice: {texto}'


def ruta_audio_desde_url(audio_url):
    if not audio_url:
        return None

    path = urlparse(audio_url).path
    media_url = settings.MEDIA_URL or '/media/'
    if not path.startswith(media_url):
        return None

    relativo = path[len(media_url):].lstrip('/')
    ruta = settings.MEDIA_ROOT / relativo
    return str(ruta) if ruta.exists() else None


def rutas_audio_desde_url(audio_url):
    if not audio_url:
        return []

    rutas = []
    parsed = urlparse(audio_url)
    query = parse_qs(parsed.query)
    metadata_nombre = (query.get('parts') or [None])[0]

    if metadata_nombre:
        metadata_ruta = settings.MEDIA_ROOT / 'audios' / os.path.basename(metadata_nombre)
        try:
            with open(metadata_ruta, 'r', encoding='utf-8') as archivo:
                data = json.load(archivo)
            for parte_url in data.get('parts', []):
                ruta = ruta_audio_desde_url(parte_url)
                if ruta:
                    rutas.append(ruta)
        except Exception as exc:
            print(f"[WhatsApp Voz] No pude leer metadata de partes: {exc}")

    if rutas:
        return rutas

    ruta = ruta_audio_desde_url(audio_url)
    return [ruta] if ruta else []


def reproducir_audio_local(ruta_audio):
    if not ruta_audio:
        return False

    visual_token = notify_audio_start(ruta_audio, source="django")
    extension = os.path.splitext(ruta_audio)[1].lower()
    try:
        if extension == '.mp3':
            reproductores = [
                ['mpg123', '-q', ruta_audio],
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', ruta_audio],
            ]
        else:
            reproductores = [
                ['paplay', ruta_audio],
                ['aplay', ruta_audio],
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', ruta_audio],
                ['play', '-q', ruta_audio],
            ]

        for comando in reproductores:
            if not shutil.which(comando[0]):
                continue
            try:
                resultado = subprocess.run(comando, timeout=25, check=False)
                if resultado.returncode == 0:
                    return True
            except Exception as exc:
                print(f"[WhatsApp Voz] No pude reproducir con {comando[0]}: {exc}")

        try:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()
            if extension == '.mp3':
                pygame.mixer.music.stop()
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
                pygame.mixer.music.load(ruta_audio)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.delay(100)
                pygame.mixer.music.unload()
            else:
                sound = pygame.mixer.Sound(ruta_audio)
                channel = sound.play()
                while channel and channel.get_busy():
                    pygame.time.delay(100)
            return True
        except Exception as exc:
            print(f"[WhatsApp Voz] No pude reproducir con pygame: {exc}")

        return False
    finally:
        notify_audio_stop(visual_token)


def reproducir_sonido_cita():
    """Reproduce un tono agradable cuando se agenda una cita."""
    try:
        import pygame
        import numpy as np

        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=1)

        # Tono ascendente (Do-Mi-Sol) para sonar positivo
        duracion = 0.25
        frecuencias = [523.25, 659.25, 783.99]  # Do, Mi, Sol

        for freq in frecuencias:
            n_samples = int(44100 * duracion)
            t = np.linspace(0, duracion, n_samples, False)

            # Generar onda seno con envolvente suave
            envolvente = np.concatenate([
                np.linspace(0, 1, int(n_samples * 0.1)),  # Attack
                np.ones(int(n_samples * 0.7)),           # Sustain
                np.linspace(1, 0, int(n_samples * 0.2))   # Release
            ])
            wave = np.sin(2 * np.pi * freq * t) * 0.3 * envolvente
            sound = pygame.sndarray.make_sound((wave * 32767).astype(np.int16))
            sound.play()
            pygame.time.wait(int(duracion * 1000))

        return True
    except ImportError:
        # Fallback sin numpy: usar pygame con sonido básico
        try:
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()

            # Intentar usar archivos de sonido
            sonidos_cita = [
                settings.MEDIA_ROOT / 'sonidos' / 'cita_confirmada.mp3',
                settings.MEDIA_ROOT / 'sonidos' / 'ding.mp3',
                settings.MEDIA_ROOT / 'sonidos' / 'success.mp3',
            ]

            for sonido in sonidos_cita:
                if sonido.exists():
                    return reproducir_audio_local(str(sonido))

            # Generar tono simple sin numpy
            try:
                from array import array
                from struct import pack

                pygame.mixer.init(frequency=44100, size=-16, channels=1)
                sonido_buffer = array('h')

                for freq in [523, 659, 783]:  # Do, Mi, Sol
                    for i in range(44100 // 4):  # 0.25 segundos
                        valor = int(32767 * 0.3 * np.sin(2 * np.pi * freq * i / 44100))
                        sonido_buffer.append(valor)

                sonido = pygame.mixer.Sound(buffer=sonido_buffer)
                sonido.play()
                pygame.time.wait(750)  # Esperar a que termine
                return True
            except Exception:
                pass

        except Exception as exc:
            print(f"[CITA] No pude reproducir sonido: {exc}")

    except Exception as exc:
        print(f"[CITA] Error generando tono de cita: {exc}")

    return False


def anunciar_mensaje_whatsapp(perfil_id, nombre_contacto, numero, contenido, tipo):
    def worker():
        try:
            perfil = PerfilAsistente.objects.get(id=perfil_id)
            texto = texto_notificacion_whatsapp(nombre_contacto, numero, contenido, tipo)
            audio_url = TTSService().generar_audio(
                texto,
                voz=perfil.voz_preferida,
                velocidad=perfil.voz_velocidad,
            )
            rutas_audio = rutas_audio_desde_url(audio_url)
            reproducido = False
            for ruta_audio in rutas_audio:
                reproducido = reproducir_audio_local(ruta_audio) or reproducido

            if not reproducido:
                try:
                    import pyttsx3

                    engine = pyttsx3.init()
                    engine.say(texto)
                    engine.runAndWait()
                except Exception as voz_exc:
                    print(f"[WhatsApp Voz] Audio generado, pero no se pudo reproducir localmente: {audio_url}. Fallback pyttsx3 fallo: {voz_exc}")
        except Exception as exc:
            print(f"[WhatsApp Voz] Error anunciando mensaje: {exc}")

    threading.Thread(target=worker, daemon=True).start()


def respuesta_audio_whatsapp():
    return "Dame un momentico y lo escucho bien."


def transcribir_audio_whatsapp(audio_base64, mimetype):
    if not audio_base64:
        print("[WhatsApp Voz] Mensaje de voz sin audio_base64; no hay audio para transcribir")
        return ''

    api_key = (getattr(settings, 'DEEPGRAM_API_KEY', None) or '').strip()
    if not api_key:
        print("[WhatsApp Voz] No hay DEEPGRAM_API_KEY para transcribir audio entrante")
        return ''

    try:
        audio_bytes = base64.b64decode(audio_base64)
        print(f"[WhatsApp Voz] Transcribiendo audio entrante ({len(audio_bytes)} bytes, {mimetype or 'audio/ogg'})")
        response = requests.post(
            "https://api.deepgram.com/v1/listen?model=nova-3&language=es&smart_format=true",
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": mimetype or "audio/ogg",
            },
            data=audio_bytes,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        transcript = (
            data.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("transcript", "")
        )
        if not transcript.strip():
            print("[WhatsApp Voz] Deepgram respondio sin transcripcion")
        return transcript.strip()
    except requests.HTTPError as exc:
        detalle = exc.response.text[:500] if exc.response is not None else str(exc)
        print(f"[WhatsApp Voz] Error HTTP transcribiendo audio entrante: {detalle}")
        return ''
    except Exception as exc:
        print(f"[WhatsApp Voz] Error transcribiendo audio entrante: {exc}")
        return ''


def limpiar_respuesta_whatsapp(respuesta, perfil):
    import re

    texto = (respuesta or '').strip()
    if not texto:
        return texto

    nombre_asistente = (perfil.nombre_asistente or '').strip()
    if nombre_asistente:
        texto = re.sub(
            rf'^\s*{re.escape(nombre_asistente)}\s*:\s*',
            '',
            texto,
            flags=re.IGNORECASE,
        ).strip()

        patrones_presentacion = [
            rf'^\s*soy\s+{re.escape(nombre_asistente)}[,.\s]+',
            rf'^\s*hola[,.\s]+soy\s+{re.escape(nombre_asistente)}[,.\s]+',
            rf'^\s*como\s+{re.escape(nombre_asistente)}[,.\s]+',
        ]
        for patron in patrones_presentacion:
            texto = re.sub(patron, '', texto, flags=re.IGNORECASE).strip()

    texto = re.sub(r'\bcomo (asistente|ia|bot)\b[,:\s]*', '', texto, flags=re.IGNORECASE).strip()
    texto = re.sub(r'\b(no puedo|no tengo la capacidad de) escuchar audios\b', 'lo escucho en un momento', texto, flags=re.IGNORECASE)
    return texto


def construir_historial(conversacion, limite=20):
    mensajes = list(conversacion.mensajes.order_by('-creado_en')[:limite])
    historial = []
    for m in reversed(mensajes):
        rol = 'user' if m.origen == 'entrante' else 'assistant'
        historial.append({'rol': rol, 'contenido': m.contenido})
    return historial


def guardar_respuesta_chat(conversacion, respuesta, audio_url=None):
    msg_saliente = Mensaje.objects.create(
        conversacion=conversacion,
        tipo='texto',
        origen='saliente',
        contenido=respuesta or '',
        respondido=True,
        audio_url=audio_url,
    )
    return msg_saliente


def respuesta_chat_json(conversacion, respuesta, audio_url=None, **extra):
    guardar_respuesta_chat(conversacion, respuesta, audio_url)
    data = {
        'respuesta': respuesta,
        'audio_url': audio_url,
        'session_memory': True,
        'conversacion_id': conversacion.id,
    }
    data.update(extra)
    return JsonResponse(data)


def respuesta_silenciosa_json(**extra):
    data = {
        'respuesta': None,
        'audio_url': None,
        'auto_respuesta': False,
        'sin_modelo_activo': True,
    }
    data.update(extra)
    return JsonResponse(data)


def generar_audio_chat(tts, texto, perfil, canal='web'):
    return tts.generar_audio(texto, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)


def requiere_investigacion_web(respuesta_ia, mensaje_usuario):
    """
    Detecta si la respuesta de la IA indica que no sabe la respuesta o si
    el mensaje del usuario requiere información actual que debería investigarse.
    """
    import re

    mensaje_lower = mensaje_usuario.lower().strip()

    patrones_conversacion_basica = [
        r'^(hola|hol[aá]|buenas|buenos dias|buenos días|buenas tardes|buenas noches)\b',
        r'^(como estas|cómo estás|que tal|qué tal|como vas|cómo vas)\??$',
        r'^(gracias|muchas gracias|mil gracias|ok|vale|listo|perfecto)\b',
        r'^(adios|adiós|hasta luego|nos vemos|chao|chau)\b',
        r'^(que puedes hacer|qué puedes hacer|ayuda|ayudame|ayúdame)\??$',
    ]
    if any(re.search(patron, mensaje_lower) for patron in patrones_conversacion_basica):
        return False, None

    if usuario_pide_accion_explicita(mensaje_usuario):
        return False, None

    # Palabras/frases que indican que la IA no sabe la respuesta
    frases_no_sabe = [
        r'\bno lo sé\b', r'\bno se\b',
        r'\bno estoy seguro\b', r'\bno estoy segura\b',
        r'\bno tengo información\b', r'\bno dispongo de información\b',
        r'\bno estoy al tanto\b', r'\bno tengo idea\b',
        r'\bmi conocimiento es limitado\b', r'\bconocimiento limitado\b',
        r'\bno puedo confirmar\b', r'\bno confirmo\b',
        r'\bdesconozco\b', r'\bignoro\b',
        r'\bno estoy informado\b', r'\bno estoy informada\b',
        r'\bno tengo datos\b', r'\bsin datos\b',
        r'\bno estoy actualizado\b', r'\bno estoy actualizada\b',
        r'\bno sé con certeza\b', r'\bno lo sé con certeza\b',
        r'\bno puedo responder\b', r'\bno respondo\b',
        r'\bno sé decirte\b', r'\bno te puedo decir\b',
        r'\bno tengo acceso\b', r'\bsin acceso a\b',
        r'\bdesconocido\b', r'\binsuficiente\b',
    ]

    respuesta_lower = respuesta_ia.lower()
    for frase in frases_no_sabe:
        if re.search(frase, respuesta_lower):
            return True, "modelo_no_sabe"

    # Detectar si la respuesta menciona versiones antiguas o dice "a mi conocimiento"
    if re.search(r'a (mi|mi|el) conocimiento|hasta donde sé|hasta donde tengo|según mi|en mi entrenamiento|basado en mi|mi entrenamiento', respuesta_lower):
        return True, "conocimiento_desactualizado"

    # Detectar si la respuesta es muy genérica y evasiva
    if len(respuesta_ia) < 150 and re.search(r'no (puedo|puedo ayudarte|puedo responder)|lo siento|perdón|disculpa|lamentablemente', respuesta_lower):
        return True, "respuesta_evasiva"

    # Detectar respuestas que dicen que necesitan más contexto o detalles
    if re.search(r'necesito más (información|contexto|detalles)|podrías (darme|proporcionar|facilitar)|requiero más', respuesta_lower):
        return True, "necesita_mas_info"

    # Detectar respuestas muy cortas que no responden la pregunta
    if len(respuesta_ia) < 80 and not re.search(r'[.!?]|\n|¿|question', respuesta_ia):
        return True, "respuesta_muy_corta"

    # Palabras clave en el mensaje del usuario que requieren información actual
    palabras_info_actual = [
        # Información actualizada
        r'\bprecio\b', r'\bcosto\b', r'\bvalor\b', r'\bcuesta\b', r'\bcuestan\b',
        r'\bversión actual\b', r'\blatest version\b', r'\búltima versión\b', r'\bversión más reciente\b',
        r'\bnoticias\b', r'\bactualidad\b', r'\bhoy\b.*\bdía\b', r'\besta semana\b', r'\beste mes\b',
        r'\b2024\b', r'\b2025\b', r'\b2026\b', r'\b2027\b',
        r'\bnuevo\b.*\bmodelo\b', r'\bnueva\b.*\bversión\b', r'\blanzado\b', r'\blanzamiento\b',
        r'\bquién ganó\b', r'\bresultado\b', r'\bpartido\b', r'\bjuego\b', r'\bmatch\b',
        r'\bclima\b', r'\btiempo\b', r'\bpronóstico\b',
        # Instalación y configuración
        r'\bcómo instalar\b', r'\binstalar\b', r'\bconfigurar\b', r'\bsetup\b',
        r'\bpasos para\b', r'\btutorial\b', r'\bguía\b', r'\bhow to\b',
        # Errores y problemas
        r'\berror\b', r'\bproblema\b', r'\bsolución\b', r'\bfix\b', r'\bbug\b',
        r'\bno funciona\b', r'\bno me funciona\b', r'\bfunciona\b', r'\btrabaja\b',
        r'\bfalla\b', r'\bfallo\b', r'\bcrashea\b', r'\bcrash\b', r'\bfreeze\b',
        r'\bcompatibilidad\b', r'\bcompatible\b', r'\bno compatible\b',
        # Tecnologías específicas
        r'\blibrería\b', r'\bpaquete\b', r'\bframework\b', r'\bherramienta\b',
        r'\bhow does\b',
        # Búsqueda específica
        r'\bbuscar\b', r'\bbuscar\b', r'\bencuentra\b', r'\bfind\b',
        r'\binvestigar\b', r'\baveriguar\b', r'\bsaber sobre\b',
        # Documentación
        r'\bdocumentación\b', r'\bdocs\b', r'\bmanual\b', r'\bguía oficial\b',
        r'\bmejor\b.*\bopción\b', r'\bmejor\b.*\balternativa\b',
    ]

    if re.search(r'\b(abre|abrir)\b.*\b(google|navegador)\b.*\bbusca\b', mensaje_lower):
        return False, None

    for patron in palabras_info_actual:
        if re.search(patron, mensaje_lower):
            return True, "requiere_info_actual"

    # Detectar si el mensaje contiene términos técnicos específicos
    terminos_tecnicos = [
        r'\bapi\b', r'\bsdk\b', r'\bendpoint\b', r'\bwebhook\b',
        r'\bdocker\b', r'\bkubernetes\b', r'\bk8s\b',
        r'\breact\b', r'\bvue\b', r'\bangular\b', r'\bsvelte\b',
        r'\bdjango\b', r'\bflask\b', r'\bfastapi\b', r'\bnode\b', r'\bnext\b',
        r'\bpython\b', r'\bjavascript\b', r'\btypescript\b', r'\bjava\b',
        r'\bgit\b', r'\bgithub\b', r'\bgitlab\b', r'\bbitbucket\b',
        r'\baws\b', r'\bazure\b', r'\bgcp\b', r'\bgoogle cloud\b',
        r'\bdatabase\b', r'\bdb\b', r'\bsql\b', r'\bnosql\b', r'\bmongodb\b', r'\bpostgres\b',
        r'\bredis\b', r'\belasticsearch\b', r'\bkafka\b',
    ]

    for termino in terminos_tecnicos:
        if re.search(termino, mensaje_lower):
            # Si hay términos técnicos y la respuesta es genérica, buscar
            if len(respuesta_ia) < 150 or re.search(r'no (puedo|sé|tengo)', respuesta_lower):
                return True, "termino_tecnico_respuesta_generica"

    return False, None


def respuesta_basica_local(mensaje_usuario, perfil):
    """Respuestas rápidas que no necesitan modelo ni búsqueda web."""
    import re

    texto = mensaje_usuario.lower().strip()
    nombre_asistente = perfil.nombre_asistente or "su asistente"

    if re.search(r'^(hola|hol[aá]|buenas|buenos dias|buenos días|buenas tardes|buenas noches)\b', texto):
        return f"Hola, con mucho gusto. Soy {nombre_asistente}, a sus órdenes. ¿En qué le puedo ayudar?"

    if re.search(r'^(como estas|cómo estás|que tal|qué tal|como vas|cómo vas)\??$', texto):
        return "Muy bien, gracias por preguntar. Estoy listo para ayudarle."

    if re.search(r'^(gracias|muchas gracias|mil gracias)\b', texto):
        return "Con mucho gusto, a sus órdenes."

    if re.search(r'^(adios|adiós|hasta luego|nos vemos|chao|chau)\b', texto):
        return "Hasta luego. Quedo atento cuando me necesite."

    if re.search(r'^(que puedes hacer|qué puedes hacer|ayuda|ayudame|ayúdame)\??$', texto):
        return (
            "Puedo conversar con usted, ayudarle con desarrollo, revisar comandos, "
            "gestionar tareas, investigar cuando haga falta y asistirle con acciones del PC."
        )

    return None


def extraer_texto_cv(archivo):
    """Extrae texto del CV/PDF y lo limpia de notas internas."""
    import re

    try:
        if archivo.name.endswith('.pdf'):
            reader = PyPDF2.PdfReader(io.BytesIO(archivo.read()))
            paginas = []
            for idx, page in enumerate(reader.pages, start=1):
                texto_pagina = page.extract_text() or ''
                texto_pagina = texto_pagina.strip()
                if texto_pagina:
                    paginas.append(f"[Pagina {idx}]\n{texto_pagina}")
            texto = "\n\n".join(paginas)
        else:
            texto = archivo.read().decode('utf-8', errors='ignore')

        # Limpiar el texto extraído de notas internas
        if texto:
            # Eliminar notas entre *(...)* o [...]
            texto = re.sub(r'\*\([^)]*\)\*', '', texto)  # *(nota)*
            texto = re.sub(r'\[[^\]]*\*(?:.|\n)*?\*\]', '', texto)  # [*(nota)*]
            texto = re.sub(r'\[[^\]]*nota[^\]]*\]', '', texto, flags=re.IGNORECASE)  # [nota...]

            # Eliminar líneas con frases típicas de notas internas (incluyendo "aquí iría")
            patrones_notas = [
                r'.*nota para ti.*',
                r'.*en una situación real.*',
                r'.*adjuntarías el.*',
                r'.*esto es un ejemplo.*',
                r'.*placeholder.*',
                r'.*aquí iría.*',
                r'.*aqui iría.*',
                r'.*aquí iria.*',
                r'.*[aA]quí.*[iI]ría.*',
                r'.*\[Aquí.*',
                r'.*\[Imagen.*',
                r'.*\[PDF.*',
                r'.*\[aAquí.*PDF.*\].*',
                r'.*tarjeta de precios.*aquí.*',
                r'.*\[DEBUG\].*',
                r'.*\[INFO\].*',
            ]

            for patron in patrones_notas:
                texto = re.sub(patron, '', texto, flags=re.IGNORECASE | re.MULTILINE)

            # Eliminar emojis seguidos de texto entre corchetes (comunes en plantillas)
            texto = re.sub(r'🖼️\s*\*?\[.*?\]\*?', '', texto)  # 🖼️ [texto]
            texto = re.sub(r'📄\s*\*?\[.*?\]\*?', '', texto)  # 📄 [texto]
            texto = re.sub(r'📋\s*\*?\[.*?\]\*?', '', texto)  # 📋 [texto]
            texto = re.sub(r'[📊📈📉🖼️📎📄📋]\s*\*?\[.*?\]\*?', '', texto)  # Cualquier emoji de documento + [texto]

            # Eliminar cualquier línea que sea solo un emoji seguido de corchetes
            texto = re.sub(r'^\s*[^\w\s]\s*\[.*?\]\s*$', '', texto, flags=re.MULTILINE)

            # Eliminar líneas vacías múltiples
            texto = re.sub(r'\n\s*\n\s*\n+', '\n\n', texto)

            return texto.strip()

        return texto

    except Exception as e:
        return f"No se pudo extraer el texto: {str(e)}"


@require_http_methods(["GET", "POST"])
def login_usuario(request):
    if request.user.is_authenticated and request.user.is_active:
        next_url = request.GET.get('next') or settings.LOGIN_REDIRECT_URL
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            next_url = settings.LOGIN_REDIRECT_URL
        return redirect(next_url)

    aviso_pago_texto = (
        'El acceso está temporalmente bloqueado porque la suscripción no registra el pago al día. '
        'Cuando el pago sea confirmado, la cuenta podrá activarse nuevamente.'
    )
    if request.method == 'POST':
        username = (request.POST.get('username') or '').strip()
        password = request.POST.get('password') or ''
        next_url = request.POST.get('next') or settings.LOGIN_REDIRECT_URL
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            next_url = settings.LOGIN_REDIRECT_URL

        ip_cliente = obtener_ip_cliente(request)
        _, bloqueo_restante = login_estado_bloqueo(username, ip_cliente)
        if bloqueo_restante:
            messages.error(
                request,
                f'Demasiados intentos fallidos. Espera {bloqueo_restante} segundos antes de volver a intentar.',
            )
        else:
            user = authenticate(request, username=username, password=password)
            if user is None:
                espera = registrar_login_fallido(username, ip_cliente)
                if espera:
                    messages.error(
                        request,
                        f'Demasiados intentos fallidos. Espera {espera} segundos antes de volver a intentar.',
                    )
                else:
                    messages.error(request, 'Usuario o contrasena incorrectos.')
            elif not user.is_active:
                messages.error(request, 'Cuenta suspendida por pago pendiente. Contacta al administrador para reactivar el acceso.')
            else:
                limpiar_login_fallidos(username, ip_cliente)
                login(request, user)
                return redirect(next_url)

    if request.GET.get('inactive'):
        messages.error(request, 'Cuenta suspendida por pago pendiente. Contacta al administrador para reactivar el acceso.')

    return render(request, 'asistente/login.html', {
        'next': request.GET.get('next', ''),
        'username': username if request.method == 'POST' else '',
        'cuenta_activa': User.objects.filter(is_active=True).exists(),
        'aviso_pago_texto': aviso_pago_texto,
    })


@require_http_methods(["GET"])
def login_aviso_pago_audio(request):
    if User.objects.filter(is_active=True).exists():
        return JsonResponse({'audio_url': None})

    aviso_pago_texto = (
        'El acceso está temporalmente bloqueado porque la suscripción no registra el pago al día. '
        'Cuando el pago sea confirmado, la cuenta podrá activarse nuevamente.'
    )
    perfil = obtener_perfil_usuario(request)
    voz = perfil.voz_preferida if perfil else None
    velocidad = perfil.voz_velocidad if perfil else 1.0
    cache_key = hashlib.sha256(f'{aviso_pago_texto}|{voz or ""}|{velocidad}'.encode('utf-8')).hexdigest()[:24]
    cache_dir = settings.MEDIA_ROOT / 'audios'
    cache_dir.mkdir(parents=True, exist_ok=True)

    for extension in ('mp3', 'wav'):
        nombre_cache = f'login_aviso_pago_{cache_key}.{extension}'
        ruta_cache = cache_dir / nombre_cache
        if ruta_cache.exists():
            return JsonResponse({'audio_url': f'{settings.MEDIA_URL}audios/{nombre_cache}'})

    try:
        audio_url = TTSService().generar_audio(
            aviso_pago_texto,
            voz=voz,
            velocidad=velocidad,
        )
    except Exception as exc:
        print(f"[Login TTS] No se pudo generar audio del aviso de pago: {exc}")
        return JsonResponse({'audio_url': None}, status=503)

    ruta_generada = ruta_audio_desde_url(audio_url)
    if ruta_generada:
        extension = os.path.splitext(ruta_generada)[1].lower().lstrip('.') or 'mp3'
        if extension in ('mp3', 'wav'):
            nombre_cache = f'login_aviso_pago_{cache_key}.{extension}'
            ruta_cache = cache_dir / nombre_cache
            try:
                shutil.copyfile(ruta_generada, ruta_cache)
                audio_url = f'{settings.MEDIA_URL}audios/{nombre_cache}'
            except OSError as exc:
                print(f"[Login TTS] No se pudo guardar cache del aviso de pago: {exc}")

    return JsonResponse({'audio_url': audio_url})


@require_http_methods(["POST"])
def logout_usuario(request):
    logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)


# ─── DASHBOARD ───────────────────────────────────────────────
def dashboard(request):
    perfil = obtener_perfil_usuario(request)
    conversaciones = []
    if perfil:
        conversaciones = Conversacion.objects.filter(perfil=perfil).order_by('-creada_en')[:20]
    return render(request, 'asistente/dashboard.html', {
        'perfil': perfil,
        'conversaciones': conversaciones,
    })


def chat_page(request):
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/chat.html', {'perfil': perfil})


def tareas_page(request):
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/tareas.html', {'perfil': perfil})


def whatsapp_page(request):
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/whatsapp.html', {'perfil': perfil})


def facebook_page(request):
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/facebook.html', {'perfil': perfil})


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_conectar_linea(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    perfil = obtener_perfil_usuario(request)
    linea = linea_whatsapp_publica(data.get('linea', 'principal') or 'principal')
    linea_interna = linea_whatsapp_interna(perfil, linea)
    iniciado, mensaje = iniciar_baileys_si_hace_falta()
    if not iniciado:
        return JsonResponse({'error': mensaje}, status=503)

    try:
        response, payload = pedir_conexion_baileys(linea_interna, timeout=10)
    except requests.RequestException as exc:
        try:
            estado = pedir_estado_baileys(linea_interna, timeout=2)
        except requests.RequestException:
            estado = {}
        if estado.get('status') in ('connecting', 'connected') or estado.get('hasQR'):
            estado['service_message'] = mensaje
            estado['connection_warning'] = f'Baileys tardo en responder al conectar la linea: {exc}'
            return JsonResponse(estado, status=200)
        return JsonResponse(
            {'error': f'Baileys inicio, pero no pude conectar la linea: {exc}'},
            status=502,
        )

    if response.status_code >= 400:
        detalle = payload.get('error') or payload.get('raw_response') or response.reason
        estado = payload or {}
        if estado.get('status') in ('connecting', 'connected') or estado.get('hasQR'):
            estado['service_message'] = mensaje
            estado['connection_warning'] = f'Baileys respondio con estado HTTP {response.status_code}: {detalle}'
            return JsonResponse(estado, status=200)
        return JsonResponse(
            {'error': f'Baileys rechazo la conexion de la linea: {detalle}'},
            status=502,
        )

    estado = esperar_qr_o_conexion_baileys(linea_interna)
    if estado.get('hasQR') or estado.get('status') == 'connected':
        payload.update(estado)
    else:
        try:
            borrar_sesion_baileys_remota(linea_interna)
            response, payload = pedir_conexion_baileys(linea_interna)
            if response.status_code >= 400:
                detalle = payload.get('error') or payload.get('raw_response') or response.reason
                return JsonResponse(
                    {'error': f'Baileys rechazo la conexion de la linea: {detalle}'},
                    status=502,
                )
            estado = esperar_qr_o_conexion_baileys(linea_interna)
            payload.update(estado)
            payload['session_reset'] = True
        except requests.RequestException as exc:
            payload['reset_error'] = str(exc)

    payload['service_message'] = mensaje
    payload['linea'] = linea
    return JsonResponse(payload, status=response.status_code)


@require_http_methods(["GET"])
def whatsapp_sesiones(request):
    perfil = obtener_perfil_usuario(request)
    lineas = listar_lineas_whatsapp_guardadas(perfil)
    sesiones = []

    for linea in lineas:
        linea_interna = linea_whatsapp_interna(perfil, linea)
        estado = {'status': 'offline', 'hasQR': False}
        if baileys_esta_activo():
            try:
                estado = pedir_estado_baileys(linea_interna, timeout=2)
            except requests.RequestException:
                estado = {'status': 'offline', 'hasQR': False}

        sesiones.append({
            'linea': linea,
            'tiene_sesion': linea_tiene_sesion_baileys(linea_interna),
            'config': obtener_config_linea(linea_interna),
            'estado': estado,
        })

    return JsonResponse({'lineas': lineas, 'sesiones': sesiones})


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_iniciar_todo(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    perfil = obtener_perfil_usuario(request)
    lineas_payload = data.get('lineas')
    if isinstance(lineas_payload, list):
        lineas = [linea_whatsapp_publica(linea) for linea in lineas_payload if str(linea).strip()]
    else:
        lineas = listar_lineas_whatsapp_guardadas(perfil)

    lineas = sorted(set(lineas))
    if not lineas:
        return JsonResponse({'error': 'No hay lineas guardadas para iniciar'}, status=400)

    iniciado, mensaje = iniciar_baileys_si_hace_falta()
    if not iniciado:
        return JsonResponse({'error': mensaje}, status=503)

    resultados = []
    for linea in lineas:
        linea_interna = linea_whatsapp_interna(perfil, linea)
        try:
            estado_actual = pedir_estado_baileys(linea_interna, timeout=2)
            if estado_actual.get('status') == 'connected':
                resultados.append({'linea': linea, 'ok': True, **estado_actual})
                continue

            response, payload = pedir_conexion_baileys(linea_interna, timeout=4)
            if response.status_code >= 400:
                detalle = payload.get('error') or payload.get('raw_response') or response.reason
                resultados.append({'linea': linea, 'ok': False, 'error': detalle})
                continue

            estado = esperar_qr_o_conexion_baileys(linea_interna, segundos=4)
            payload.update(estado)
            resultados.append({'linea': linea, 'ok': True, **payload})
        except requests.RequestException as exc:
            resultados.append({'linea': linea, 'ok': False, 'error': str(exc)})

    return JsonResponse({
        'ok': any(item.get('ok') for item in resultados),
        'service_message': mensaje,
        'lineas': lineas,
        'resultados': resultados,
    })


@require_http_methods(["GET"])
def whatsapp_estado_linea(request, linea):
    perfil = obtener_perfil_usuario(request)
    linea = linea_whatsapp_interna(perfil, linea)
    try:
        response = requests.get(f"{baileys_service_url()}/status/{linea}", timeout=3)
    except requests.RequestException as exc:
        return JsonResponse(
            {'status': 'offline', 'hasQR': False, 'error': f'Baileys no responde: {exc}'},
            status=200,
        )

    payload = respuesta_json_o_texto(response) if response.content else {}
    if response.status_code >= 400:
        payload.setdefault('status', 'offline')
        payload.setdefault('hasQR', False)
    return JsonResponse(payload, status=200)


@require_http_methods(["GET"])
def whatsapp_qr_linea(request, linea):
    perfil = obtener_perfil_usuario(request)
    linea = linea_whatsapp_interna(perfil, linea)
    try:
        response = requests.get(
            f"{baileys_service_url()}/qr/{linea}",
            timeout=5,
        )
    except requests.RequestException as exc:
        return JsonResponse(
            {'qr': None, 'error': f'Baileys no responde: {exc}'},
            status=200,
        )

    payload = respuesta_json_o_texto(response) if response.content else {}
    return JsonResponse(payload, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_desconectar_linea(request, linea):
    perfil = obtener_perfil_usuario(request)
    linea = linea_whatsapp_interna(perfil, linea)
    try:
        response = requests.post(
            f"{baileys_service_url()}/disconnect/{linea}",
            timeout=8,
        )
    except requests.RequestException as exc:
        return JsonResponse({'error': f'Baileys no responde: {exc}'}, status=502)

    payload = respuesta_json_o_texto(response) if response.content else {}
    if response.status_code >= 400:
        return JsonResponse(payload, status=502)
    return JsonResponse(payload, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_borrar_memoria(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    linea = linea_whatsapp_publica(data.get('linea') or '')
    borrar_todo = bool(data.get('todo')) or not linea

    conversaciones = Conversacion.objects.filter(perfil=perfil)
    if borrar_todo:
        conversaciones = conversaciones.exclude(numero_whatsapp__startswith='web:').exclude(
            numero_whatsapp__startswith='desktop:'
        )
        alcance = 'todo WhatsApp'
    else:
        conversaciones = conversaciones.filter(numero_whatsapp__startswith=f'{linea}:')
        alcance = f'linea {linea}'

    total_conversaciones = conversaciones.count()
    total_mensajes = Mensaje.objects.filter(conversacion__in=conversaciones).count()
    conversaciones.delete()

    return JsonResponse({
        'ok': True,
        'alcance': alcance,
        'conversaciones_eliminadas': total_conversaciones,
        'mensajes_eliminados': total_mensajes,
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_config_lineas(request):
    perfil = obtener_perfil_usuario(request)
    if request.method == 'GET':
        lineas_param = request.GET.get('lineas', '')
        if lineas_param.strip():
            lineas = [
                linea_whatsapp_publica(linea)
                for linea in lineas_param.split(',')
                if linea.strip()
            ]
        else:
            lineas = listar_lineas_whatsapp_guardadas(perfil) or ['principal']
        return JsonResponse({
            'lineas': {linea: obtener_config_linea(linea_whatsapp_interna(perfil, linea)) for linea in lineas}
        })

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)

    linea = linea_whatsapp_publica(data.get('linea'))
    linea_interna = linea_whatsapp_interna(perfil, linea)
    config = cargar_config_whatsapp()
    actual = obtener_config_linea(linea_interna)

    if 'responder_chats' in data:
        actual['responder_chats'] = bool(data['responder_chats'])
    if 'responder_grupos' in data:
        actual['responder_grupos'] = bool(data['responder_grupos'])
    if 'leer_chats' in data:
        actual['leer_chats'] = bool(data['leer_chats'])
    if 'leer_grupos' in data:
        actual['leer_grupos'] = bool(data['leer_grupos'])
    if 'responder_voz' in data:
        actual['responder_voz'] = bool(data['responder_voz'])
    if data.get('desactivar_todo'):
        actual['responder_chats'] = False
        actual['responder_grupos'] = False
    if data.get('activar_todo'):
        actual['responder_chats'] = True
        actual['responder_grupos'] = True

    config[linea_interna] = {
        'responder_chats': actual['responder_chats'],
        'responder_grupos': actual['responder_grupos'],
        'leer_chats': actual['leer_chats'],
        'leer_grupos': actual['leer_grupos'],
        'responder_voz': actual['responder_voz'],
    }
    actual['linea'] = linea
    guardar_config_whatsapp(config)
    return JsonResponse({'ok': True, 'config': actual})


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_borrar_sesion(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    perfil = obtener_perfil_usuario(request)
    linea = linea_whatsapp_publica(data.get('linea'))
    linea_interna = linea_whatsapp_interna(perfil, linea)
    respuesta_baileys = None

    if baileys_esta_activo():
        try:
            response = requests.post(
                f"{baileys_service_url()}/delete-session/{linea_interna}",
                timeout=10,
            )
            respuesta_baileys = respuesta_json_o_texto(response) if response.content else {}
            if response.status_code >= 400:
                return JsonResponse({
                    'error': respuesta_baileys.get('error') or 'Baileys no pudo borrar la sesion',
                    'baileys': respuesta_baileys,
                }, status=502)
        except requests.RequestException as exc:
            print(f"[WhatsApp Sesion] Baileys no respondio al borrar sesion: {exc}")

    borrada_local, carpeta = borrar_sesion_baileys_local(linea_interna)
    return JsonResponse({
        'ok': True,
        'linea': linea,
        'auth_folder': carpeta,
        'auth_deleted': borrada_local or bool(respuesta_baileys and respuesta_baileys.get('authDeleted')),
        'baileys': respuesta_baileys,
        'message': 'Sesion eliminada. Si WhatsApp sigue mostrando el dispositivo vinculado, elimínalo tambien desde el telefono.',
    })


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_eliminar_linea(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    linea = linea_whatsapp_publica(data.get('linea'))
    perfil = obtener_perfil_usuario(request)
    linea_interna = linea_whatsapp_interna(perfil, linea)
    respuesta_baileys = None
    error_baileys = None

    if baileys_esta_activo():
        try:
            response = requests.post(
                f"{baileys_service_url()}/delete-session/{linea_interna}",
                timeout=10,
            )
            respuesta_baileys = respuesta_json_o_texto(response) if response.content else {}
            if response.status_code >= 400:
                error_baileys = respuesta_baileys.get('error') or 'Baileys no pudo borrar la sesion'
        except requests.RequestException as exc:
            error_baileys = str(exc)
            print(f"[WhatsApp Linea] Baileys no respondio al eliminar linea: {exc}")

    borrada_local, carpeta = borrar_sesion_baileys_local(linea_interna)
    config_eliminada = eliminar_config_linea_whatsapp(linea_interna)

    conversaciones_eliminadas = 0
    mensajes_eliminados = 0
    if perfil:
        conversaciones = Conversacion.objects.filter(
            perfil=perfil,
            numero_whatsapp__startswith=f'{linea}:',
        )
        conversaciones_eliminadas = conversaciones.count()
        mensajes_eliminados = Mensaje.objects.filter(conversacion__in=conversaciones).count()
        conversaciones.delete()

    advertencias = []
    if error_baileys:
        advertencias.append(
            'La linea se elimino localmente, pero Baileys no confirmo el cierre remoto.'
        )

    return JsonResponse({
        'ok': True,
        'linea': linea,
        'auth_folder': carpeta,
        'auth_deleted': borrada_local or bool(respuesta_baileys and respuesta_baileys.get('authDeleted')),
        'config_deleted': config_eliminada,
        'conversaciones_eliminadas': conversaciones_eliminadas,
        'mensajes_eliminados': mensajes_eliminados,
        'baileys': respuesta_baileys,
        'baileys_error': error_baileys,
        'warnings': advertencias,
        'message': 'Linea eliminada definitivamente.',
    }, status=200)


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_programar_masivo(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    linea = linea_whatsapp_publica(data.get('linea') or 'principal')
    linea_interna = linea_whatsapp_interna(perfil, linea)
    numeros = parsear_lista_numeros_whatsapp(data.get('numeros') or [])
    modo_contenido = (data.get('modo_contenido') or 'mensaje').strip().lower()
    mensaje = (data.get('mensaje') or '').strip()
    prompt = (data.get('prompt') or '').strip()
    formato = (data.get('formato') or 'texto').strip().lower()
    delay_unit = (data.get('delay_unit') or 'segundos').strip().lower()
    programado_para_str = (data.get('programado_para') or '').strip()

    if not numeros:
        return JsonResponse({'error': 'Agrega al menos un número válido'}, status=400)
    if modo_contenido not in ('mensaje', 'prompt'):
        return JsonResponse({'error': 'Selecciona mensaje específico o prompt'}, status=400)
    if modo_contenido == 'mensaje' and not mensaje:
        return JsonResponse({'error': 'Escribe el mensaje específico'}, status=400)
    if modo_contenido == 'prompt' and not prompt:
        return JsonResponse({'error': 'Escribe el prompt para generar el mensaje'}, status=400)
    if formato not in ('texto', 'audio'):
        return JsonResponse({'error': 'El formato debe ser texto o audio'}, status=400)
    if delay_unit not in ('segundos', 'minutos'):
        return JsonResponse({'error': 'La unidad de espera debe ser segundos o minutos'}, status=400)
    if not programado_para_str:
        return JsonResponse({'error': 'Selecciona fecha y hora de envío'}, status=400)

    try:
        if 'T' in programado_para_str:
            naive_dt = datetime.strptime(programado_para_str, '%Y-%m-%dT%H:%M')
            programado_para = timezone.make_aware(naive_dt)
        elif ' ' in programado_para_str:
            naive_dt = datetime.strptime(programado_para_str, '%Y-%m-%d %H:%M')
            programado_para = timezone.make_aware(naive_dt)
        else:
            return JsonResponse({'error': 'Usa fecha y hora completas'}, status=400)
    except ValueError:
        return JsonResponse({'error': 'Fecha/hora inválida'}, status=400)

    try:
        delay_min = float(data.get('delay_min') or 0)
        delay_max = float(data.get('delay_max') or delay_min)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'El tiempo de espera debe ser numérico'}, status=400)

    delay_min = max(0, delay_min)
    delay_max = max(delay_min, delay_max)
    titulo = (data.get('titulo') or f"Campaña WhatsApp {linea} ({len(numeros)} números)").strip()
    parametros = {
        'linea': linea_interna,
        'linea_publica': linea,
        'numeros': numeros,
        'modo_contenido': modo_contenido,
        'mensaje': mensaje,
        'prompt': prompt,
        'formato': formato,
        'delay_min': delay_min,
        'delay_max': delay_max,
        'delay_unit': delay_unit,
    }

    tarea = SchedulerService.crear_tarea(
        perfil=perfil,
        titulo=titulo,
        tipo_accion='whatsapp',
        parametros=parametros,
        programado_para=programado_para,
    )

    from .services import obtener_scheduler
    obtener_scheduler()

    return JsonResponse({
        'ok': True,
        'tarea': {
            'id': tarea.id,
            'titulo': tarea.titulo,
            'programado_para': tarea.programado_para.strftime('%Y-%m-%d %H:%M'),
            'estado': tarea.estado,
            'parametros': tarea.parametros,
        }
    })


@require_http_methods(["GET"])
def facebook_config(request):
    perfil = obtener_perfil_usuario(request)
    page_id = (getattr(perfil, 'meta_page_id', '') or '').strip()
    page_token = meta_page_token(perfil)
    app_secret = (getattr(perfil, 'meta_app_secret', '') or '').strip()
    verify_token = (getattr(perfil, 'meta_verify_token', '') or '').strip()
    webhook_url = (getattr(perfil, 'meta_webhook_url', '') or request.build_absolute_uri('/webhook/facebook/')).strip()
    return JsonResponse({
        'ok': True,
        'page_id': page_id,
        'graph_version': getattr(perfil, 'meta_graph_api_version', 'v25.0') or 'v25.0',
        'webhook_url': webhook_url,
        'config': obtener_config_facebook(perfil),
        'credenciales': {
            'page_id': bool(page_id),
            'page_access_token': bool(page_token),
            'app_secret': bool(app_secret),
            'verify_token': bool(verify_token),
        },
        'permisos_sugeridos': [
            'pages_messaging',
            'pages_manage_metadata',
            'pages_read_engagement',
            'pages_manage_engagement',
        ],
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def facebook_configuracion(request):
    perfil = obtener_perfil_usuario(request)
    if request.method == 'GET':
        return JsonResponse({'config': obtener_config_facebook(perfil)})
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)
    config = cargar_config_facebook()
    actual = obtener_config_facebook(perfil)
    for campo in FACEBOOK_CONFIG_DEFAULTS:
        if campo in data:
            actual[campo] = bool(data[campo])
    config[facebook_key_perfil(perfil)] = actual
    guardar_config_facebook(config)
    return JsonResponse({'ok': True, 'config': actual})


def facebook_mensajes(request):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'mensajes': []})
    tipo = request.GET.get('tipo', 'todos')
    conversaciones = Conversacion.objects.filter(perfil=perfil, numero_whatsapp__startswith='facebook:')
    if tipo in ('message', 'comment'):
        conversaciones = conversaciones.filter(numero_whatsapp__contains=f':{tipo}:')
    mensajes = Mensaje.objects.filter(conversacion__in=conversaciones, origen='entrante').order_by('-creado_en')[:60]
    return JsonResponse({'mensajes': [serializar_mensaje_whatsapp(m) for m in mensajes]})


def facebook_conversacion_detalle(request, conversacion_id):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)
    try:
        conversacion = Conversacion.objects.get(id=conversacion_id, perfil=perfil, numero_whatsapp__startswith='facebook:')
    except Conversacion.DoesNotExist:
        return JsonResponse({'error': 'Conversacion no encontrada'}, status=404)
    destino = datos_destino_facebook(conversacion)
    mensajes = reversed(list(conversacion.mensajes.order_by('-creado_en')[:80]))
    return JsonResponse({
        'conversacion': {'id': conversacion.id, 'contacto': conversacion.nombre_contacto, 'numero': conversacion.numero_whatsapp, **destino},
        'mensajes': [serializar_mensaje_whatsapp(m) for m in mensajes],
    })


@csrf_exempt
@require_http_methods(["POST"])
def facebook_conversacion_enviar(request, conversacion_id):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)
    try:
        conversacion = Conversacion.objects.get(id=conversacion_id, perfil=perfil, numero_whatsapp__startswith='facebook:')
    except Conversacion.DoesNotExist:
        return JsonResponse({'error': 'Conversacion no encontrada'}, status=404)
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}
    mensaje = (data.get('mensaje') or '').strip()
    if not mensaje:
        return JsonResponse({'error': 'Escribe un mensaje para enviar'}, status=400)
    destino = datos_destino_facebook(conversacion)
    if destino['tipo'] == 'message':
        ok, payload = enviar_mensaje_facebook(perfil, destino['externo_id'], mensaje)
    elif destino['tipo'] == 'comment':
        ok, payload = responder_comentario_facebook(perfil, destino['externo_id'], mensaje)
    else:
        return JsonResponse({'error': 'Destino Facebook no soportado'}, status=400)
    if not ok:
        return JsonResponse({'error': payload.get('error') or 'Meta no acepto el envio', 'detalle': payload}, status=502)
    msg = Mensaje.objects.create(conversacion=conversacion, tipo='texto', origen='saliente', contenido=mensaje, respondido=True)
    return JsonResponse({'ok': True, 'mensaje': serializar_mensaje_whatsapp(msg), 'meta': payload})


@csrf_exempt
@require_http_methods(["POST"])
def facebook_borrar_memoria(request):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)
    conversaciones = Conversacion.objects.filter(perfil=perfil, numero_whatsapp__startswith='facebook:')
    total_conversaciones = conversaciones.count()
    total_mensajes = Mensaje.objects.filter(conversacion__in=conversaciones).count()
    conversaciones.delete()
    return JsonResponse({'ok': True, 'conversaciones_eliminadas': total_conversaciones, 'mensajes_eliminados': total_mensajes})


def voz_page(request):
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/voz.html', {'perfil': perfil})


def configurar_perfil(request):
    if request.method == 'POST':
        nombre_usuario = request.POST.get('nombre_usuario')
        nombre_asistente = request.POST.get('nombre_asistente')
        cv_archivo = request.FILES.get('cv_archivo')
        modo_dev = request.POST.get('modo_desarrollador') == 'on'

        perfil, _ = obtener_perfil_usuario(request), False
        perfil.nombre_usuario = nombre_usuario
        perfil.nombre_asistente = nombre_asistente
        perfil.meta_graph_api_version = (request.POST.get('meta_graph_api_version') or 'v25.0').strip() or 'v25.0'
        perfil.meta_page_id = (request.POST.get('meta_page_id') or '').strip()
        perfil.meta_page_access_token = (request.POST.get('meta_page_access_token') or '').strip()
        perfil.meta_app_secret = (request.POST.get('meta_app_secret') or '').strip()
        perfil.meta_verify_token = (request.POST.get('meta_verify_token') or '').strip()
        perfil.meta_webhook_url = (request.POST.get('meta_webhook_url') or '').strip()

        if cv_archivo:
            perfil.cv_archivo = cv_archivo
            perfil.cv_texto = extraer_texto_cv(cv_archivo)

        perfil.save()
        return redirect('dashboard')

    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/configurar.html', {'perfil': perfil})


def usuarios_page(request):
    from django.db.models import Count, Q
    from django.db.models.functions import TruncDate
    from asistente.models import Cita

    perfil_actual = getattr(request.user, 'perfil_asistente', None)
    if not perfil_actual or not perfil_actual.puede_ver_seccion('usuarios'):
        return JsonResponse({'error': 'No autorizado'}, status=403)

    if request.method == 'POST':
        accion = request.POST.get('accion')
        user_id = request.POST.get('user_id')
        secciones_enviadas = request.POST.get('secciones_enviadas') == '1'
        modelos_enviados = request.POST.get('modelos_enviados') == '1'
        secciones_permitidas = [
            seccion for seccion in request.POST.getlist('secciones')
            if seccion in dict(SECCIONES_DISPONIBLES)
        ]

        if accion == 'crear':
            username = (request.POST.get('username') or '').strip()
            email = (request.POST.get('email') or '').strip()
            password = request.POST.get('password') or ''
            nombre_empresa = (request.POST.get('nombre_empresa') or username).strip()
            is_staff = request.POST.get('is_staff') == 'on'
            usar_groq_respuestas_normales = request.POST.get('usar_groq_respuestas_normales') == 'on'
            usar_groq_lexico_complejo = request.POST.get('usar_groq_lexico_complejo') == 'on'

            if not username or not password:
                messages.error(request, 'El usuario y la contraseña son obligatorios.')
            elif User.objects.filter(username=username).exists():
                messages.error(request, 'Ya existe un usuario con ese nombre.')
            else:
                usuario = User.objects.create_user(
                    username=username,
                    email=email,
                    password=password,
                    is_staff=is_staff,
                    is_active=True,
                )
                PerfilAsistente.objects.create(
                    usuario=usuario,
                    nombre_usuario=nombre_empresa,
                    nombre_asistente='Asistente',
                    secciones_permitidas=secciones_permitidas if secciones_enviadas else list(SECCIONES_PERMITIDAS_DEFAULT),
                    usar_groq_respuestas_normales=usar_groq_respuestas_normales,
                    usar_groq_lexico_complejo=usar_groq_lexico_complejo,
                )
                messages.success(request, f'Usuario {username} creado correctamente.')
            return redirect('usuarios_page')

        usuario = User.objects.filter(id=user_id).first()
        if not usuario:
            messages.error(request, 'Usuario no encontrado.')
            return redirect('usuarios_page')

        if accion == 'actualizar':
            username = (request.POST.get('username') or '').strip()
            email = (request.POST.get('email') or '').strip()
            nombre_empresa = (request.POST.get('nombre_empresa') or username).strip()

            if not username:
                messages.error(request, 'El nombre de usuario no puede quedar vacío.')
            elif User.objects.exclude(id=usuario.id).filter(username=username).exists():
                messages.error(request, 'Ya existe otro usuario con ese nombre.')
            else:
                usuario.username = username
                usuario.email = email
                usuario.is_staff = request.POST.get('is_staff') == 'on'
                usuario.save(update_fields=['username', 'email', 'is_staff'])
                perfil, _ = PerfilAsistente.objects.get_or_create(
                    usuario=usuario,
                    defaults={'nombre_usuario': nombre_empresa, 'nombre_asistente': 'Asistente'},
                )
                perfil.nombre_usuario = nombre_empresa
                update_fields = ['nombre_usuario', 'actualizado_en']
                if modelos_enviados:
                    perfil.usar_groq_respuestas_normales = request.POST.get('usar_groq_respuestas_normales') == 'on'
                    perfil.usar_groq_lexico_complejo = request.POST.get('usar_groq_lexico_complejo') == 'on'
                    update_fields.extend(['usar_groq_respuestas_normales', 'usar_groq_lexico_complejo'])
                if secciones_enviadas:
                    perfil.secciones_permitidas = secciones_permitidas
                    update_fields.append('secciones_permitidas')
                perfil.save(update_fields=update_fields)
                messages.success(request, f'Usuario {username} actualizado.')

        elif accion == 'password':
            password = request.POST.get('password') or ''
            if len(password) < 6:
                messages.error(request, 'La contraseña debe tener al menos 6 caracteres.')
            else:
                usuario.set_password(password)
                usuario.save(update_fields=['password'])
                messages.success(request, f'Contraseña actualizada para {usuario.username}.')

        elif accion == 'toggle_activo':
            if usuario.id == request.user.id:
                messages.error(request, 'No puedes inactivar tu propio usuario desde esta pantalla.')
            else:
                usuario.is_active = not usuario.is_active
                usuario.save(update_fields=['is_active'])
                estado = 'activado' if usuario.is_active else 'inactivado'
                messages.success(request, f'Usuario {usuario.username} {estado}.')

        return redirect('usuarios_page')

    usuarios = list(User.objects.select_related('perfil_asistente').order_by('-is_active', 'username'))
    hoy = timezone.localdate()
    inicio = hoy - timedelta(days=6)
    dias = [inicio + timedelta(days=i) for i in range(7)]

    for usuario in usuarios:
        perfil = getattr(usuario, 'perfil_asistente', None)
        uso = {
            'total': 0,
            'whatsapp': 0,
            'web': 0,
            'desktop': 0,
            'facebook': 0,
            'conversaciones': 0,
            'contactos': 0,
            'entrantes_total': 0,
            'salientes_total': 0,
            'texto': 0,
            'voz': 0,
            'imagen': 0,
            'citas': 0,
            'citas_pendientes': 0,
            'citas_confirmadas': 0,
            'citas_completadas': 0,
            'citas_canceladas': 0,
            'tareas': 0,
            'tareas_pendientes': 0,
            'tareas_completadas': 0,
            'tareas_fallidas': 0,
            'tareas_canceladas': 0,
            'dias': [],
            'ultima_actividad': 'Sin actividad',
            'chart': {
                'entrantes': '',
                'salientes': '',
                'area_entrantes': '',
                'area_salientes': '',
                'puntos_entrantes': [],
                'puntos_salientes': [],
            },
            'dominante': 'Sin actividad',
        }

        if perfil:
            mensajes = Mensaje.objects.filter(conversacion__perfil=perfil)
            conversaciones = Conversacion.objects.filter(perfil=perfil)
            citas_qs = Cita.objects.filter(perfil=perfil)
            tareas_qs = TareaProgramada.objects.filter(perfil=perfil)
            uso['total'] = mensajes.count()
            uso['web'] = mensajes.filter(conversacion__numero_whatsapp__startswith='web:').count()
            uso['desktop'] = mensajes.filter(conversacion__numero_whatsapp__startswith='desktop:').count()
            uso['facebook'] = mensajes.filter(conversacion__numero_whatsapp__startswith='facebook:').count()
            uso['whatsapp'] = mensajes.exclude(
                Q(conversacion__numero_whatsapp__startswith='web:') |
                Q(conversacion__numero_whatsapp__startswith='desktop:') |
                Q(conversacion__numero_whatsapp__startswith='facebook:')
            ).count()
            uso['conversaciones'] = conversaciones.count()
            uso['contactos'] = conversaciones.exclude(
                Q(numero_whatsapp__startswith='web:') |
                Q(numero_whatsapp__startswith='desktop:')
            ).values('numero_whatsapp').distinct().count()
            uso['entrantes_total'] = mensajes.filter(origen='entrante').count()
            uso['salientes_total'] = mensajes.filter(origen='saliente').count()
            uso['texto'] = mensajes.filter(tipo='texto').count()
            uso['voz'] = mensajes.filter(tipo='voz').count()
            uso['imagen'] = mensajes.filter(tipo='imagen').count()
            uso['citas'] = citas_qs.count()
            uso['citas_pendientes'] = citas_qs.filter(estado='pendiente').count()
            uso['citas_confirmadas'] = citas_qs.filter(estado='confirmada').count()
            uso['citas_completadas'] = citas_qs.filter(estado='completada').count()
            uso['citas_canceladas'] = citas_qs.filter(estado='cancelada').count()
            uso['tareas'] = tareas_qs.count()
            uso['tareas_pendientes'] = tareas_qs.filter(estado='pendiente').count()
            uso['tareas_completadas'] = tareas_qs.filter(estado='completada').count()
            uso['tareas_fallidas'] = tareas_qs.filter(estado='fallida').count()
            uso['tareas_canceladas'] = tareas_qs.filter(estado='cancelada').count()
            ultimo = mensajes.order_by('-creado_en').first()
            if ultimo:
                uso['ultima_actividad'] = timezone.localtime(ultimo.creado_en).strftime('%Y-%m-%d %H:%M')

            por_dia = mensajes.filter(creado_en__date__gte=inicio).annotate(
                dia=TruncDate('creado_en')
            ).values('dia', 'origen').annotate(total=Count('id'))
            mapa_dias = {(item['dia'], item['origen']): item['total'] for item in por_dia}
            entrantes = [mapa_dias.get((dia, 'entrante'), 0) for dia in dias]
            salientes = [mapa_dias.get((dia, 'saliente'), 0) for dia in dias]
            uso['dias'] = [
                {
                    'fecha': dia.strftime('%d/%m'),
                    'entrantes': entrantes[idx],
                    'salientes': salientes[idx],
                    'total': entrantes[idx] + salientes[idx],
                }
                for idx, dia in enumerate(dias)
            ]
            maximo = max(entrantes + salientes) if entrantes or salientes else 0

            def puntos_chart(valores):
                if not valores:
                    return []
                ancho = 150
                alto = 54
                paso = ancho / max(1, len(valores) - 1)
                return [
                    {
                        'x': round(idx * paso, 2),
                        'y': round(alto - ((valor / maximo) * 42) - 6, 2) if maximo else alto - 6,
                        'valor': valor,
                    }
                    for idx, valor in enumerate(valores)
                ]

            puntos_entrantes = puntos_chart(entrantes)
            puntos_salientes = puntos_chart(salientes)

            def path_linea(puntos):
                if not puntos:
                    return ''
                if len(puntos) == 1:
                    return f"M {puntos[0]['x']} {puntos[0]['y']}"
                partes = [f"M {puntos[0]['x']} {puntos[0]['y']}"]
                for idx in range(1, len(puntos)):
                    previo = puntos[idx - 1]
                    actual = puntos[idx]
                    medio_x = round((previo['x'] + actual['x']) / 2, 2)
                    partes.append(f"Q {previo['x']} {previo['y']} {medio_x} {round((previo['y'] + actual['y']) / 2, 2)}")
                    partes.append(f"T {actual['x']} {actual['y']}")
                return ' '.join(partes)

            def path_area(puntos):
                if not puntos:
                    return ''
                linea = path_linea(puntos)
                return f"{linea} L {puntos[-1]['x']} 60 L {puntos[0]['x']} 60 Z"

            uso['chart'] = {
                'entrantes': path_linea(puntos_entrantes),
                'salientes': path_linea(puntos_salientes),
                'area_entrantes': path_area(puntos_entrantes),
                'area_salientes': path_area(puntos_salientes),
                'puntos_entrantes': puntos_entrantes,
                'puntos_salientes': puntos_salientes,
            }

            canales = [
                ('WhatsApp', uso['whatsapp']),
                ('Web', uso['web']),
                ('Desktop', uso['desktop']),
                ('Facebook', uso['facebook']),
            ]
            uso['dominante'] = max(canales, key=lambda item: item[1])[0] if uso['total'] else 'Sin actividad'

        usuario.uso_bot = uso

    usuarios_uso = {
        str(usuario.id): {
            'username': usuario.username,
            'empresa': getattr(getattr(usuario, 'perfil_asistente', None), 'nombre_usuario', '') or '',
            'email': usuario.email or '',
            'is_staff': usuario.is_staff,
            'secciones_permitidas': getattr(getattr(usuario, 'perfil_asistente', None), 'secciones_permitidas', []) or [],
            'usar_groq_respuestas_normales': bool(getattr(getattr(usuario, 'perfil_asistente', None), 'usar_groq_respuestas_normales', False)),
            'usar_groq_lexico_complejo': bool(getattr(getattr(usuario, 'perfil_asistente', None), 'usar_groq_lexico_complejo', False)),
            **usuario.uso_bot,
        }
        for usuario in usuarios
    }

    return render(request, 'asistente/usuarios.html', {
        'usuarios': usuarios,
        'usuarios_uso': usuarios_uso,
        'secciones_disponibles': SECCIONES_DISPONIBLES,
    })


# ─── WEBHOOK FACEBOOK / META ─────────────────────────────────
@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_facebook(request):
    if request.method == 'GET':
        mode = request.GET.get('hub.mode')
        token = request.GET.get('hub.verify_token')
        challenge = request.GET.get('hub.challenge', '')
        perfil = perfil_facebook_por_verify_token(token)
        if mode == 'subscribe' and perfil:
            return HttpResponse(challenge, content_type='text/plain')
        return JsonResponse({'error': 'Verificacion fallida'}, status=403)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)

    page_ids = [str(entry.get('id') or '') for entry in data.get('entry', []) if entry.get('id')]
    perfiles = [perfil_facebook_por_page_id(page_id) for page_id in page_ids]
    perfiles = [perfil for perfil in perfiles if perfil]
    if not perfiles:
        return JsonResponse({'error': 'Perfil Facebook no configurado'}, status=400)
    if not any(verificar_firma_meta(request, perfil) for perfil in perfiles):
        return JsonResponse({'error': 'Firma Meta invalida'}, status=401)

    eventos = []
    for entry in data.get('entry', []):
        page_id = str(entry.get('id') or '')
        perfil = perfil_facebook_por_page_id(page_id)
        if not perfil:
            continue
        conf = obtener_config_facebook(perfil)

        for messaging in entry.get('messaging', []):
            sender_id = str((messaging.get('sender') or {}).get('id') or '')
            recipient_id = str((messaging.get('recipient') or {}).get('id') or page_id)
            if not sender_id or sender_id == recipient_id or 'message' not in messaging:
                continue
            contenido = ((messaging.get('message') or {}).get('text') or '[Mensaje sin texto]').strip()
            conversacion = conversacion_facebook(perfil, recipient_id, 'message', sender_id, f'Messenger {sender_id}')
            msg = Mensaje.objects.create(conversacion=conversacion, tipo='texto', origen='entrante', contenido=contenido)
            evento = {'tipo': 'message', 'mensaje_id': msg.id}
            eventos.append(evento)
            if conf['leer_mensajes']:
                anunciar_mensaje_whatsapp(perfil.id, conversacion.nombre_contacto, sender_id, contenido, 'texto')
            # ─── DETECCIÓN DE INTENCIÓN DE AGENDAMIENTO ─────────────────────
            from .services import CitaService

            cita_service = CitaService()

            print(f"[CITA DEBUG Facebook] Analizando mensaje: {contenido[:100]}...")

            if cita_service.detectar_intencion_agendamiento(contenido, conversacion=conversacion):
                print(f"[CITA] ✅ Intención de agendamiento detectada en mensaje de Facebook: {contenido[:50]}...")

                datos_cita = cita_service.extraer_datos_cita(contenido, perfil, conversacion=conversacion)

                print(f"[CITA] Datos extraídos: {datos_cita}")

                if datos_cita.get('completo') and datos_cita.get('fecha_hora'):
                    try:
                        # Crear la cita (con validación de conflictos)
                        cita, resultado = cita_service.crear_cita(conversacion, datos_cita, linea_whatsapp='facebook')

                        if resultado['exito']:
                            # Generar respuesta de confirmación
                            respuesta_texto = cita_service.formatear_confirmacion_cita(cita)

                            # Reproducir sonido de confirmación
                            try:
                                reproducir_sonido_cita()
                            except Exception as sonido_exc:
                                print(f"[CITA] No pude reproducir sonido: {sonido_exc}")

                            # Enviar respuesta de confirmación por Facebook
                            ok, payload = enviar_mensaje_facebook(perfil, sender_id, respuesta_texto)
                            Mensaje.objects.create(
                                conversacion=conversacion,
                                tipo='texto',
                                origen='saliente',
                                contenido=respuesta_texto,
                                respondido=ok,
                            )

                            evento['auto_respuesta'] = ok
                            evento['cita_agendada'] = True
                            evento['cita_id'] = cita.id

                            print(f"[CITA] Cita creada exitosamente: {cita.id}")
                        else:
                            # Hay conflicto - enviar mensaje de conflicto con horarios disponibles
                            respuesta_texto = resultado['mensaje']

                            # Enviar respuesta de conflicto por Facebook
                            ok, payload = enviar_mensaje_facebook(perfil, sender_id, respuesta_texto)
                            Mensaje.objects.create(
                                conversacion=conversacion,
                                tipo='texto',
                                origen='saliente',
                                contenido=respuesta_texto,
                                respondido=ok,
                            )

                            evento['auto_respuesta'] = ok
                            evento['cita_conflicto'] = True
                            if resultado.get('dia_completo'):
                                evento['dia_completo'] = True
                            if resultado['conflicto'] and resultado['conflicto'].get('cita_conflicto'):
                                evento['cita_conflicto_id'] = resultado['conflicto']['cita_conflicto'].id

                            print(f"[CITA] Conflicto detectado: {respuesta_texto[:100]}...")

                        continue  # No generar respuesta automática adicional

                    except Exception as cita_exc:
                        print(f"[CITA] Error creando cita: {cita_exc}")
                        # Continuar con respuesta normal del chat
                else:
                    print(f"[CITA] ⚠️ Datos incompletos para crear cita. completo={datos_cita.get('completo')}, fecha_hora={datos_cita.get('fecha_hora')}")
                    respuesta_texto = (
                        "Claro, lo coordinamos con gusto. "
                        "¿Prefieres que lo revisemos por llamada, WhatsApp o una reunión por Meet?"
                    )
                    ok, payload = enviar_mensaje_facebook(perfil, sender_id, respuesta_texto)
                    Mensaje.objects.create(
                        conversacion=conversacion,
                        tipo='texto',
                        origen='saliente',
                        contenido=respuesta_texto,
                        respondido=ok,
                    )
                    evento['auto_respuesta'] = ok
                    evento['cita_datos_incompletos'] = True
                    continue
            # ─── FIN DETECCIÓN DE AGENDAMIENTO ─────────────────────────────

            if conf['auto_mensajes']:
                print(f'[Facebook] Generando respuesta para mensaje: {contenido[:50]}')
                historial = construir_historial(conversacion, limite=20)[:-1]
                respuesta = GLMService().chat(
                    contenido,
                    perfil,
                    historial,
                    canal='facebook',
                    contacto={'nombre': conversacion.nombre_contacto, 'numero': sender_id, 'linea': 'Messenger', 'linea_numero': recipient_id},
                )
                print(f'[Facebook] Respuesta generada: {respuesta[:100]}...')
                respuesta = limpiar_respuesta_whatsapp(respuesta, perfil)
                if not respuesta.strip():
                    evento['auto_respuesta'] = False
                    evento['sin_modelo_activo'] = True
                    continue
                print(f'[Facebook] Enviando a Facebook API - sender_id: {sender_id}')
                ok, payload = enviar_mensaje_facebook(perfil, sender_id, respuesta)
                print(f'[Facebook] Resultado envio: ok={ok}, payload={payload}')
                Mensaje.objects.create(conversacion=conversacion, tipo='texto', origen='saliente', contenido=respuesta, respondido=ok)
                evento['auto_respuesta'] = ok
                if not ok:
                    evento['error'] = payload

        for change in entry.get('changes', []):
            if change.get('field') not in ('feed', 'comments'):
                continue
            value = change.get('value') or {}
            item = value.get('item')
            verb = value.get('verb')
            comment_id = str(value.get('comment_id') or value.get('id') or '')
            if item != 'comment' or verb not in ('add', 'edited') or not comment_id:
                continue
            contenido = (value.get('message') or '[Comentario sin texto]').strip()
            autor = (value.get('from') or {}).get('name') or f'Comentario {comment_id}'
            conversacion = conversacion_facebook(perfil, page_id, 'comment', comment_id, autor)
            msg = Mensaje.objects.create(conversacion=conversacion, tipo='texto', origen='entrante', contenido=contenido)
            evento = {'tipo': 'comment', 'mensaje_id': msg.id}
            eventos.append(evento)
            if conf['leer_comentarios']:
                anunciar_mensaje_whatsapp(perfil.id, autor, comment_id, contenido, 'texto')
            if conf['auto_comentarios'] and conf['responder_comentarios_publicamente']:
                historial = construir_historial(conversacion, limite=20)[:-1]
                respuesta = GLMService().chat(
                    contenido,
                    perfil,
                    historial,
                    canal='facebook',
                    contacto={'nombre': autor, 'numero': comment_id, 'linea': 'Comentarios', 'linea_numero': page_id},
                )
                respuesta = limpiar_respuesta_whatsapp(respuesta, perfil)
                if not respuesta.strip():
                    evento['auto_respuesta'] = False
                    evento['sin_modelo_activo'] = True
                    continue
                ok, payload = responder_comentario_facebook(perfil, comment_id, respuesta)
                Mensaje.objects.create(conversacion=conversacion, tipo='texto', origen='saliente', contenido=respuesta, respondido=ok)
                evento['auto_respuesta'] = ok
                if not ok:
                    evento['error'] = payload

    return JsonResponse({'ok': True, 'eventos': eventos})


# ─── WEBHOOK BAILEYS ─────────────────────────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def webhook_whatsapp(request):
    secret = request.headers.get('X-Webhook-Secret', '')
    if secret != settings.BAILEYS_WEBHOOK_SECRET:
        return JsonResponse({'error': 'No autorizado'}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    perfil, linea = perfil_desde_linea_whatsapp(data.get('linea', 'principal') or 'principal')
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    numero = data.get('numero', '')
    numero_real = numero_whatsapp_visible(data.get('numero_real', ''))
    linea_interna = data.get('linea', 'principal') or 'principal'
    linea = linea_whatsapp_publica(linea_interna)
    linea_numero = data.get('linea_numero', '')
    contenido = data.get('mensaje', '')
    tipo = data.get('tipo', 'texto')
    nombre_contacto = data.get('nombre', '')
    es_grupo = bool(data.get('es_grupo', False))
    remitente_grupo = data.get('remitente_grupo', '')
    audio_base64 = data.get('audio_base64')
    audio_mimetype = data.get('audio_mimetype', 'audio/ogg')
    es_audio_entrante = (
        tipo == 'voz'
        or contenido == '[Audio]'
        or bool(audio_base64)
        or str(audio_mimetype or '').startswith('audio/')
    )

    if es_audio_entrante:
        transcripcion = transcribir_audio_whatsapp(audio_base64, audio_mimetype)
        if transcripcion:
            contenido = transcripcion
        else:
            print(
                "[WhatsApp Voz] No se pudo transcribir; "
                f"audio_base64={'si' if audio_base64 else 'no'}, "
                f"mimetype={audio_mimetype or 'sin mimetype'}"
            )

    numero_conversacion = numero_real or numero
    conversacion_numero = f"{linea}:{numero_conversacion}"[:80]

    conversacion, _ = Conversacion.objects.get_or_create(
        perfil=perfil,
        numero_whatsapp=conversacion_numero,
        defaults={'nombre_contacto': nombre_contacto}
    )
    if nombre_contacto and conversacion.nombre_contacto != nombre_contacto:
        conversacion.nombre_contacto = nombre_contacto
        conversacion.save(update_fields=['nombre_contacto'])

    mensaje = Mensaje.objects.create(
        conversacion=conversacion,
        tipo=tipo,
        origen='entrante',
        contenido=contenido,
    )
    if debe_leer_whatsapp(linea_interna, es_grupo):
        anunciar_mensaje_whatsapp(perfil.id, nombre_contacto, numero, contenido, tipo)

    # ─── DETECCIÓN DE INTENCIÓN DE AGENDAMIENTO ─────────────────────
    from .services import CitaService

    cita_service = CitaService()

    # Debug: Imprimir información del mensaje
    print(f"[CITA DEBUG] 📍📍 Mensaje recibido - Tipo: {tipo}, Es grupo: {es_grupo}")
    print(f"[CITA DEBUG] 📍📍 Debe responder: {debe_responder_whatsapp(linea_interna, es_grupo)}")
    print(f"[CITA DEBUG] 📍📍 Contenido: {contenido[:100]}...")
    print(f"[CITA DEBUG] 📍📍 Línea: {linea_interna}")
    print(
        "[WhatsApp Voz] Respuesta audio: "
        f"entrante_audio={es_audio_entrante}, "
        f"switch={debe_responder_voz_whatsapp(linea_interna)}, "
        f"generar_audio={debe_responder_audio_por_mensaje(linea_interna, es_audio_entrante)}"
    )

    # Solo procesar agendamiento si no es grupo y se debe responder
    if not es_grupo and debe_responder_whatsapp(linea_interna, es_grupo):
        print(f"[CITA DEBUG] ✅ Pasando a detección de intención...")
        if cita_service.detectar_intencion_agendamiento(contenido, conversacion=conversacion):
            print(f"[CITA] ✅ Intención de agendamiento detectada en mensaje: {contenido[:50]}...")

            datos_cita = cita_service.extraer_datos_cita(contenido, perfil, conversacion=conversacion)

            print(f"[CITA] Datos extraídos: {datos_cita}")

            if datos_cita.get('completo') and datos_cita.get('fecha_hora'):
                try:
                    # Crear la cita (con validación de conflictos)
                    cita, resultado = cita_service.crear_cita(conversacion, datos_cita, linea_whatsapp=linea)

                    if resultado['exito']:
                        # Generar respuesta de confirmación
                        respuesta_texto = cita_service.formatear_confirmacion_cita(cita)

                        # Reproducir sonido de confirmación
                        try:
                            reproducir_sonido_cita()
                        except Exception as sonido_exc:
                            print(f"[CITA] No pude reproducir sonido: {sonido_exc}")

                        # Guardar respuesta
                        audio_url = None
                        if debe_responder_audio_por_mensaje(linea_interna, es_audio_entrante):
                            try:
                                audio_url = TTSService().generar_audio(
                                    respuesta_texto,
                                    voz=perfil.voz_preferida,
                                    velocidad=perfil.voz_velocidad,
                                )
                            except Exception as exc:
                                print(f"[CITA] No pude generar audio: {exc}")

                        Mensaje.objects.create(
                            conversacion=conversacion,
                            tipo='texto',
                            origen='saliente',
                            contenido=respuesta_texto,
                            audio_url=audio_url,
                            respondido=True,
                        )

                        return JsonResponse({
                            'respuesta': respuesta_texto,
                            'audio_url': audio_url,
                            'numero': numero,
                            'linea': linea,
                            'audio_recibido': es_audio_entrante,
                            'transcripcion': contenido if tipo == 'voz' and contenido != '[Audio]' else '',
                            'cita_agendada': True,
                            'cita_id': cita.id,
                        })
                    else:
                        # Hay conflicto - enviar mensaje de conflicto con horarios disponibles
                        respuesta_texto = resultado['mensaje']

                        # Generar audio si está configurado
                        audio_url = None
                        if debe_responder_audio_por_mensaje(linea_interna, es_audio_entrante):
                            try:
                                audio_url = TTSService().generar_audio(
                                    respuesta_texto,
                                    voz=perfil.voz_preferida,
                                    velocidad=perfil.voz_velocidad,
                                )
                            except Exception as exc:
                                print(f"[CITA] No pude generar audio: {exc}")

                        Mensaje.objects.create(
                            conversacion=conversacion,
                            tipo='texto',
                            origen='saliente',
                            contenido=respuesta_texto,
                            audio_url=audio_url,
                            respondido=True,
                        )

                        return JsonResponse({
                            'respuesta': respuesta_texto,
                            'audio_url': audio_url,
                            'numero': numero,
                            'linea': linea,
                            'audio_recibido': es_audio_entrante,
                            'transcripcion': contenido if tipo == 'voz' and contenido != '[Audio]' else '',
                            'cita_conflicto': True,
                            'cita_conflicto_id': resultado['conflicto'].get('cita_conflicto').id if resultado['conflicto'] and resultado['conflicto'].get('cita_conflicto') else None,
                            'dia_completo': resultado.get('dia_completo', False),
                        })

                except Exception as cita_exc:
                    print(f"[CITA] Error creando cita: {cita_exc}")
                    # Continuar con respuesta normal del chat
            else:
                print(f"[CITA] ⚠️ Datos incompletos para crear cita. completo={datos_cita.get('completo')}, fecha_hora={datos_cita.get('fecha_hora')}")
                respuesta_texto = (
                    "Claro, lo coordinamos con gusto. "
                    "¿Prefieres que lo revisemos por llamada, WhatsApp o una reunión por Meet?"
                )
                audio_url = None
                if debe_responder_audio_por_mensaje(linea_interna, es_audio_entrante):
                    try:
                        audio_url = TTSService().generar_audio(
                            respuesta_texto,
                            voz=perfil.voz_preferida,
                            velocidad=perfil.voz_velocidad,
                        )
                    except Exception as exc:
                        print(f"[CITA] No pude generar audio: {exc}")

                Mensaje.objects.create(
                    conversacion=conversacion,
                    tipo='texto',
                    origen='saliente',
                    contenido=respuesta_texto,
                    audio_url=audio_url,
                    respondido=True,
                )

                return JsonResponse({
                    'respuesta': respuesta_texto,
                    'audio_url': audio_url,
                    'numero': numero,
                    'linea': linea,
                    'audio_recibido': es_audio_entrante,
                    'transcripcion': contenido if tipo == 'voz' and contenido != '[Audio]' else '',
                    'cita_datos_incompletos': True,
                })
        else:
            print(f"[CITA] ❌ No se detectó intención de agendamiento en el mensaje")
    # ─── FIN DETECCIÓN DE AGENDAMIENTO ─────────────────────────────

    if not debe_responder_whatsapp(linea_interna, es_grupo):
        return JsonResponse({
            'respuesta': None,
            'audio_url': None,
            'numero': numero,
            'linea': linea,
            'es_grupo': es_grupo,
            'auto_respuesta': False,
        })

    if (tipo == 'voz' or contenido == '[Audio]') and contenido == '[Audio]':
        respuesta_texto = respuesta_audio_whatsapp()
    else:
        # Historial de conversacion sin duplicar el mensaje actual.
        historial = construir_historial(conversacion, limite=20)[:-1]

        glm = GLMService()
        respuesta_texto = glm.chat(
            contenido,
            perfil,
            historial,
            canal='whatsapp',
            contacto={
                'nombre': nombre_contacto,
                'numero': numero,
                'linea': linea,
                'linea_numero': linea_numero,
                'es_grupo': es_grupo,
                'remitente_grupo': remitente_grupo,
                'forzar_groq_primero': es_audio_entrante,
            },
        )
        respuesta_texto = limpiar_respuesta_whatsapp(respuesta_texto, perfil)

    if not (respuesta_texto or '').strip():
        return respuesta_silenciosa_json(
            numero=numero,
            linea=linea,
            es_grupo=es_grupo,
            audio_recibido=es_audio_entrante,
            transcripcion=contenido if tipo == 'voz' and contenido != '[Audio]' else '',
        )

    audio_url = None
    if debe_responder_audio_por_mensaje(linea_interna, es_audio_entrante):
        try:
            audio_url = TTSService().generar_audio(
                respuesta_texto,
                voz=perfil.voz_preferida,
                velocidad=perfil.voz_velocidad,
            )
        except Exception as exc:
            print(f"[WhatsApp Voz] No pude generar audio de respuesta: {exc}")

    # Guardar respuesta
    msg_saliente = Mensaje.objects.create(
        conversacion=conversacion,
        tipo='texto',
        origen='saliente',
        contenido=respuesta_texto,
        audio_url=audio_url,
        respondido=True,
    )

    return JsonResponse({
        'respuesta': respuesta_texto,
        'audio_url': audio_url,
        'numero': numero,
        'linea': linea,
        'audio_recibido': es_audio_entrante,
        'transcripcion': contenido if tipo == 'voz' and contenido != '[Audio]' else '',
    })


# ─── API CHAT DIRECTO (desde dashboard) ──────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def chat_directo(request):
    """Chat directo con soporte para comandos de desarrollo y respuesta inmediata"""
    try:
        data = json.loads(request.body)
        mensaje = data.get('mensaje', '')
        segundo_plano = data.get('segundo_plano', False)
        session_id = data.get('session_id')
        canal = data.get('canal', 'web')
    except:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Configure el perfil primero'}, status=400)

    if mensaje.startswith('[PRUEBA_VOZ]'):
        texto_prueba = mensaje.replace('[PRUEBA_VOZ]', '', 1).strip()
        audio_url = TTSService().generar_audio(
            texto_prueba,
            voz=perfil.voz_preferida,
            velocidad=perfil.voz_velocidad,
        )
        return JsonResponse({'respuesta': texto_prueba, 'audio_url': audio_url, 'comando_ejecutado': False})

    conversacion = obtener_conversacion_sesion(request, perfil, session_id=session_id, canal=canal)
    historial = construir_historial(conversacion, limite=20)
    Mensaje.objects.create(
        conversacion=conversacion,
        tipo='texto',
        origen='entrante',
        contenido=mensaje,
    )

    glm = GLMService()
    tts = TTSService()

    respuesta_local = respuesta_basica_local(mensaje, perfil)
    if respuesta_local:
        audio_url = tts.generar_audio(respuesta_local, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
        return respuesta_chat_json(
            conversacion,
            respuesta_local,
            audio_url,
            comando_ejecutado=False,
            respuesta_local=True,
        )

    # Detectar comandos especiales con /
    if mensaje.startswith("/"):
        if mensaje.startswith("/bg "):
            segundo_plano = True
            mensaje = mensaje[4:].strip()
            mensaje = normalizar_comando_extraido(mensaje)

        if mensaje.startswith("/web"):
            respuesta = procesar_comando_desarrollo(mensaje, perfil)
            audio_url = generar_audio_chat(tts, respuesta, perfil, canal)
            return respuesta_chat_json(
                conversacion, respuesta, audio_url,
                comando_ejecutado=False,
                investigacion_web=True,
            )

        if mensaje.startswith("/whatsapp_masivo"):
            respuesta = procesar_comando_desarrollo(mensaje, perfil)
            audio_url = generar_audio_chat(tts, respuesta, perfil, canal)
            return respuesta_chat_json(
                conversacion, respuesta, audio_url,
                comando_ejecutado=True,
                resultado_comando=respuesta,
            )

        if segundo_plano and mensaje not in ("/help", "/ayuda"):
            tarea = BackgroundTaskManager.crear(
                titulo=f"Comando: {mensaje}",
                comando=mensaje,
                target=procesar_comando_desarrollo,
                mensaje=mensaje,
                perfil=perfil,
                owner_id=request.user.id,
            )
            respuesta = f"Listo. Dejé esta tarea trabajando en segundo plano:\n{mensaje}\n\nID: {tarea['id']}"
            audio_url = tts.generar_audio("Listo. Dejé la tarea trabajando en segundo plano.", voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
            return respuesta_chat_json(
                conversacion, respuesta, audio_url,
                tarea_id=tarea['id'],
                tarea=tarea,
                comando_ejecutado=True,
                segundo_plano=True,
            )

        if mensaje not in ("/help", "/ayuda"):
            respuesta_previa = describir_accion_previa(mensaje)
            audio_url = tts.generar_audio(respuesta_previa, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
            return respuesta_chat_json(
                conversacion, respuesta_previa, audio_url,
                accion_pendiente=True,
                comando_pendiente=mensaje,
                comando_ejecutado=True,
            )

        respuesta = procesar_comando_desarrollo(mensaje, perfil)
        audio_url = tts.generar_audio(respuesta, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
        return respuesta_chat_json(conversacion, respuesta, audio_url)

    # Obtener respuesta de la IA
    respuesta_ia = glm.chat(mensaje, perfil, historial)
    if not (respuesta_ia or '').strip():
        return respuesta_silenciosa_json(
            session_memory=True,
            conversacion_id=conversacion.id,
        )

    # Detectar si la IA devuelve un comando entre corchetes [COMANDO]
    comando_extraido = extraer_comando_ia(respuesta_ia)
    if comando_extraido and comando_requiere_accion_explicita(comando_extraido) and not usuario_pide_accion_explicita(mensaje):
        print(f"[COMANDO] Ignorado por falta de intención explícita: {comando_extraido}")
        comando_extraido = None

    # Verificación automática: si la IA no sabe algo o requiere info actual, buscar en internet automáticamente
    if not comando_extraido:
        requiere_web, razon = requiere_investigacion_web(respuesta_ia, mensaje)
        if requiere_web:
            try:
                consulta = f"investigar: {mensaje}"
                respuesta_web = procesar_investigacion_web(consulta, perfil=perfil, pregunta_original=mensaje)
                audio_url = generar_audio_chat(tts, respuesta_web, perfil, canal)
                return respuesta_chat_json(
                    conversacion,
                    respuesta_web,
                    audio_url,
                    comando_ejecutado=False,
                    investigacion_web=True,
                    razon_investigacion=razon,
                )
            except Exception as e:
                # Si falla la búsqueda web, devolver la respuesta original de la IA
                import traceback
                print(f"[WEB_SEARCH] Error: {e}")
                print(traceback.format_exc())
                pass

    # Fallback adicional: si la respuesta es muy corta y parece genérica, buscar automáticamente
    if not comando_extraido and len(respuesta_ia.strip()) < 100:
        # Verificar si la respuesta es muy genérica o no responde la pregunta
        palabras_genericas = [
            'entendido', 'de acuerdo', 'correcto', 'perfecto', 'excelente',
            'claro', 'ok', 'vale', 'está bien', 'muy bien',
        ]
        respuesta_lower = respuesta_ia.lower().strip()
        if any(palabra in respuesta_lower for palabra in palabras_genericas):
            try:
                consulta = f"información detallada: {mensaje}"
                respuesta_web = procesar_investigacion_web(consulta, perfil=perfil, pregunta_original=mensaje)
                audio_url = generar_audio_chat(tts, respuesta_web, perfil, canal)
                return respuesta_chat_json(
                    conversacion,
                    respuesta_web,
                    audio_url,
                    comando_ejecutado=False,
                    investigacion_web=True,
                    razon_investigacion="respuesta_generica_corta",
                )
            except Exception as e:
                import traceback
                print(f"[WEB_SEARCH_FALLBACK] Error: {e}")
                print(traceback.format_exc())
                pass

    if comando_extraido:
        if comando_extraido.startswith("/web"):
            consulta = comando_extraido.replace("/web", "", 1).strip() or mensaje
            try:
                respuesta = procesar_investigacion_web(consulta, perfil=perfil, pregunta_original=mensaje)
            except Exception as e:
                respuesta = f"No pude consultar internet en este momento: {str(e)}"
            audio_url = generar_audio_chat(tts, respuesta, perfil, canal)
            return respuesta_chat_json(
                conversacion, respuesta, audio_url,
                comando_ejecutado=False,
                resultado_comando=respuesta,
                investigacion_web=True,
            )

        if comando_extraido.startswith("/whatsapp_masivo"):
            import re
            respuesta_limpia = re.sub(r'\[.*?\]', '', respuesta_ia).strip()
            resultado_cmd = procesar_comando_desarrollo(comando_extraido, perfil)
            respuesta = resultado_cmd or respuesta_limpia or "Proceso de WhatsApp finalizado."
            audio_url = generar_audio_chat(tts, respuesta, perfil, canal)
            return respuesta_chat_json(
                conversacion,
                respuesta,
                audio_url,
                comando_ejecutado=True,
                resultado_comando=resultado_cmd,
            )

        if comando_extraido.startswith("/bg "):
            import re
            respuesta_limpia = re.sub(r'\[.*?\]', '', respuesta_ia).strip()
            respuesta_previa = respuesta_limpia or describir_accion_previa(comando_extraido[4:].strip())
            audio_url = tts.generar_audio(respuesta_previa, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
            return respuesta_chat_json(
                conversacion, respuesta_previa, audio_url,
                accion_pendiente=True,
                comando_pendiente=comando_extraido,
                comando_ejecutado=True,
            )
        if usuario_confirmo(mensaje) and comando_pc_requiere_confirmacion(comando_extraido):
            comando_extraido = f"{comando_extraido} confirmar"

        # Extraer la respuesta de confirmación (antes del comando)
        import re
        respuesta_limpia = re.sub(r'\[.*?\]', '', respuesta_ia).strip()

        audio_final = comando_necesita_audio_final(comando_extraido)
        if not segundo_plano:
            respuesta_previa = respuesta_limpia or describir_accion_previa(comando_extraido)
            audio_url = tts.generar_audio(respuesta_previa, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
            return respuesta_chat_json(
                conversacion, respuesta_previa, audio_url,
                accion_pendiente=True,
                comando_pendiente=comando_extraido,
                comando_ejecutado=True,
            )

        audio_url = None if audio_final else tts.generar_audio(respuesta_limpia, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)

        ejecutar_en_segundo_plano = segundo_plano or debe_ejecutar_en_segundo_plano(mensaje, comando_extraido)
        if ejecutar_en_segundo_plano:
            tarea = BackgroundTaskManager.crear(
                titulo=mensaje[:80] or comando_extraido,
                comando=comando_extraido,
                target=procesar_comando_desarrollo,
                mensaje=comando_extraido,
                perfil=perfil,
                owner_id=request.user.id,
            )
            respuesta = (
                f"{respuesta_limpia}\n\n"
                f"Dejé la tarea trabajando en segundo plano.\nID: {tarea['id']}"
            )
            return respuesta_chat_json(
                conversacion, respuesta, audio_url,
                comando_ejecutado=True,
                segundo_plano=True,
                tarea_id=tarea['id'],
                tarea=tarea,
            )

        # Ejecutar el comando (esto puede tardar)
        resultado_cmd = procesar_comando_desarrollo(comando_extraido, perfil)

        # Construir respuesta final
        respuesta = f"{respuesta_limpia}\n\n{resultado_cmd}"
        if audio_final:
            audio_url = tts.generar_audio(respuesta, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)

        # Marcar que hay un comando en ejecución para que el frontend sepa
        return respuesta_chat_json(
            conversacion, respuesta, audio_url,
            comando_ejecutado=True,
            resultado_comando=resultado_cmd,
        )
    else:
        # Respuesta normal sin comandos
        audio_url = tts.generar_audio(respuesta_ia, voz=perfil.voz_preferida, velocidad=perfil.voz_velocidad)
        return respuesta_chat_json(
            conversacion, respuesta_ia, audio_url,
            comando_ejecutado=False,
        )


def extraer_comando_ia(respuesta):
    """Extrae comandos entre corchetes de la respuesta de la IA"""
    import re
    # Buscar patrones como [ABRIR:code], [CREAR:django:nombre], etc.
    patrones = {
        r'\[BG:([^\]]+)\]': r'/bg \1',
        r'\[CMD:([^\]]+)\]': r'/cmd \1',
        r'\[ABRIR:([^\]]+)\]': r'/abrir \1',
        r'\[CREAR:([^\:]+):([^\]]+)\]': r'/crear \1 \2',
        r'\[GIT:init\]': '/git init',
        r'\[GIT:clone:([^\]]+)\]': r'/git clone \1',
        r'\[GIT:status\]': '/git status',
        r'\[GIT:add:([^\]]+)\]': r'/git add \1',
        r'\[GIT:commit:([^\]]+)\]': r'/git commit \1',
        r'\[GIT:push\]': '/git push',
        r'\[GIT:pull\]': '/git pull',
        r'\[DOCKER:ps\]': '/docker ps',
        r'\[DOCKER:up\]': '/docker up',
        r'\[DOCKER:down\]': '/docker down',
        r'\[DOCKER:build\]': '/docker build',
        r'\[LEER:([^\]]+)\]': r'/leer \1',
        r'\[LS:([^\]]*)\]': r'/ls \1',
        r'\[SYS\]': '/sys',
        r'\[WEB:([^\]]+)\]': r'/web \1',
        r'\[PC:([^\]]+)\]': r'/pc \1',
        r'\[WHATSAPP_MASIVO:([^\]]+)\]': r'/whatsapp_masivo \1',
    }

    for patron, reemplazo in patrones.items():
        match = re.search(patron, respuesta)
        if match:
            if match.groups():
                # Reemplazar con los grupos capturados
                resultado = reemplazo
                for i, grupo in enumerate(match.groups(), 1):
                    resultado = resultado.replace(f'\\{i}', grupo)
                return normalizar_comando_extraido(resultado)
            else:
                return normalizar_comando_extraido(reemplazo)

    return None


def comando_requiere_accion_explicita(comando):
    return comando.startswith((
        "/abrir",
        "/crear",
        "/git",
        "/docker",
        "/cmd",
        "/leer",
        "/escribir",
        "/ls",
        "/sys",
        "/pc",
        "/whatsapp_masivo",
        "/bg ",
    ))


def usuario_pide_accion_explicita(mensaje):
    import re

    texto = mensaje.lower().strip()

    if texto.startswith("/"):
        return True

    tareas_desarrollo = [
        r'\barregla\b', r'\barreglar\b',
        r'\bsoluciona\b', r'\bsolucionar\b',
        r'\brepara\b', r'\breparar\b',
        r'\brevisa\b', r'\brevisar\b',
        r'\bdiagnostica\b', r'\bdiagnosticar\b',
        r'\binstala\b', r'\binstalar\b',
        r'\bconfigura\b', r'\bconfigurar\b',
        r'\bprueba\b', r'\bprobar\b',
        r'\btestea\b', r'\bejecuta pruebas\b',
        r'\bcompila\b', r'\bcompilar\b',
        r'\bdepura\b', r'\bdebug\b',
        r'\bimplementa\b', r'\bmodifica\b',
        r'\baplica\b.*\bmigraciones\b',
        r'\bmigrate\b', r'\bmakemigrations\b',
        r'\brunserver\b',
    ]
    if any(re.search(patron, texto) for patron in tareas_desarrollo):
        return True

    # Preguntas informativas: no deben convertirse en acciones del PC/terminal.
    if re.search(r'^(qué|que|cuál|cual|cuáles|cuales|cómo|como|por qué|porque|para qué|cuando|cuándo|dónde|donde)\b', texto):
        return False
    if re.search(r'\b(explica|explícame|explicame|ayúdame a entender|ayudame a entender|información|informacion|investiga|averigua|consulta|documentación|documentacion)\b', texto):
        return False

    acciones = [
        r'\babre\b', r'\babrir\b',
        r'\bcrea\b', r'\bcrear\b',
        r'\bejecuta\b', r'\bejecutar\b', r'\bcorre\b', r'\bcorrer\b',
        r'\bhaz\b', r'\bhacer\b',
        r'\binicia\b', r'\biniciar\b', r'\binicializa\b',
        r'\blevanta\b', r'\bdetén\b', r'\bdeten\b',
        r'\bcierra\b', r'\bcerrar\b',
        r'\bbloquea\b', r'\bsuspende\b', r'\bapaga\b', r'\breinicia\b',
        r'\blee\b', r'\bleer\b', r'\bescribe\b', r'\bescribir\b',
        r'\benvía\b', r'\benvia\b', r'\benviar\b', r'\benvíalo\b', r'\benvialo\b',
        r'\bmanda\b', r'\bmandar\b', r'\bmándalo\b', r'\bmandalo\b',
        r'\bwhatsapp\b', r'\bmensaje\b.*\bnúmeros\b', r'\bmensaje\b.*\bnumeros\b',
        r'\blista\b', r'\blistar\b',
        r'\bdiagnostica\b', r'\brevisa errores del pc\b',
        r'\bgit status\b', r'\bgit commit\b', r'\bgit push\b', r'\bgit pull\b',
        r'\bdocker ps\b', r'\bdocker up\b', r'\bdocker down\b',
    ]
    return any(re.search(patron, texto) for patron in acciones)


def normalizar_comando_extraido(comando):
    comando = comando.strip()
    if comando.startswith("/bg "):
        interno = normalizar_comando_extraido(comando[4:].strip())
        return f"/bg {interno}"
    if comando.startswith("PC:"):
        return f"/pc {comando[3:]}"
    if comando.startswith("ABRIR:"):
        return f"/abrir {comando[6:]}"
    if comando.startswith("SYS"):
        return "/sys"
    if comando.startswith("WEB:"):
        return f"/web {comando[4:]}"
    if comando.startswith("CMD:"):
        return f"/cmd {comando[4:]}"
    if comando.startswith("WHATSAPP_MASIVO:"):
        return f"/whatsapp_masivo {comando[17:]}"
    return comando


def usuario_confirmo(mensaje):
    texto = mensaje.lower()
    return any(palabra in texto for palabra in (" confirmar", " confirmado", " confirmo", " sí", " si ", "si,"))


def comando_pc_requiere_confirmacion(comando):
    comando = comando.lower()
    acciones = (
        "/pc suspender",
        "/pc hibernar",
        "/pc apagar",
        "/pc reiniciar",
    )
    return comando.startswith(acciones) and "confirmar" not in comando


def comando_necesita_audio_final(comando):
    comando = comando.lower()
    return comando.startswith(("/pc cerrar_app", "/pc cerrar_navegador"))


def describir_accion_previa(comando):
    partes = comando.split()
    if not partes:
        return "Con mucho gusto. Voy a ejecutar la acción."

    if comando.startswith("/pc cerrar_navegador"):
        objetivo = partes[2] if len(partes) > 2 else "el navegador"
        return f"Con mucho gusto. Voy a cerrar {objetivo}."
    if comando.startswith("/pc cerrar_app"):
        objetivo = " ".join(partes[2:]) if len(partes) > 2 else "la aplicación"
        return f"Con mucho gusto. Voy a cerrar {objetivo}."
    if comando.startswith("/pc abrir_url"):
        return "Con mucho gusto. Voy a abrir la página."
    if comando.startswith("/pc buscar"):
        consulta = " ".join(partes[2:])
        return f"Con mucho gusto. Voy a buscar {consulta}."
    if comando.startswith("/pc diagnostico"):
        return "Con mucho gusto. Voy a diagnosticar el PC."
    if comando.startswith("/pc errores"):
        return "Con mucho gusto. Voy a revisar los errores recientes del sistema."
    if comando.startswith("/abrir"):
        objetivo = " ".join(partes[1:]) if len(partes) > 1 else "la aplicación"
        return f"Con mucho gusto. Voy a abrir {objetivo}."
    if comando.startswith("/git"):
        return "Con mucho gusto. Voy a ejecutar la acción de Git."
    if comando.startswith("/docker"):
        return "Con mucho gusto. Voy a ejecutar la acción de Docker."
    if comando.startswith("/cmd"):
        return "Con mucho gusto. Voy a ejecutar el comando necesario en la terminal."
    if comando.startswith("/whatsapp_masivo"):
        return "Con mucho gusto. Voy a enviar el mensaje a la lista indicada de forma secuencial."
    if comando.startswith("/crear"):
        return "Con mucho gusto. Voy a crear el proyecto."
    return f"Con mucho gusto. Voy a ejecutar: {comando}"


def debe_ejecutar_en_segundo_plano(mensaje_usuario, comando):
    texto = f"{mensaje_usuario} {comando}".lower()
    claves = [
        "segundo plano", "tarea larga", "trabaja en", "trabajando en",
        "reporte", "auditoria", "auditoría", "analiza", "revisa todo",
        "instala", "descarga", "compila", "ejecuta tests", "docker build",
        "npm install", "pip install",
        "whatsapp_masivo", "varios numeros", "varios números",
    ]
    return any(clave in texto for clave in claves)


def limpiar_numero_whatsapp(numero):
    limpio = ''.join(c for c in str(numero or '') if c.isdigit())
    codigo_pais = ''.join(c for c in str(getattr(settings, 'WHATSAPP_DEFAULT_COUNTRY_CODE', '57')) if c.isdigit()) or '57'

    # Colombia: números móviles locales de 10 dígitos empiezan por 3.
    if len(limpio) == 10 and limpio.startswith('3'):
        return f"{codigo_pais}{limpio}"

    # Si alguien escribe 0314..., quitar prefijo nacional y agregar país.
    if len(limpio) == 11 and limpio.startswith('03'):
        return f"{codigo_pais}{limpio[1:]}"

    return limpio


def numero_whatsapp_visible(numero):
    """Devuelve solo telefonos reales, ocultando LID, grupos e IDs internos."""
    texto = str(numero or '').strip()
    if not texto:
        return ''

    if texto.startswith('facebook:'):
        return ''

    if ':' in texto and not texto.startswith(('web:', 'desktop:')):
        texto = texto.split(':', 1)[1]

    texto_lower = texto.lower()
    if (
        texto_lower.startswith(('web:', 'desktop:')) or
        '@lid' in texto_lower or
        '@g.us' in texto_lower or
        '-' in texto_lower
    ):
        return ''

    limpio = limpiar_numero_whatsapp(texto)
    if 8 <= len(limpio) <= 15:
        return limpio
    return ''


def parsear_lista_numeros_whatsapp(valor):
    if isinstance(valor, (list, tuple)):
        return list(valor)

    texto = str(valor or '').strip()
    if not texto:
        return []

    partes = [p.strip() for p in re.split(r'[,|\n]+', texto) if p.strip()]
    if len(partes) == 1:
        limpio = limpiar_numero_whatsapp(partes[0])
        if len(limpio) > 15 and re.search(r'\s+', partes[0]):
            partes = [p.strip() for p in re.split(r'\s+', partes[0]) if p.strip()]
    return partes


def extraer_parametros_whatsapp_masivo(argumentos):
    argumentos = (argumentos or '').strip()
    if not argumentos:
        return {}

    if argumentos.startswith('{'):
        data = json.loads(argumentos)
        numeros = data.get('numeros') or data.get('numeros_whatsapp') or data.get('destinatarios') or data.get('numero')
        if isinstance(numeros, str):
            numeros = parsear_lista_numeros_whatsapp(numeros)
        return {
            'linea': data.get('linea', 'yo'),
            'numeros': numeros or [],
            'mensaje': data.get('mensaje') or data.get('texto') or '',
            'delay_min': data.get('delay_min'),
            'delay_max': data.get('delay_max'),
        }

    pares = {}
    for parte in argumentos.split(';'):
        if '=' in parte:
            clave, valor = parte.split('=', 1)
            pares[clave.strip().lower()] = valor.strip()

    numeros = pares.get('numeros') or pares.get('destinatarios') or pares.get('numero') or ''
    return {
        'linea': pares.get('linea', 'yo'),
        'numeros': parsear_lista_numeros_whatsapp(numeros),
        'mensaje': pares.get('mensaje') or pares.get('texto') or '',
        'delay_min': pares.get('delay_min'),
        'delay_max': pares.get('delay_max'),
    }


def enviar_whatsapp_masivo(parametros, perfil=None):
    numeros = []
    vistos = set()
    for numero in parametros.get('numeros') or []:
        limpio = limpiar_numero_whatsapp(numero)
        if limpio and limpio not in vistos:
            numeros.append(limpio)
            vistos.add(limpio)

    mensaje = (parametros.get('mensaje') or '').strip()
    linea = linea_whatsapp_publica((parametros.get('linea') or 'yo').strip() or 'yo')
    linea_envio = linea_whatsapp_interna(perfil, linea)
    max_destinatarios = int(getattr(settings, 'WHATSAPP_BULK_MAX_RECIPIENTS', 50))

    if not numeros:
        return "No encontré números válidos para enviar."
    if not mensaje:
        return "Falta el texto exacto del mensaje."
    if len(numeros) > max_destinatarios:
        return f"La lista tiene {len(numeros)} números. Por seguridad el límite actual es {max_destinatarios} por lote."

    try:
        delay_min = float(parametros.get('delay_min') or getattr(settings, 'WHATSAPP_BULK_DELAY_MIN', 1.0))
        delay_max = float(parametros.get('delay_max') or getattr(settings, 'WHATSAPP_BULK_DELAY_MAX', 1.6))
    except (TypeError, ValueError):
        delay_min, delay_max = 1.0, 1.6

    delay_min = max(0.5, min(delay_min, 10.0))
    delay_max = max(delay_min, min(delay_max, 15.0))
    url_baileys = getattr(settings, 'BAILEYS_SERVICE_URL', None) or 'http://localhost:3002'

    enviados = []
    fallidos = []
    for idx, numero in enumerate(numeros, 1):
        try:
            response = requests.post(
                f"{url_baileys}/send-message",
                json={'numero': numero, 'mensaje': mensaje, 'linea': linea_envio},
                timeout=15,
            )
            if response.status_code == 200:
                enviados.append(numero)
            else:
                detalle = response.text[:180]
                fallidos.append(f"{numero}: {detalle}")
        except Exception as exc:
            fallidos.append(f"{numero}: {exc}")

        if idx < len(numeros):
            time.sleep(random.uniform(delay_min, delay_max))

    partes = [
        f"Envío terminado por la línea {linea}.",
        f"Enviados: {len(enviados)} de {len(numeros)}.",
    ]
    if fallidos:
        partes.append("Fallidos:\n" + "\n".join(f"- {item}" for item in fallidos[:10]))
        if len(fallidos) > 10:
            partes.append(f"...y {len(fallidos) - 10} fallos más.")
    return "\n".join(partes)


def respuesta_comando(exito, resultado, tarea):
    if exito:
        return resultado

    investigacion = WebResearchService().investigar_fallo(tarea, resultado)
    return (
        f"Error: {resultado}\n\n"
        "Busqué una solución según el sistema operativo y versión de este equipo:\n\n"
        f"{investigacion}"
    )


def procesar_investigacion_web(consulta, perfil=None, pregunta_original=None):
    investigacion = WebResearchService()
    contexto = investigacion.resumen_contexto_sistema()
    reporte = investigacion.investigar(consulta, max_results=6, incluir_contexto=False)
    resultados = reporte["resultados"]

    if not resultados:
        detalle_errores = "\n".join(reporte["errores"][:3])
        extra = f"\n\nErrores detectados:\n{detalle_errores}" if detalle_errores else ""
        return f"No encontré resultados útiles para: {consulta}{extra}"

    resumen = sintetizar_investigacion_web(
        pregunta_original or consulta,
        consulta,
        reporte,
        contexto,
        perfil,
    )
    if resumen:
        return limpiar_urls_de_texto(resumen)

    # Fallback sin síntesis: dar una respuesta más natural sin enlaces
    if len(resultados) == 1:
        titulo = limpiar_urls_de_texto(resultados[0].get('titulo', ''))
        return f"Según encontré: {titulo}"

    # Si hay múltiples resultados, combinarlos de forma natural
    titulos = [limpiar_urls_de_texto(r.get('titulo', '')) for r in resultados[:3]]
    if len(titulos) == 2:
        return f"Según mi búsqueda:\n• {titulos[0]}\n• {titulos[1]}"
    else:
        return f"Según mi búsqueda:\n• {titulos[0]}\n• {titulos[1]}\n• {titulos[2]}"


def limpiar_urls_de_texto(texto):
    """Quita enlaces para que el chat y el TTS digan solo el resultado."""
    import re

    if not texto:
        return texto

    texto = re.sub(r'https?://\S+', '', texto)
    texto = re.sub(r'www\.\S+', '', texto)
    texto = re.sub(
        r'\b[\w.-]+\.(com|co|org|net|io|dev|ai|app|edu|gov)(/\S*)?',
        '',
        texto,
        flags=re.IGNORECASE,
    )
    texto = re.sub(r'[ \t]+', ' ', texto)
    texto = re.sub(r' *\n *', '\n', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


def sintetizar_investigacion_web(pregunta, consulta, reporte, contexto, perfil=None):
    if not getattr(settings, "ZAI_API_KEY", ""):
        return None

    # Formatear resultados sin URLs para que el modelo genere una respuesta natural
    resultados_texto = "\n\n".join(
        f"{idx}. {limpiar_urls_de_texto(item['titulo'])}"
        for idx, item in enumerate(reporte["resultados"], 1)
    )

    system = (
        "Eres un asistente de desarrollo que responde preguntas basándose en resultados de búsqueda web. "
        "Tu objetivo es dar una respuesta clara, directa y útil en español. "
        "NO menciones URLs, enlaces, dominios, nombres de sitios ni fuentes en tu respuesta. "
        "NO deletrees ni leas direcciones web. "
        "NO digas 'según la búsqueda' o 'los resultados indican'. "
        "Simplemente responde la pregunta como si supieras la respuesta. "
        "Usa la información de los resultados para construir una respuesta completa y precisa. "
        "Si hay información contradictoria en los resultados, menciona las diferentes perspectivas. "
        "Sé conciso pero completo. Responde como si le estuvieras explicando a un colega desarrollador."
    )
    nombre = getattr(perfil, "nombre_usuario", "el usuario") if perfil else "el usuario"
    user = f"""Pregunta: {pregunta}

Resultados de búsqueda encontrados:
{resultados_texto}

Basándote en estos resultados, responde la pregunta de forma clara y directa.
No incluyas URLs, dominios, enlaces ni menciones las fuentes. Simplemente da la respuesta."""

    try:
        response = requests.post(
            f"{settings.ZAI_BASE_URL}/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": settings.ZAI_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": settings.ZAI_MODEL,
                "max_tokens": 1500,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=12,
        )
        response.raise_for_status()
        return limpiar_urls_de_texto(response.json()["content"][0]["text"])
    except Exception as e:
        print(f"[WEB_SYNTH] Error sintetizando respuesta: {e}")
        return None


# ─── API MENSAJES ─────────────────────────────────────────────
def serializar_mensaje_whatsapp(mensaje):
    return {
        'id': mensaje.id,
        'conversacion_id': mensaje.conversacion_id,
        'numero': mensaje.conversacion.numero_whatsapp,
        'contacto': mensaje.conversacion.nombre_contacto,
        'tipo': mensaje.tipo,
        'origen': mensaje.origen,
        'contenido': mensaje.contenido,
        'audio_url': mensaje.audio_url,
        'respondido': mensaje.respondido,
        'creado_en': mensaje.creado_en.strftime('%Y-%m-%d %H:%M'),
    }


def datos_destino_whatsapp(conversacion):
    numero_guardado = conversacion.numero_whatsapp or ''
    if ':' in numero_guardado:
        linea, numero = numero_guardado.split(':', 1)
    else:
        linea, numero = 'yo', numero_guardado
    if linea in ('desktop', 'web'):
        return linea, ''
    return (linea or 'yo').strip(), limpiar_numero_whatsapp(numero)


def mensajes_recientes(request):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'mensajes': []})

    mensajes = Mensaje.objects.filter(
        conversacion__perfil=perfil,
        origen='entrante',
        leido=False,
    ).order_by('-creado_en')[:20]

    data = [{
        'id': m.id,
        'conversacion_id': m.conversacion_id,
        'numero': m.conversacion.numero_whatsapp,
        'contacto': m.conversacion.nombre_contacto,
        'tipo': m.tipo,
        'contenido': m.contenido,
        'creado_en': m.creado_en.strftime('%Y-%m-%d %H:%M'),
    } for m in mensajes]

    return JsonResponse({'mensajes': data})


def whatsapp_conversacion_detalle(request, conversacion_id):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    try:
        conversacion = Conversacion.objects.get(id=conversacion_id, perfil=perfil)
    except Conversacion.DoesNotExist:
        return JsonResponse({'error': 'Conversación no encontrada'}, status=404)

    linea, numero = datos_destino_whatsapp(conversacion)
    mensajes = reversed(list(conversacion.mensajes.order_by('-creado_en')[:80]))
    return JsonResponse({
        'conversacion': {
            'id': conversacion.id,
            'contacto': conversacion.nombre_contacto,
            'numero': conversacion.numero_whatsapp,
            'linea': linea,
            'destino': numero,
        },
        'mensajes': [serializar_mensaje_whatsapp(m) for m in mensajes],
    })


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_conversacion_enviar(request, conversacion_id):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    try:
        conversacion = Conversacion.objects.get(id=conversacion_id, perfil=perfil)
    except Conversacion.DoesNotExist:
        return JsonResponse({'error': 'Conversación no encontrada'}, status=404)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    mensaje = (data.get('mensaje') or '').strip()
    if not mensaje:
        return JsonResponse({'error': 'Escribe un mensaje para enviar'}, status=400)

    linea, numero = datos_destino_whatsapp(conversacion)
    if not numero:
        return JsonResponse({'error': 'No encontré un número válido para esta conversación'}, status=400)

    url_baileys = getattr(settings, 'BAILEYS_SERVICE_URL', None) or 'http://localhost:3002'
    try:
        response = requests.post(
            f"{url_baileys}/send-message",
            json={'numero': numero, 'mensaje': mensaje, 'linea': linea_whatsapp_interna(perfil, linea)},
            timeout=15,
        )
        payload = response.json() if response.headers.get('content-type', '').startswith('application/json') else {'detalle': response.text[:300]}
    except Exception as exc:
        return JsonResponse({'error': f'No pude enviar por Baileys: {exc}'}, status=502)

    if response.status_code != 200:
        return JsonResponse({'error': payload.get('error') or 'No se pudo enviar el mensaje', 'detalle': payload}, status=response.status_code)

    msg = Mensaje.objects.create(
        conversacion=conversacion,
        tipo='texto',
        origen='saliente',
        contenido=mensaje,
        respondido=True,
    )
    return JsonResponse({'ok': True, 'mensaje': serializar_mensaje_whatsapp(msg), 'baileys': payload})


@csrf_exempt
@require_http_methods(["PATCH", "DELETE"])
def whatsapp_mensaje_detalle(request, mensaje_id):
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    try:
        mensaje = Mensaje.objects.select_related('conversacion').get(
            id=mensaje_id,
            conversacion__perfil=perfil,
        )
    except Mensaje.DoesNotExist:
        return JsonResponse({'error': 'Mensaje no encontrado'}, status=404)

    if request.method == 'DELETE':
        mensaje.delete()
        return JsonResponse({'ok': True})

    if mensaje.origen != 'saliente':
        return JsonResponse({'error': 'Solo puedes editar mensajes de respuesta o enviados por la IA'}, status=400)

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    contenido = (data.get('contenido') or '').strip()
    if not contenido:
        return JsonResponse({'error': 'El mensaje no puede quedar vacío'}, status=400)

    mensaje.contenido = contenido
    mensaje.save(update_fields=['contenido'])
    return JsonResponse({'ok': True, 'mensaje': serializar_mensaje_whatsapp(mensaje)})


def whatsapp_estadisticas(request):
    """Resumen de uso de WhatsApp para el dashboard."""
    from datetime import timedelta
    from django.db.models import Count, Q
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({
            'labels': [],
            'entrantes': [],
            'salientes': [],
            'total': 0,
            'entrantes_total': 0,
            'salientes_total': 0,
            'contactos': 0,
        })

    base = Mensaje.objects.filter(conversacion__perfil=perfil).exclude(
        Q(conversacion__numero_whatsapp__startswith='web:') |
        Q(conversacion__numero_whatsapp__startswith='desktop:')
    )

    hoy = timezone.localdate()
    inicio = hoy - timedelta(days=6)
    por_dia = base.filter(creado_en__date__gte=inicio).annotate(
        dia=TruncDate('creado_en')
    ).values('dia', 'origen').annotate(total=Count('id'))

    mapa = {(item['dia'], item['origen']): item['total'] for item in por_dia}
    dias = [inicio + timedelta(days=i) for i in range(7)]

    entrantes = [mapa.get((dia, 'entrante'), 0) for dia in dias]
    salientes = [mapa.get((dia, 'saliente'), 0) for dia in dias]

    return JsonResponse({
        'labels': [dia.strftime('%d/%m') for dia in dias],
        'entrantes': entrantes,
        'salientes': salientes,
        'total': base.count(),
        'entrantes_total': base.filter(origen='entrante').count(),
        'salientes_total': base.filter(origen='saliente').count(),
        'contactos': base.values('conversacion_id').distinct().count(),
    })


def chat_historial(request):
    """Devuelve el historial de la sesión actual del chat directo."""
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'mensajes': []})

    session_id = request.GET.get('session_id')
    canal = request.GET.get('canal', 'web')
    conversacion = obtener_conversacion_sesion(request, perfil, session_id=session_id, canal=canal)
    mensajes = conversacion.mensajes.order_by('-creado_en')[:50]
    data = [{
        'id': m.id,
        'rol': 'user' if m.origen == 'entrante' else 'assistant',
        'contenido': m.contenido,
        'audio_url': m.audio_url,
        'creado_en': m.creado_en.strftime('%Y-%m-%d %H:%M'),
    } for m in reversed(list(mensajes))]

    return JsonResponse({
        'mensajes': data,
        'conversacion_id': conversacion.id,
        'session_memory': True,
    })


# ─── API VOCES TTS ─────────────────────────────────────────────
def voces_disponibles(request):
    """Lista de voces TTS disponibles"""
    voces = TTSService.obtener_voces_disponibles()
    data = [{'codigo': v[0], 'nombre': v[1]} for v in voces]
    return JsonResponse({'voces': data})


@csrf_exempt
@require_http_methods(["POST"])
def actualizar_voz(request):
    """Actualizar la voz preferida del perfil"""
    try:
        data = json.loads(request.body)
        voz = data.get('voz')
        velocidad = data.get('velocidad', 1.0)
    except:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    if voz:
        perfil.voz_preferida = voz
    if velocidad:
        perfil.voz_velocidad = max(0.5, min(2.0, float(velocidad)))
    perfil.save()

    return JsonResponse({'success': True, 'voz': perfil.voz_preferida, 'velocidad': perfil.voz_velocidad})


def obtener_voz_actual(request):
    """Obtener la voz configurada actualmente"""
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'voz': 'piper:es_ES-mls_10246-low', 'velocidad': 1.0})

    return JsonResponse({'voz': perfil.voz_preferida, 'velocidad': perfil.voz_velocidad})


# ─── TAREAS EN SEGUNDO PLANO ────────────────────────────────────
def tareas_listar(request):
    """Lista tareas recientes ejecutadas en segundo plano."""
    return JsonResponse({'tareas': BackgroundTaskManager.listar(owner_id=request.user.id)})


def tarea_detalle(request, tarea_id):
    """Obtiene el estado y resultado de una tarea."""
    tarea = BackgroundTaskManager.obtener(tarea_id, owner_id=request.user.id)
    if not tarea:
        return JsonResponse({'error': 'Tarea no encontrada'}, status=404)
    return JsonResponse({'tarea': tarea})


def tarea_resumen_voz(request, tarea_id):
    """Genera un resumen breve con audio para una tarea finalizada."""
    tarea = BackgroundTaskManager.obtener(tarea_id, owner_id=request.user.id)
    if not tarea:
        return JsonResponse({'error': 'Tarea no encontrada'}, status=404)

    if tarea.get('estado') not in ('completada', 'error'):
        return JsonResponse({'error': 'La tarea aún no ha finalizado'}, status=409)

    resumen_guardado = tarea.get('resumen')
    audio_guardado = tarea.get('audio_url')
    if resumen_guardado and audio_guardado:
        return JsonResponse({'resumen': resumen_guardado, 'audio_url': audio_guardado, 'tarea': tarea})

    perfil = obtener_perfil_usuario(request)
    salida = tarea.get('resultado') if tarea.get('estado') == 'completada' else tarea.get('error')
    resumen = generar_resumen_tarea(tarea, salida or '')

    audio_url = None
    if perfil:
        audio_url = TTSService().generar_audio(
            resumen,
            voz=perfil.voz_preferida,
            velocidad=perfil.voz_velocidad,
        )

    BackgroundTaskManager._actualizar(tarea_id, resumen=resumen, audio_url=audio_url)
    tarea = BackgroundTaskManager.obtener(tarea_id, owner_id=request.user.id)
    return JsonResponse({'resumen': resumen, 'audio_url': audio_url, 'tarea': tarea})


@csrf_exempt
@require_http_methods(["POST"])
def ejecutar_accion_pendiente(request):
    """Ejecuta una acción después de que el cliente reprodujo el audio previo."""
    try:
        data = json.loads(request.body)
        comando = data.get('comando', '').strip()
        session_id = data.get('session_id')
        canal = data.get('canal', 'web')
    except:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    if not comando:
        return JsonResponse({'error': 'Comando vacío'}, status=400)

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Configure el perfil primero'}, status=400)

    conversacion = obtener_conversacion_sesion(request, perfil, session_id=session_id, canal=canal)

    ejecutar_como_bg = comando.startswith("/bg ")
    if ejecutar_como_bg:
        comando_real = normalizar_comando_extraido(comando[4:].strip())
    else:
        comando_real = normalizar_comando_extraido(comando)

    if comando_real.startswith("/web"):
        respuesta = procesar_comando_desarrollo(comando_real, perfil)
        audio_url = TTSService().generar_audio(
            respuesta,
            voz=perfil.voz_preferida,
            velocidad=perfil.voz_velocidad,
        )
        guardar_respuesta_chat(conversacion, respuesta, audio_url)
        return JsonResponse({
            'respuesta': respuesta,
            'audio_url': audio_url,
            'comando_ejecutado': False,
            'investigacion_web': True,
            'session_memory': True,
            'conversacion_id': conversacion.id,
        })

    tarea = BackgroundTaskManager.crear(
        titulo=f"Comando: {comando_real}",
        comando=comando_real,
        target=procesar_comando_desarrollo,
        mensaje=comando_real,
        perfil=perfil,
        owner_id=request.user.id,
    )

    if ejecutar_como_bg:
        respuesta = f"Ya inicié la tarea en segundo plano.\nID: {tarea['id']}"
        audio_texto = "Ya inicié la tarea en segundo plano."
    else:
        respuesta = f"Ya inicié la acción.\nID: {tarea['id']}"
        audio_texto = "Ya inicié la acción."

    audio_url = TTSService().generar_audio(
        audio_texto,
        voz=perfil.voz_preferida,
        velocidad=perfil.voz_velocidad,
    )
    guardar_respuesta_chat(conversacion, respuesta, audio_url)
    return JsonResponse({
        'respuesta': respuesta,
        'audio_url': audio_url,
        'segundo_plano': True,
        'tarea_id': tarea['id'],
        'tarea': tarea,
        'comando_ejecutado': True,
        'session_memory': True,
        'conversacion_id': conversacion.id,
    })


def generar_resumen_tarea(tarea, salida):
    """Resumen corto y hablable de una tarea, sin depender de otra llamada a IA."""
    import re

    if tarea.get('estado') == 'error':
        detalle = (salida or 'No hubo detalle del error.').strip()
        return f"La tarea terminó con error. Detalle principal: {detalle[:500]}"

    texto = salida or ''
    comando = tarea.get('comando', '')
    partes = ["Ya terminé la tarea."]

    if 'diagnostico' in comando or '## Disco' in texto:
        disco = re.search(r'/dev/\S+\s+\S+\s+\S+\s+\S+\s+(\d+)%\s+/', texto)
        memoria = re.search(r'Mem:\s+(\S+)\s+(\S+)\s+(\S+)', texto)
        carga = re.search(r'load average:\s*([0-9,.\s]+)', texto)
        servicios_ok = '0 loaded units listed' in texto

        if disco:
            uso = int(disco.group(1))
            if uso >= 90:
                partes.append(f"El punto crítico es el disco principal: está al {uso} por ciento de uso.")
            else:
                partes.append(f"El disco principal está al {uso} por ciento de uso.")

        if memoria:
            partes.append(f"La memoria reporta {memoria.group(2)} usado y {memoria.group(3)} libre.")

        if carga:
            partes.append(f"La carga promedio es {carga.group(1).strip()}, conviene revisarla si el equipo se siente lento.")

        if servicios_ok:
            partes.append("No encontré servicios fallidos de systemd.")

        partes.append("Mi recomendación inicial es liberar espacio en disco, cerrar procesos pesados y volver a revisar la carga.")
        return " ".join(partes)

    limpio = " ".join(texto.split())
    if not limpio:
        return "Ya terminé la tarea. El comando no devolvió salida."
    return f"Ya terminé la tarea. Resumen del resultado: {limpio[:700]}"


# ─── TERMINAL WEB ────────────────────────────────────────────────

# ─── API ALARMAS / TAREAS PROGRAMADAS ─────────────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def crear_alarma(request):
    """Crea una nueva alarma/tarea programada."""
    try:
        data = json.loads(request.body)
        titulo = data.get('titulo')
        tipo_accion = data.get('tipo_accion')
        parametros = data.get('parametros', {})
        programado_para_str = data.get('programado_para')
        repetir = data.get('repetir')
    except:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    if not titulo or not tipo_accion or not programado_para_str:
        return JsonResponse({'error': 'Faltan campos requeridos: titulo, tipo_accion, programado_para'}, status=400)

    try:
        if tipo_accion == 'whatsapp':
            parametros = dict(parametros or {})
            linea_publica = linea_whatsapp_publica(parametros.get('linea') or 'principal')
            parametros['linea'] = linea_whatsapp_interna(perfil, linea_publica)
            parametros['linea_publica'] = linea_publica

        # Parsear la fecha/hora
        from datetime import timedelta

        # Soportar formatos relativos como "+30min", "+1h", "+2dias"
        if programado_para_str.startswith('+'):
            cantidad_str = programado_para_str[1:]
            if 'min' in cantidad_str.lower():
                minutos = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = timezone.now() + timedelta(minutes=minutos)
            elif 'h' in cantidad_str.lower() or 'hr' in cantidad_str.lower():
                horas = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = timezone.now() + timedelta(hours=horas)
            elif 'dia' in cantidad_str.lower():
                dias = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = timezone.now() + timedelta(days=dias)
            else:
                return JsonResponse({'error': f'Formato relativo no reconocido: {programado_para_str}'}, status=400)
        else:
            # Formato: "YYYY-MM-DD HH:MM" o "HH:MM" (para hoy)
            if ' ' in programado_para_str:
                naive_dt = datetime.strptime(programado_para_str, '%Y-%m-%d %H:%M')
                programado_para = timezone.make_aware(naive_dt)
            else:
                # Solo hora, para hoy
                hoy = timezone.now().date()
                hora = datetime.strptime(programado_para_str, '%H:%M').time()
                naive_dt = datetime.combine(hoy, hora)
                programado_para = timezone.make_aware(naive_dt)

                # Si la hora ya pasó, asumir que es para mañana
                if programado_para <= timezone.now():
                    programado_para = programado_para + timedelta(days=1)

        # Crear la tarea
        tarea = SchedulerService.crear_tarea(
            perfil=perfil,
            titulo=titulo,
            tipo_accion=tipo_accion,
            parametros=parametros,
            programado_para=programado_para,
            repetir=repetir
        )

        # Asegurar que el scheduler esté corriendo
        from .services import obtener_scheduler
        obtener_scheduler()

        return JsonResponse({
            'success': True,
            'tarea': {
                'id': tarea.id,
                'titulo': tarea.titulo,
                'tipo_accion': tarea.tipo_accion,
                'programado_para': tarea.programado_para.isoformat(),
                'estado': tarea.estado,
                'repetir': tarea.repetir,
            }
        })

    except ValueError as e:
        return JsonResponse({'error': f'Error en formato de fecha/hora: {str(e)}'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@require_http_methods(["GET"])
def listar_alarmas(request):
    """Lista las alarmas/tareas programadas pendientes."""
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'alarmas': []})

    estado = request.GET.get('estado', 'pendiente')
    limite = int(request.GET.get('limite', 20))

    tareas = TareaProgramada.objects.filter(
        perfil=perfil,
        estado=estado
    ).order_by('programado_para')[:limite]

    data = [{
        'id': t.id,
        'titulo': t.titulo,
        'tipo_accion': t.tipo_accion,
        'tipo_accion_display': t.get_tipo_accion_display(),
        'parametros': t.parametros,
        'programado_para': t.programado_para.isoformat(),
        'programado_para_formatted': t.programado_para.strftime('%Y-%m-%d %H:%M'),
        'estado': t.estado,
        'estado_display': t.get_estado_display(),
        'repetir': t.repetir,
        'creado_en': t.creado_en.isoformat(),
    } for t in tareas]

    return JsonResponse({'alarmas': data})


@csrf_exempt
@require_http_methods(["POST", "DELETE"])
def cancelar_alarma(request, tarea_id):
    """Cancela una alarma/tarea programada."""
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    try:
        tarea = TareaProgramada.objects.get(id=tarea_id, perfil=perfil)
        if tarea.estado == 'completada':
            return JsonResponse({'error': 'No se puede cancelar una tarea ya completada'}, status=400)

        tarea.cancelar()
        return JsonResponse({'success': True, 'mensaje': 'Alarma cancelada'})

    except TareaProgramada.DoesNotExist:
        return JsonResponse({'error': 'Tarea no encontrada'}, status=404)


@require_http_methods(["GET"])
def listar_todas_tareas(request):
    """Lista todas las tareas programadas (incluyendo completadas y fallidas)."""
    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'tareas': []})

    tareas = TareaProgramada.objects.filter(
        perfil=perfil
    ).order_by('-creado_en')[:50]

    data = [{
        'id': t.id,
        'titulo': t.titulo,
        'tipo_accion': t.tipo_accion,
        'tipo_accion_display': t.get_tipo_accion_display(),
        'parametros': t.parametros,
        'programado_para': t.programado_para.isoformat(),
        'programado_para_formatted': t.programado_para.strftime('%Y-%m-%d %H:%M'),
        'estado': t.estado,
        'estado_display': t.get_estado_display(),
        'resultado': t.resultado,
        'error': t.error,
        'repetir': t.repetir,
        'creado_en': t.creado_en.isoformat(),
        'ejecutado_en': t.ejecutado_en.isoformat() if t.ejecutado_en else None,
    } for t in tareas]

    return JsonResponse({'tareas': data})


@require_http_methods(["GET"])
def alarmas_resumen(request):
    """Retorna un resumen de las alarmas (pendientes, completadas hoy, etc)."""
    from django.db.models import Count, Q
    from django.utils import timezone

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'resumen': {}})

    hoy = timezone.now().date()

    resumen = TareaProgramada.objects.filter(perfil=perfil).aggregate(
        pendientes=Count('id', filter=Q(estado='pendiente')),
        ejecutando=Count('id', filter=Q(estado='ejecutando')),
        completadas_hoy=Count('id', filter=Q(estado='completada') & Q(ejecutado_en__date=hoy)),
        fallidas=Count('id', filter=Q(estado='fallida')),
    )

    # Próxima alarma pendiente
    proxima = TareaProgramada.objects.filter(
        perfil=perfil,
        estado='pendiente'
    ).order_by('programado_para').first()

    resumen['proxima_alarma'] = {
        'id': proxima.id,
        'titulo': proxima.titulo,
        'programado_para': proxima.programado_para.isoformat(),
        'programado_para_formatted': proxima.programado_para.strftime('%Y-%m-%d %H:%M'),
    } if proxima else None

    return JsonResponse({'resumen': resumen})


# ─── INICIAR SCHEDULER ─────────────────────────────────────────────
@require_http_methods(["POST"])
def scheduler_iniciar(request):
    """Asegura que el scheduler esté corriendo."""
    from .services import obtener_scheduler

    try:
        scheduler = obtener_scheduler()
        return JsonResponse({
            'success': True,
            'mensaje': 'Scheduler iniciado/corriendo',
            'intervalo_segundos': scheduler.intervalo
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


def politica_privacidad(request):
    return HttpResponse("""<!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Política de Privacidad - DAGI Bot</title>
            <style>
                body { font-family: Arial, sans-serif; max-width: 800px; margin: 40px auto; padding: 20px; color: #333; }
                h1 { color: #1a73e8; }
                h2 { color: #444; margin-top: 30px; }
                p { line-height: 1.7; }
                footer { margin-top: 40px; color: #888; font-size: 0.9em; }
            </style>
        </head>
        <body>
            <h1>Política de Privacidad</h1>
            <p><strong>DAGI – Desarrollo de Aplicaciones para la Gestión Inteligente</strong></p>
            <p>Última actualización: 20 de mayo de 2026</p>

            <h2>1. Información que recopilamos</h2>
            <p>DAGI Bot recopila únicamente los mensajes enviados por los usuarios a través de Facebook Messenger y los comentarios realizados en publicaciones de la página oficial de DAGI en Facebook. Esta información se utiliza exclusivamente para responder de forma automática a las consultas de los clientes.</p>

            <h2>2. Uso de la información</h2>
            <p>La información recopilada se utiliza para:</p>
            <ul>
                <li>Responder automáticamente a mensajes y comentarios de clientes.</li>
                <li>Generar estadísticas internas sobre preguntas y respuestas frecuentes para mejorar la calidad del servicio.</li>
            </ul>

            <h2>3. Almacenamiento y seguridad</h2>
            <p>Los datos recopilados se almacenan en servidores seguros y no son compartidos con terceros. Implementamos medidas técnicas y organizativas para proteger la información contra accesos no autorizados.</p>

            <h2>4. Retención de datos</h2>
            <p>Los datos de conversación se conservan únicamente durante el tiempo necesario para prestar el servicio y mejorar la experiencia del usuario. El usuario puede solicitar la eliminación de sus datos en cualquier momento.</p>

            <h2>5. Derechos del usuario</h2>
            <p>Los usuarios tienen derecho a:</p>
            <ul>
                <li>Acceder a sus datos personales.</li>
                <li>Solicitar la corrección o eliminación de sus datos.</li>
                <li>Oponerse al tratamiento de sus datos.</li>
            </ul>

            <h2>6. Contacto</h2>
            <p>Para cualquier consulta relacionada con esta política de privacidad, puede contactarnos a través de:</p>
            <p>Correo electrónico: <a href="mailto:dagidesarrollo@gmail.com">dagidesarrollo@gmail.com</a></p>

            <h2>7. Cambios en esta política</h2>
            <p>DAGI se reserva el derecho de actualizar esta política de privacidad en cualquier momento. Los cambios serán publicados en esta misma página.</p>

            <footer>
                <p>© 2026 DAGI – Desarrollo de Aplicaciones para la Gestión Inteligente. Todos los derechos reservados.</p>
            </footer>
        </body>
        </html>""", content_type='text/html')


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_facebook(request):
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"[WEBHOOK FB] {request.method} - Headers: {dict(request.headers)} - Body: {request.body[:500]}")
    print(f"[WEBHOOK FB] {request.method} llegó", flush=True)


# ─── PÁGINA DE CITAS ──────────────────────────────────────────────
@require_http_methods(["GET"])
def citas_page(request):
    """Página principal de citas y agendamientos."""
    perfil = obtener_perfil_usuario(request)
    return render(request, 'asistente/citas.html', {'perfil': perfil})


@csrf_exempt
@require_http_methods(["GET"])
def citas_listar(request):
    """API para listar citas del usuario actual."""
    from asistente.models import Cita
    from django.core.paginator import Paginator

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    orden = request.GET.get('orden', 'recientes')
    if orden == 'creadas':
        citas = Cita.objects.filter(perfil=perfil).select_related('conversacion').order_by('-creado_en', '-fecha_hora')
    elif orden == 'proximas':
        citas = Cita.objects.filter(perfil=perfil).select_related('conversacion').order_by('fecha_hora', '-creado_en')
    else:
        citas = Cita.objects.filter(perfil=perfil).select_related('conversacion').order_by('-fecha_hora', '-creado_en')

    try:
        pagina = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        pagina = 1

    try:
        por_pagina = int(request.GET.get('per_page', 6))
    except (TypeError, ValueError):
        por_pagina = 6
    por_pagina = min(max(por_pagina, 1), 30)

    paginador = Paginator(citas, por_pagina)
    pagina_obj = paginador.get_page(pagina)

    hoy = timezone.now().date()
    manana = hoy + timezone.timedelta(days=1)
    proximos_7_dias = hoy + timezone.timedelta(days=7)

    citas_data = []
    for cita in pagina_obj.object_list:
        estado_color = 'amber'
        if cita.estado == 'confirmada':
            estado_color = 'blue'
        elif cita.estado == 'completada':
            estado_color = 'green'
        elif cita.estado == 'cancelada':
            estado_color = 'red'

        conversacion = cita.conversacion
        contacto_nombre, contacto_numero = _datos_contacto_cita(conversacion)
        contexto_reunion = _contexto_reunion_cita(cita)
        fecha_hora_local = timezone.localtime(cita.fecha_hora)

        citas_data.append({
            'id': cita.id,
            'titulo': cita.titulo,
            'descripcion': cita.descripcion,
            'contexto_reunion': contexto_reunion,
            'contacto_nombre': contacto_nombre or 'Sin nombre',
            'contacto_numero': contacto_numero or 'Sin telefono',
            'fecha_hora': fecha_hora_local.isoformat(),
            'fecha_hora_formatted': fecha_hora_local.strftime('%d/%m %H:%M'),
            'duracion_minutos': cita.duracion_minutos,
            'ubicacion': cita.ubicacion,
            'tipo_ubicacion': cita.tipo_ubicacion,
            'estado': cita.estado,
            'estado_display': cita.get_estado_display(),
            'estado_color': estado_color,
            'creado_en': cita.creado_en.isoformat(),
            'categoria': _clasificar_cita_por_fecha(fecha_hora_local, hoy, manana, proximos_7_dias),
        })

    citas_lista = list(citas)
    total = len(citas_lista)
    pendientes = sum(1 for cita in citas_lista if cita.estado == 'pendiente')
    confirmadas = sum(1 for cita in citas_lista if cita.estado == 'confirmada')
    hoy_count = sum(1 for cita in citas_lista if timezone.localtime(cita.fecha_hora).date() == hoy)
    proximas_count = sum(
        1 for cita in citas_lista
        if hoy < timezone.localtime(cita.fecha_hora).date() <= proximos_7_dias
    )

    return JsonResponse({
        'citas': citas_data,
        'resumen': {
            'total': total,
            'pendientes': pendientes,
            'confirmadas': confirmadas,
            'hoy': hoy_count,
            'proximas': proximas_count,
        },
        'paginacion': {
            'pagina': pagina_obj.number,
            'por_pagina': por_pagina,
            'total': total,
            'total_paginas': paginador.num_pages,
            'tiene_anterior': pagina_obj.has_previous(),
            'tiene_siguiente': pagina_obj.has_next(),
            'anterior': pagina_obj.previous_page_number() if pagina_obj.has_previous() else None,
            'siguiente': pagina_obj.next_page_number() if pagina_obj.has_next() else None,
        },
    })


def _datos_contacto_cita(conversacion):
    """Devuelve nombre y telefono legibles para mostrar en la agenda."""
    if not conversacion:
        return '', ''

    nombre = conversacion.nombre_contacto or ''
    numero = numero_whatsapp_visible(conversacion.numero_whatsapp)

    if not numero and nombre:
        alternativa = Conversacion.objects.filter(
            perfil=conversacion.perfil,
            nombre_contacto=nombre,
        ).exclude(id=conversacion.id).exclude(
            numero_whatsapp__startswith='web:'
        ).exclude(
            numero_whatsapp__startswith='desktop:'
        ).order_by('-creada_en').first()
        if alternativa:
            numero = numero_whatsapp_visible(alternativa.numero_whatsapp)

    return nombre, numero


def _limpiar_texto_contexto_cita(texto):
    texto = re.sub(r'\s+', ' ', texto or '').strip()
    texto = re.sub(r'https?://\S+', '', texto).strip()
    return texto


def _contexto_reunion_cita(cita):
    """Construye un contexto breve usando lo hablado con el cliente."""
    descripcion = _limpiar_texto_contexto_cita(cita.descripcion)
    if descripcion and descripcion.lower() not in ('cita', 'cita agendada', 'reunion', 'reunión'):
        base = descripcion
    else:
        base = ''

    mensajes_cliente = []
    if cita.conversacion:
        qs = cita.conversacion.mensajes.filter(origen='entrante').order_by('-creado_en')[:8]
        for mensaje in qs:
            texto = _limpiar_texto_contexto_cita(mensaje.contenido)
            if not texto or texto == '[Audio]':
                continue
            if len(texto) > 180:
                texto = texto[:177].rsplit(' ', 1)[0] + '...'
            if texto.lower() not in {item.lower() for item in mensajes_cliente}:
                mensajes_cliente.append(texto)

    piezas = []
    if base:
        piezas.append(base)
    piezas.extend(reversed(mensajes_cliente[-4:]))

    if not piezas:
        return cita.titulo or 'Reunion agendada'

    contexto = ' | '.join(piezas)
    if len(contexto) > 360:
        contexto = contexto[:357].rsplit(' ', 1)[0] + '...'
    return contexto


def _clasificar_cita_por_fecha(fecha_cita, hoy, manana, proximos_7_dias):
    """Clasifica una cita según su fecha."""
    fecha = fecha_cita.date() if hasattr(fecha_cita, 'date') else fecha_cita

    if fecha == hoy:
        return 'hoy'
    elif fecha == manana:
        return 'manana'
    elif fecha <= proximos_7_dias:
        return 'esta_semana'
    elif fecha > proximos_7_dias:
        return 'futuro'
    else:
        return 'pasado'


@csrf_exempt
@require_http_methods(["GET"])
def cita_detalle(request, cita_id):
    """API para obtener detalles de una cita específica."""
    from asistente.models import Cita

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    try:
        cita = Cita.objects.get(id=cita_id, perfil=perfil)
    except Cita.DoesNotExist:
        return JsonResponse({'error': 'Cita no encontrada'}, status=404)

    fecha_hora_local = timezone.localtime(cita.fecha_hora)

    return JsonResponse({
        'id': cita.id,
        'titulo': cita.titulo,
        'descripcion': cita.descripcion,
        'fecha_hora': fecha_hora_local.isoformat(),
        'fecha_hora_formatted': fecha_hora_local.strftime('%d/%m/%Y %H:%M'),
        'duracion_minutos': cita.duracion_minutos,
        'ubicacion': cita.ubicacion,
        'tipo_ubicacion': cita.tipo_ubicacion,
        'estado': cita.estado,
        'estado_display': cita.get_estado_display(),
        'recordatorio_enviado': cita.recordatorio_enviado,
        'recordatorio_minutos_antes': cita.recordatorio_minutos_antes,
        'conversacion_id': cita.conversacion.id if cita.conversacion else None,
        'creado_en': cita.creado_en.isoformat(),
        'actualizado_en': cita.actualizado_en.isoformat(),
    })


@csrf_exempt
@require_http_methods(["POST"])
def cita_cancelar(request, cita_id):
    """API para cancelar una cita."""
    from asistente.models import Cita

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    try:
        cita = Cita.objects.get(id=cita_id, perfil=perfil)
    except Cita.DoesNotExist:
        return JsonResponse({'error': 'Cita no encontrada'}, status=404)

    cita.marcar_cancelada()

    return JsonResponse({
        'success': True,
        'mensaje': f'Cita "{cita.titulo}" cancelada',
        'cita_id': cita.id,
    })


@csrf_exempt
@require_http_methods(["POST"])
def cita_confirmar(request, cita_id):
    """API para confirmar una cita."""
    from asistente.models import Cita

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    try:
        cita = Cita.objects.get(id=cita_id, perfil=perfil)
    except Cita.DoesNotExist:
        return JsonResponse({'error': 'Cita no encontrada'}, status=404)

    cita.marcar_confirmada()

    return JsonResponse({
        'success': True,
        'mensaje': f'Cita "{cita.titulo}" confirmada',
        'cita_id': cita.id,
    })


@csrf_exempt
@require_http_methods(["POST"])
def cita_completar(request, cita_id):
    """API para marcar una cita como completada."""
    from asistente.models import Cita

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    try:
        cita = Cita.objects.get(id=cita_id, perfil=perfil)
    except Cita.DoesNotExist:
        return JsonResponse({'error': 'Cita no encontrada'}, status=404)

    cita.marcar_completada()

    return JsonResponse({
        'success': True,
        'mensaje': f'Cita "{cita.titulo}" marcada como completada',
        'cita_id': cita.id,
    })


@csrf_exempt
@require_http_methods(["GET"])
def citas_horarios_disponibles(request):
    """API para consultar horarios disponibles de un día específico."""
    from datetime import datetime
    from .services import CitaService

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    # Obtener parámetros
    fecha_str = request.GET.get('fecha')
    duracion = int(request.GET.get('duracion', 60))
    hora_inicio = request.GET.get('hora_inicio', '08:00')
    hora_fin = request.GET.get('hora_fin', '18:00')

    if not fecha_str:
        return JsonResponse({'error': 'Se requiere el parámetro fecha (formato: YYYY-MM-DD)'}, status=400)

    try:
        fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Formato de fecha inválido. Use YYYY-MM-DD'}, status=400)

    cita_service = CitaService()
    horarios = cita_service.calcular_horarios_disponibles(
        perfil=perfil,
        fecha=fecha,
        duracion_minutos=duracion,
        hora_inicio=hora_inicio,
        hora_fin=hora_fin,
    )

    return JsonResponse({
        'fecha': fecha_str,
        'duracion_minutos': duracion,
        'horarios_disponibles': [h.strftime('%Y-%m-%d %H:%M') for h in horarios],
        'total': len(horarios),
        'texto_formateado': cita_service.formatear_horarios_disponibles(horarios),
    })


@csrf_exempt
@require_http_methods(["POST"])
def test_cita_detectar(request):
    """Endpoint de prueba para detectar intenciones de agendamiento."""
    from .services import CitaService

    perfil = obtener_perfil_usuario(request)
    if not perfil:
        return JsonResponse({'error': 'No autenticado'}, status=401)

    try:
        data = json.loads(request.body)
        mensaje = data.get('mensaje', '')
    except:
        return JsonResponse({'error': 'JSON inválido'}, status=400)

    cita_service = CitaService()

    # Detectar intención
    intencion = cita_service.detectar_intencion_agendamiento(mensaje)

    # Extraer datos si hay intención
    datos_cita = None
    if intencion:
        datos_cita = cita_service.extraer_datos_cita(mensaje, perfil)

    return JsonResponse({
        'mensaje': mensaje,
        'intencion_detectada': intencion,
        'datos_cita': datos_cita,
    })
