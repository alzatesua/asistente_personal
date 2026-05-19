import json
import os
import hashlib
import subprocess
import time
import threading
import shutil
import base64
import requests
import random
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from .models import PerfilAsistente, Conversacion, Mensaje, ComandoEjecutado, TareaProgramada
from .services import GLMService, ComandoService, TTSService, DeveloperTools, PCActionService, BackgroundTaskManager, WebResearchService, SchedulerService
from audio_visual_state import notify_audio_start, notify_audio_stop
import PyPDF2
import io


BAILEYS_START_TIMEOUT_SECONDS = 8
WHATSAPP_NOTIFICACION_MAX_CHARS = 260
WHATSAPP_LINE_DEFAULTS = {
    'responder_chats': True,
    'responder_grupos': True,
    'leer_chats': True,
    'leer_grupos': True,
    'responder_voz': False,
}


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


def listar_lineas_whatsapp_guardadas(perfil=None):
    lineas = set()

    sessions_root = baileys_sessions_root()
    if sessions_root.exists():
        for carpeta in sessions_root.iterdir():
            if not carpeta.is_dir():
                continue
            nombre = carpeta.name
            if '.backup-' in nombre or nombre.endswith('.bak'):
                continue
            if (carpeta / 'creds.json').exists():
                lineas.add(normalizar_linea_whatsapp(nombre))

    if perfil:
        prefijos_excluidos = ('web:', 'desktop:')
        numeros = Conversacion.objects.filter(perfil=perfil).values_list('numero_whatsapp', flat=True)
        for numero in numeros:
            numero = numero or ''
            if ':' not in numero or numero.startswith(prefijos_excluidos):
                continue
            linea = numero.split(':', 1)[0]
            lineas.add(normalizar_linea_whatsapp(linea))

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
    try:
        if archivo.name.endswith('.pdf'):
            reader = PyPDF2.PdfReader(io.BytesIO(archivo.read()))
            texto = ""
            for page in reader.pages:
                texto += page.extract_text()
            return texto
        else:
            return archivo.read().decode('utf-8', errors='ignore')
    except Exception as e:
        return f"No se pudo extraer el texto: {str(e)}"


@require_http_methods(["GET", "POST"])
def login_usuario(request):
    if request.user.is_authenticated and request.user.is_active:
        next_url = request.GET.get('next') or settings.LOGIN_REDIRECT_URL
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            next_url = settings.LOGIN_REDIRECT_URL
        return redirect(next_url)

    usuario_activo = User.objects.filter(is_active=True).order_by('id').first()
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

        if not usuario_activo:
            messages.error(request, 'Cuenta suspendida por pago pendiente. Contacta al administrador para reactivar el acceso.')
        elif username != usuario_activo.username:
            messages.error(request, 'Usuario o contrasena incorrectos.')
        else:
            user = authenticate(request, username=username, password=password)
            if user is None:
                messages.error(request, 'Usuario o contrasena incorrectos.')
            elif not user.is_active:
                messages.error(request, 'Cuenta suspendida por pago pendiente. Contacta al administrador para reactivar el acceso.')
            else:
                login(request, user)
                return redirect(next_url)

    if request.GET.get('inactive'):
        messages.error(request, 'Cuenta suspendida por pago pendiente. Contacta al administrador para reactivar el acceso.')

    return render(request, 'asistente/login.html', {
        'next': request.GET.get('next', ''),
        'username': usuario_activo.username if usuario_activo else '',
        'cuenta_activa': bool(usuario_activo),
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
    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
    conversaciones = []
    if perfil:
        conversaciones = Conversacion.objects.filter(perfil=perfil).order_by('-creada_en')[:20]
    return render(request, 'asistente/dashboard.html', {
        'perfil': perfil,
        'conversaciones': conversaciones,
    })


def chat_page(request):
    perfil = PerfilAsistente.objects.first()
    return render(request, 'asistente/chat.html', {'perfil': perfil})


def tareas_page(request):
    perfil = PerfilAsistente.objects.first()
    return render(request, 'asistente/tareas.html', {'perfil': perfil})


def whatsapp_page(request):
    perfil = PerfilAsistente.objects.first()
    return render(request, 'asistente/whatsapp.html', {'perfil': perfil})


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_conectar_linea(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    linea = normalizar_linea_whatsapp(data.get('linea', 'principal') or 'principal')
    iniciado, mensaje = iniciar_baileys_si_hace_falta()
    if not iniciado:
        return JsonResponse({'error': mensaje}, status=503)

    try:
        response, payload = pedir_conexion_baileys(linea, timeout=10)
    except requests.RequestException as exc:
        try:
            estado = pedir_estado_baileys(linea, timeout=2)
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

    estado = esperar_qr_o_conexion_baileys(linea)
    if estado.get('hasQR') or estado.get('status') == 'connected':
        payload.update(estado)
    else:
        try:
            borrar_sesion_baileys_remota(linea)
            response, payload = pedir_conexion_baileys(linea)
            if response.status_code >= 400:
                detalle = payload.get('error') or payload.get('raw_response') or response.reason
                return JsonResponse(
                    {'error': f'Baileys rechazo la conexion de la linea: {detalle}'},
                    status=502,
                )
            estado = esperar_qr_o_conexion_baileys(linea)
            payload.update(estado)
            payload['session_reset'] = True
        except requests.RequestException as exc:
            payload['reset_error'] = str(exc)

    payload['service_message'] = mensaje
    return JsonResponse(payload, status=response.status_code)


@require_http_methods(["GET"])
def whatsapp_sesiones(request):
    perfil = PerfilAsistente.objects.first()
    lineas = listar_lineas_whatsapp_guardadas(perfil)
    sesiones = []

    for linea in lineas:
        estado = {'status': 'offline', 'hasQR': False}
        if baileys_esta_activo():
            try:
                estado = pedir_estado_baileys(linea, timeout=2)
            except requests.RequestException:
                estado = {'status': 'offline', 'hasQR': False}

        sesiones.append({
            'linea': linea,
            'tiene_sesion': linea_tiene_sesion_baileys(linea),
            'config': obtener_config_linea(linea),
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

    perfil = PerfilAsistente.objects.first()
    lineas_payload = data.get('lineas')
    if isinstance(lineas_payload, list):
        lineas = [normalizar_linea_whatsapp(linea) for linea in lineas_payload if str(linea).strip()]
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
        try:
            estado_actual = pedir_estado_baileys(linea, timeout=2)
            if estado_actual.get('status') == 'connected':
                resultados.append({'linea': linea, 'ok': True, **estado_actual})
                continue

            response, payload = pedir_conexion_baileys(linea, timeout=4)
            if response.status_code >= 400:
                detalle = payload.get('error') or payload.get('raw_response') or response.reason
                resultados.append({'linea': linea, 'ok': False, 'error': detalle})
                continue

            estado = esperar_qr_o_conexion_baileys(linea, segundos=4)
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
    linea = normalizar_linea_whatsapp(linea)
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
    linea = normalizar_linea_whatsapp(linea)
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
    linea = normalizar_linea_whatsapp(linea)
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

    perfil = PerfilAsistente.objects.first()
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    linea = (data.get('linea') or '').strip()
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
    if request.method == 'GET':
        lineas_param = request.GET.get('lineas', '')
        if lineas_param.strip():
            lineas = [
                normalizar_linea_whatsapp(linea)
                for linea in lineas_param.split(',')
                if linea.strip()
            ]
        else:
            lineas = sorted(cargar_config_whatsapp().keys()) or ['principal']
        return JsonResponse({
            'lineas': {linea: obtener_config_linea(linea) for linea in lineas}
        })

    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'error': 'JSON invalido'}, status=400)

    linea = normalizar_linea_whatsapp(data.get('linea'))
    config = cargar_config_whatsapp()
    actual = obtener_config_linea(linea)

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

    config[linea] = {
        'responder_chats': actual['responder_chats'],
        'responder_grupos': actual['responder_grupos'],
        'leer_chats': actual['leer_chats'],
        'leer_grupos': actual['leer_grupos'],
        'responder_voz': actual['responder_voz'],
    }
    guardar_config_whatsapp(config)
    return JsonResponse({'ok': True, 'config': actual})


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_borrar_sesion(request):
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        data = {}

    linea = normalizar_linea_whatsapp(data.get('linea'))
    respuesta_baileys = None

    if baileys_esta_activo():
        try:
            response = requests.post(
                f"{baileys_service_url()}/delete-session/{linea}",
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

    borrada_local, carpeta = borrar_sesion_baileys_local(linea)
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

    linea = normalizar_linea_whatsapp(data.get('linea'))
    perfil = PerfilAsistente.objects.first()
    respuesta_baileys = None
    error_baileys = None

    if baileys_esta_activo():
        try:
            response = requests.post(
                f"{baileys_service_url()}/delete-session/{linea}",
                timeout=10,
            )
            respuesta_baileys = respuesta_json_o_texto(response) if response.content else {}
            if response.status_code >= 400:
                error_baileys = respuesta_baileys.get('error') or 'Baileys no pudo borrar la sesion'
        except requests.RequestException as exc:
            error_baileys = str(exc)
            print(f"[WhatsApp Linea] Baileys no respondio al eliminar linea: {exc}")

    borrada_local, carpeta = borrar_sesion_baileys_local(linea)
    config_eliminada = eliminar_config_linea_whatsapp(linea)

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

    perfil = PerfilAsistente.objects.first()
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    linea = normalizar_linea_whatsapp(data.get('linea') or 'principal')
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
            programado_para = datetime.strptime(programado_para_str, '%Y-%m-%dT%H:%M')
        elif ' ' in programado_para_str:
            programado_para = datetime.strptime(programado_para_str, '%Y-%m-%d %H:%M')
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
        'linea': linea,
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


def voz_page(request):
    perfil = PerfilAsistente.objects.first()
    return render(request, 'asistente/voz.html', {'perfil': perfil})


def configurar_perfil(request):
    if request.method == 'POST':
        nombre_usuario = request.POST.get('nombre_usuario')
        nombre_asistente = request.POST.get('nombre_asistente')
        cv_archivo = request.FILES.get('cv_archivo')
        modo_dev = request.POST.get('modo_desarrollador') == 'on'

        perfil, _ = PerfilAsistente.objects.get_or_create(id=1)
        perfil.nombre_usuario = nombre_usuario
        perfil.nombre_asistente = nombre_asistente

        if cv_archivo:
            perfil.cv_archivo = cv_archivo
            perfil.cv_texto = extraer_texto_cv(cv_archivo)

        perfil.save()
        return redirect('dashboard')

    perfil = PerfilAsistente.objects.first()
    return render(request, 'asistente/configurar.html', {'perfil': perfil})


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

    perfil = PerfilAsistente.objects.first()
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    numero = data.get('numero', '')
    linea = data.get('linea', 'principal') or 'principal'
    linea_numero = data.get('linea_numero', '')
    contenido = data.get('mensaje', '')
    tipo = data.get('tipo', 'texto')
    nombre_contacto = data.get('nombre', '')
    es_grupo = bool(data.get('es_grupo', False))
    remitente_grupo = data.get('remitente_grupo', '')
    audio_base64 = data.get('audio_base64')
    audio_mimetype = data.get('audio_mimetype', 'audio/ogg')

    if tipo == 'voz' or contenido == '[Audio]':
        transcripcion = transcribir_audio_whatsapp(audio_base64, audio_mimetype)
        if transcripcion:
            contenido = transcripcion
        else:
            print(
                "[WhatsApp Voz] No se pudo transcribir; "
                f"audio_base64={'si' if audio_base64 else 'no'}, "
                f"mimetype={audio_mimetype or 'sin mimetype'}"
            )

    conversacion_numero = f"{linea}:{numero}"[:80]

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
    if debe_leer_whatsapp(linea, es_grupo):
        anunciar_mensaje_whatsapp(perfil.id, nombre_contacto, numero, contenido, tipo)

    if not debe_responder_whatsapp(linea, es_grupo):
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
            },
        )
        respuesta_texto = limpiar_respuesta_whatsapp(respuesta_texto, perfil)

    audio_url = None
    if debe_responder_voz_whatsapp(linea):
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
        'audio_recibido': bool(audio_base64),
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

    perfil = PerfilAsistente.objects.first()
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


def enviar_whatsapp_masivo(parametros):
    numeros = []
    vistos = set()
    for numero in parametros.get('numeros') or []:
        limpio = limpiar_numero_whatsapp(numero)
        if limpio and limpio not in vistos:
            numeros.append(limpio)
            vistos.add(limpio)

    mensaje = (parametros.get('mensaje') or '').strip()
    linea = (parametros.get('linea') or 'yo').strip() or 'yo'
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
                json={'numero': numero, 'mensaje': mensaje, 'linea': linea},
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


def procesar_comando_desarrollo(mensaje, perfil):
    """Procesa comandos de desarrollo que empiezan con /"""
    dev_tools = DeveloperTools(working_dir=settings.BASE_DIR)
    pc_tools = PCActionService(working_dir=os.path.expanduser("~"))

    partes = mensaje.split()
    comando = partes[0].lower()
    args = partes[1:] if len(partes) > 1 else []

    try:
        if comando == "/whatsapp_masivo":
            if not args:
                return (
                    "Uso: /whatsapp_masivo linea=yo;numeros=573001234567,573009876543;"
                    "mensaje=Texto exacto a enviar"
                )
            try:
                parametros = extraer_parametros_whatsapp_masivo(" ".join(args))
            except Exception as exc:
                return f"No pude leer la lista o el mensaje del envío: {exc}"
            return enviar_whatsapp_masivo(parametros)

        # Comandos de Git
        if comando == "/git":
            if not args:
                exito, resultado = dev_tools.git_status()
            elif args[0] == "init":
                exito, resultado = dev_tools.git_init()
            elif args[0] == "clone" and len(args) > 1:
                exito, resultado = dev_tools.git_clone(args[1], args[2] if len(args) > 2 else None)
            elif args[0] == "add":
                exito, resultado = dev_tools.git_add(args[1] if len(args) > 1 else ".")
            elif args[0] == "commit" and len(args) > 1:
                exito, resultado = dev_tools.git_commit(" ".join(args[1:]))
            elif args[0] == "push":
                exito, resultado = dev_tools.git_push(args[1] if len(args) > 1 else "origin", args[2] if len(args) > 2 else "main")
            elif args[0] == "pull":
                exito, resultado = dev_tools.git_pull()
            elif args[0] == "branch":
                exito, resultado = dev_tools.git_branch(args[1] if len(args) > 1 else None, crear=len(args) > 1)
            else:
                return "Comando git no reconocido. Opciones: init, clone, add, commit, push, pull, branch"
            return respuesta_comando(exito, resultado, mensaje)

        # Comandos de Docker
        elif comando == "/docker":
            if not args or args[0] == "ps":
                exito, resultado = dev_tools.docker_ps()
            elif args[0] == "build":
                exito, resultado = dev_tools.docker_build(args[1] if len(args) > 1 else "latest")
            elif args[0] == "up":
                exito, resultado = dev_tools.docker_up(args[1] if len(args) > 1 else "docker-compose.yml")
            elif args[0] == "down":
                exito, resultado = dev_tools.docker_down(args[1] if len(args) > 1 else "docker-compose.yml")
            else:
                return "Comando docker no reconocido. Opciones: ps, build, up, down"
            return respuesta_comando(exito, resultado, mensaje)

        # Terminal libre en modo desarrollador
        elif comando == "/cmd":
            if not args:
                return "Uso: /cmd <comando>. Ejemplo: /cmd python manage.py check"
            comando_terminal = " ".join(args)
            cmd_service = ComandoService(modo_desarrollador=True)
            exito, resultado = cmd_service.ejecutar(
                comando_terminal,
                timeout=120,
                working_dir=settings.BASE_DIR,
            )
            return respuesta_comando(exito, resultado or "Comando ejecutado sin salida.", mensaje)

        # Comandos de proyectos
        elif comando == "/crear":
            if not args:
                return "Uso: /crear <tipo> <nombre>. Tipos: django, fastapi, flask, react, vue, next"
            tipo = args[0].lower()
            nombre = args[1] if len(args) > 1 else "mi-proyecto"

            if tipo == "django":
                exito, resultado = dev_tools.crear_django(nombre)
            elif tipo == "fastapi":
                exito, resultado = dev_tools.crear_fastapi(nombre)
            elif tipo == "flask":
                exito, resultado = dev_tools.crear_flask(nombre)
            elif tipo == "react":
                exito, resultado = dev_tools.crear_react(nombre)
            elif tipo == "vue":
                exito, resultado = dev_tools.crear_vue(nombre)
            elif tipo == "next":
                exito, resultado = dev_tools.crear_next(nombre)
            else:
                return f"Tipo de proyecto no reconocido: {tipo}. Opciones: django, fastapi, flask, react, vue, next"
            return respuesta_comando(exito, resultado, mensaje)

        # Comandos de aplicaciones
        elif comando == "/abrir":
            if not args:
                return "Uso: /abrir <aplicacion|url>. Ejemplos: /abrir code, /abrir firefox https://google.com"
            if args[0] == "code" or args[0] == "vscode":
                ruta = args[1] if len(args) > 1 else "."
                exito, resultado = dev_tools.abrir_vscode(ruta)
            elif args[0].startswith("http"):
                exito, resultado = dev_tools.abrir_navegador(args[0])
            else:
                exito, resultado = dev_tools.abrir_aplicacion(args[0])
            return respuesta_comando(exito, resultado, mensaje)

        # Comandos de archivos
        elif comando == "/leer":
            if not args:
                return "Uso: /leer <archivo>"
            exito, resultado = dev_tools.leer_archivo(args[0])
            return respuesta_comando(exito, resultado, mensaje)

        elif comando == "/escribir":
            if len(args) < 2:
                return "Uso: /escribir <archivo> <contenido>"
            archivo = args[0]
            contenido = " ".join(args[1:])
            exito, resultado = dev_tools.escribir_archivo(archivo, contenido)
            return respuesta_comando(exito, resultado, mensaje)

        elif comando == "/ls":
            patron = args[0] if args else "*"
            exito, resultado = dev_tools.listar_archivos(".", patron)
            return respuesta_comando(exito, resultado, mensaje)

        # Información del sistema
        elif comando == "/sys":
            exito, resultado = dev_tools.obtener_info_sistema()
            return resultado

        # Investigación web
        elif comando == "/web":
            if not args:
                return "Uso: /web <consulta>. Ejemplo: /web instalar docker ubuntu 24.04"
            consulta = " ".join(args)
            try:
                return procesar_investigacion_web(consulta, perfil=perfil)
            except Exception as e:
                return f"No pude consultar internet en este momento: {str(e)}"

        # Acciones generales del PC
        elif comando == "/pc":
            if not args:
                return "Uso: /pc <accion> [parametros]. Escriba /help para ver acciones disponibles."

            if ":" in args[0]:
                accion, parametro = args[0].split(":", 1)
                args = [accion, parametro, *args[1:]]

            accion = args[0].lower().replace("-", "_")
            parametros = args[1:]
            confirmado = any(p.lower() in ("confirmar", "--confirmar", "confirmado", "si", "sí") for p in parametros)
            parametros = [p for p in parametros if p.lower() not in ("confirmar", "--confirmar", "confirmado", "si", "sí")]

            aliases = {
                "url": "abrir_url",
                "web": "abrir_url",
                "abrir_web": "abrir_url",
                "internet": "buscar",
                "buscar_web": "buscar",
                "terminal": "abrir_terminal",
                "consola": "abrir_terminal",
                "carpeta": "abrir_carpeta",
                "archivos": "abrir_carpeta",
                "cerrar": "cerrar_app",
                "cerrar_browser": "cerrar_navegador",
                "lock": "bloquear",
                "bloquea": "bloquear",
                "suspende": "suspender",
                "sleep": "suspender",
                "shutdown": "apagar",
                "restart": "reiniciar",
                "reboot": "reiniciar",
                "diagnosticar": "diagnostico",
                "diagnóstico": "diagnostico",
                "reparar": "diagnostico",
            }
            accion = aliases.get(accion, accion)

            if pc_tools.requiere_confirmacion(accion) and not confirmado:
                parametros_texto = " ".join(parametros)
                comando_confirmacion = f"/pc {accion} {parametros_texto} confirmar".replace("  ", " ").strip()
                return (
                    f"La acción '{accion}' puede cerrar programas, pausar la sesión o interrumpir trabajo en curso.\n"
                    f"Para ejecutarla, repita: {comando_confirmacion}"
                )

            if accion == "abrir_url":
                if not parametros:
                    return "Uso: /pc abrir_url <url>"
                exito, resultado = pc_tools.abrir_url(" ".join(parametros))
            elif accion == "buscar":
                if not parametros:
                    return "Uso: /pc buscar <consulta>"
                exito, resultado = pc_tools.buscar_web(" ".join(parametros))
            elif accion == "abrir_terminal":
                ruta = os.path.expanduser(" ".join(parametros)) if parametros else None
                exito, resultado = pc_tools.abrir_terminal(ruta)
            elif accion == "abrir_carpeta":
                ruta = os.path.expanduser(" ".join(parametros)) if parametros else "."
                exito, resultado = pc_tools.abrir_carpeta(ruta)
            elif accion == "cerrar_app":
                if not parametros:
                    return "Uso: /pc cerrar_app <nombre>"
                exito, resultado = pc_tools.cerrar_app(" ".join(parametros))
            elif accion == "cerrar_navegador":
                navegador = parametros[0] if parametros else "firefox"
                exito, resultado = pc_tools.cerrar_navegador(navegador)
            elif accion == "bloquear":
                exito, resultado = pc_tools.bloquear()
            elif accion == "suspender":
                exito, resultado = pc_tools.suspender()
            elif accion == "hibernar":
                exito, resultado = pc_tools.hibernar()
            elif accion == "apagar":
                exito, resultado = pc_tools.apagar()
            elif accion == "reiniciar":
                exito, resultado = pc_tools.reiniciar()
            elif accion == "diagnostico":
                exito, resultado = pc_tools.diagnostico()
            elif accion == "errores":
                exito, resultado = pc_tools.errores_recientes()
            else:
                return (
                    f"Acción de PC no reconocida: {accion}. Opciones: abrir_url, buscar, abrir_terminal, "
                    "abrir_carpeta, cerrar_app, cerrar_navegador, bloquear, suspender, hibernar, "
                    "apagar, reiniciar, diagnostico, errores."
                )

            return respuesta_comando(exito, resultado, mensaje)

        # Ayuda
        elif comando == "/help" or comando == "/ayuda":
            return """Comandos disponibles:

Git:
  /git init              - Inicializa repositorio
  /git clone <url>       - Clona repositorio
  /git status            - Estado del repositorio
  /git add [archivos]    - Agrega archivos (default: todos)
  /git commit <msg>      - Crea commit
  /git push [remoto] [rama] - Hace push
  /git pull              - Hace pull
  /git branch [nombre]   - Lista o crea rama

Docker:
  /docker ps             - Lista contenedores
  /docker build [tag]    - Construye imagen
  /docker up [compose]   - Levanta servicios
  /docker down [compose] - Detiene servicios

Proyectos:
  /crear django <nombre>     - Crea proyecto Django
  /crear fastapi <nombre>    - Crea proyecto FastAPI
  /crear flask <nombre>      - Crea proyecto Flask
  /crear react <nombre>      - Crea proyecto React
  /crear vue <nombre>        - Crea proyecto Vue
  /crear next <nombre>       - Crea proyecto Next.js

Aplicaciones:
  /abrir code [ruta]         - Abre VS Code
  /abrir <app>               - Abre aplicación
  /abrir <url>               - Abre URL en navegador

PC:
  /pc abrir_url <url>              - Abre una página web
  /pc buscar <consulta>            - Busca en Google
  /pc abrir_terminal [ruta]        - Abre una terminal gráfica
  /pc abrir_carpeta [ruta]         - Abre el explorador de archivos
  /pc cerrar_app <nombre>          - Cierra una aplicación por nombre
  /pc cerrar_navegador [nav]       - Cierra Firefox/Chrome/Chromium/etc.
  /pc bloquear                     - Bloquea la sesión
  /pc suspender confirmar          - Suspende el PC
  /pc apagar confirmar             - Apaga el PC
  /pc reiniciar confirmar          - Reinicia el PC
  /pc diagnostico                  - Revisa disco, memoria, carga y servicios
  /pc errores                      - Muestra errores recientes del sistema

Archivos:
  /leer <archivo>            - Lee archivo
  /escribir <file> <content> - Escribe archivo
  /ls [patrón]               - Lista archivos

Sistema:
  /sys                       - Info del sistema
  /web <consulta>            - Investiga en internet con contexto del sistema
  /help                      - Muestra esta ayuda"""

        else:
            return f"Comando no reconocido: {comando}. Escribe /help para ver comandos disponibles."

    except Exception as e:
        return f"Error ejecutando comando: {str(e)}"


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
    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
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
            json={'numero': numero, 'mensaje': mensaje, 'linea': linea},
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
    perfil = PerfilAsistente.objects.first()
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

    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
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

    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
    if not perfil:
        return JsonResponse({'voz': 'piper:es_ES-mls_10246-low', 'velocidad': 1.0})

    return JsonResponse({'voz': perfil.voz_preferida, 'velocidad': perfil.voz_velocidad})


# ─── TAREAS EN SEGUNDO PLANO ────────────────────────────────────
def tareas_listar(request):
    """Lista tareas recientes ejecutadas en segundo plano."""
    return JsonResponse({'tareas': BackgroundTaskManager.listar()})


def tarea_detalle(request, tarea_id):
    """Obtiene el estado y resultado de una tarea."""
    tarea = BackgroundTaskManager.obtener(tarea_id)
    if not tarea:
        return JsonResponse({'error': 'Tarea no encontrada'}, status=404)
    return JsonResponse({'tarea': tarea})


def tarea_resumen_voz(request, tarea_id):
    """Genera un resumen breve con audio para una tarea finalizada."""
    tarea = BackgroundTaskManager.obtener(tarea_id)
    if not tarea:
        return JsonResponse({'error': 'Tarea no encontrada'}, status=404)

    if tarea.get('estado') not in ('completada', 'error'):
        return JsonResponse({'error': 'La tarea aún no ha finalizado'}, status=409)

    resumen_guardado = tarea.get('resumen')
    audio_guardado = tarea.get('audio_url')
    if resumen_guardado and audio_guardado:
        return JsonResponse({'resumen': resumen_guardado, 'audio_url': audio_guardado, 'tarea': tarea})

    perfil = PerfilAsistente.objects.first()
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
    tarea = BackgroundTaskManager.obtener(tarea_id)
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

    perfil = PerfilAsistente.objects.first()
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
@csrf_exempt
@require_http_methods(["POST"])
def terminal_ejecutar(request):
    """Ejecuta un comando desde la terminal web en modo desarrollador"""
    try:
        data = json.loads(request.body)
        comando = data.get('comando', '').strip()
    except:
        return JsonResponse({'success': False, 'output': 'JSON inválido', 'command': ''}, status=400)

    if not comando:
        return JsonResponse({'success': False, 'output': 'Comando vacío', 'command': ''})

    segundo_plano = data.get('segundo_plano', False) or comando.startswith("bg ")
    if comando.startswith("bg "):
        comando = comando[3:].strip()

    if segundo_plano:
        def ejecutar_terminal_background():
            cmd_service = ComandoService(modo_desarrollador=True)
            exitoso, resultado = cmd_service.ejecutar(comando, timeout=3600)
            prefijo = "Comando completado" if exitoso else "Comando terminó con error"
            return f"{prefijo}: {comando}\n\n{resultado}"

        tarea = BackgroundTaskManager.crear(
            titulo=f"Terminal: {comando}",
            comando=comando,
            target=ejecutar_terminal_background,
        )
        return JsonResponse({
            'success': True,
            'output': f"Tarea enviada a segundo plano. ID: {tarea['id']}",
            'command': comando,
            'segundo_plano': True,
            'tarea_id': tarea['id'],
            'tarea': tarea,
        })

    # Usar modo desarrollador con acceso completo
    cmd_service = ComandoService(modo_desarrollador=True)
    exitoso, resultado = cmd_service.ejecutar(comando, timeout=60)

    return JsonResponse({
        'success': exitoso,
        'output': resultado,
        'command': comando
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def gestion_permisos(request):
    """Gestiona los permisos de comandos (ahora solo informativo)"""
    if request.method == 'GET':
        # Retornar información sobre comandos disponibles
        from .services import ComandoService
        cmd_service = ComandoService(modo_desarrollador=False)

        return JsonResponse({
            'modo_desarrollador': True,
            'comandos_basicos': ComandoService.COMANDOS_BASICOS,
            'comandos_desarrollo': ComandoService.COMANDOS_DESARROLLO,
            'comandos_sistema': ComandoService.COMANDOS_SISTEMA,
            'mensaje': 'Modo desarrollador activo - Todos los comandos están disponibles'
        })

    return JsonResponse({'success': True, 'mensaje': 'Modo desarrollador activo'})


# ─── API DEVELOPER TOOLS ───────────────────────────────────────
@csrf_exempt
@require_http_methods(["POST"])
def devtools_accion(request):
    """Ejecuta acciones de DeveloperTools"""
    try:
        data = json.loads(request.body)
        accion = data.get('accion')
        params = data.get('params', {})
    except:
        return JsonResponse({'success': False, 'error': 'JSON inválido'}, status=400)

    dev_tools = DeveloperTools(working_dir=settings.BASE_DIR)

    try:
        if accion == 'crear_django':
            exito, resultado = dev_tools.crear_django(params.get('nombre', 'mi-proyecto'))
        elif accion == 'crear_fastapi':
            exito, resultado = dev_tools.crear_fastapi(params.get('nombre', 'mi-proyecto'))
        elif accion == 'crear_flask':
            exito, resultado = dev_tools.crear_flask(params.get('nombre', 'mi-proyecto'))
        elif accion == 'crear_react':
            exito, resultado = dev_tools.crear_react(params.get('nombre', 'mi-proyecto'), params.get('typescript', False))
        elif accion == 'crear_vue':
            exito, resultado = dev_tools.crear_vue(params.get('nombre', 'mi-proyecto'))
        elif accion == 'crear_next':
            exito, resultado = dev_tools.crear_next(params.get('nombre', 'mi-proyecto'))
        elif accion == 'git_init':
            exito, resultado = dev_tools.git_init()
        elif accion == 'git_clone':
            exito, resultado = dev_tools.git_clone(params.get('url'), params.get('nombre'))
        elif accion == 'git_status':
            exito, resultado = dev_tools.git_status()
        elif accion == 'git_add':
            exito, resultado = dev_tools.git_add(params.get('archivos', '.'))
        elif accion == 'git_commit':
            exito, resultado = dev_tools.git_commit(params.get('mensaje', 'Update'))
        elif accion == 'git_push':
            exito, resultado = dev_tools.git_push(params.get('remoto', 'origin'), params.get('rama', 'main'))
        elif accion == 'git_pull':
            exito, resultado = dev_tools.git_pull()
        elif accion == 'abrir_vscode':
            exito, resultado = dev_tools.abrir_vscode(params.get('ruta', '.'))
        elif accion == 'abrir_navegador':
            exito, resultado = dev_tools.abrir_navegador(params.get('url', 'https://google.com'), params.get('navegador', 'firefox'))
        elif accion == 'docker_ps':
            exito, resultado = dev_tools.docker_ps()
        elif accion == 'docker_up':
            exito, resultado = dev_tools.docker_up(params.get('compose_file', 'docker-compose.yml'))
        elif accion == 'docker_down':
            exito, resultado = dev_tools.docker_down(params.get('compose_file', 'docker-compose.yml'))
        elif accion == 'leer_archivo':
            exito, resultado = dev_tools.leer_archivo(params.get('ruta'))
        elif accion == 'sysinfo':
            exito, resultado = dev_tools.obtener_info_sistema()
        else:
            return JsonResponse({'success': False, 'error': f'Acción no reconocida: {accion}'}, status=400)

        return JsonResponse({
            'success': exito,
            'resultado': resultado,
            'accion': accion
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


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

    perfil = PerfilAsistente.objects.first()
    if not perfil:
        return JsonResponse({'error': 'Perfil no configurado'}, status=400)

    if not titulo or not tipo_accion or not programado_para_str:
        return JsonResponse({'error': 'Faltan campos requeridos: titulo, tipo_accion, programado_para'}, status=400)

    try:
        # Parsear la fecha/hora
        from datetime import datetime, timedelta

        # Soportar formatos relativos como "+30min", "+1h", "+2dias"
        if programado_para_str.startswith('+'):
            cantidad_str = programado_para_str[1:]
            if 'min' in cantidad_str.lower():
                minutos = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = datetime.now() + timedelta(minutes=minutos)
            elif 'h' in cantidad_str.lower() or 'hr' in cantidad_str.lower():
                horas = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = datetime.now() + timedelta(hours=horas)
            elif 'dia' in cantidad_str.lower():
                dias = int(''.join(c for c in cantidad_str if c.isdigit()))
                programado_para = datetime.now() + timedelta(days=dias)
            else:
                return JsonResponse({'error': f'Formato relativo no reconocido: {programado_para_str}'}, status=400)
        else:
            # Formato: "YYYY-MM-DD HH:MM" o "HH:MM" (para hoy)
            if ' ' in programado_para_str:
                programado_para = datetime.strptime(programado_para_str, '%Y-%m-%d %H:%M')
            else:
                # Solo hora, para hoy
                hoy = datetime.now().date()
                hora = datetime.strptime(programado_para_str, '%H:%M').time()
                programado_para = datetime.combine(hoy, hora)

                # Si la hora ya pasó, asumir que es para mañana
                if programado_para <= datetime.now():
                    from datetime import timedelta
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
    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
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
    perfil = PerfilAsistente.objects.first()
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

    perfil = PerfilAsistente.objects.first()
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
