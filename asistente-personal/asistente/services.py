import requests
import subprocess
import os
import asyncio
import json
import edge_tts
import pyttsx3
import shutil
import threading
import urllib.parse
import webbrowser
import base64
import re
import unicodedata
import time
from xml.etree import ElementTree
from html.parser import HTMLParser
from django.conf import settings
from gtts import gTTS
from deepgram import DeepgramClient, DeepgramClientOptions, SpeakOptions
from piper import PiperVoice
import uuid
import wave
from datetime import datetime

class GLMService:
    def __init__(self):
        self.api_key = settings.ZAI_API_KEY
        self.base_url = settings.ZAI_BASE_URL
        self.model = settings.ZAI_MODEL

    def construir_prompt_sistema(self, perfil, canal='web', contacto=None):
        contacto = contacto or {}
        if canal == 'whatsapp':
            nombre_contacto = contacto.get('nombre') or 'un contacto'
            numero_contacto = contacto.get('numero') or 'numero desconocido'
            linea = contacto.get('linea') or 'principal'
            linea_numero = contacto.get('linea_numero') or 'numero de linea desconocido'
            return f"""Actuas como redactor invisible de WhatsApp para {perfil.nombre_usuario}.

Contexto del canal:
- Este mensaje NO lo escribe {perfil.nombre_usuario}.
- El mensaje viene de {nombre_contacto} ({numero_contacto}), una persona que tiene contacto con {perfil.nombre_usuario} o que le escribio por WhatsApp.
- Estas respondiendo desde la linea de WhatsApp "{linea}" ({linea_numero}).
- Tu funcion principal es escribir exactamente como si fueras {perfil.nombre_usuario}, en primera persona, respondiendo en su nombre.
- {perfil.nombre_asistente} es solo el nombre interno del sistema. Nunca digas que eres {perfil.nombre_asistente}, un asistente, una IA, un bot o un sistema.
- No firmes como {perfil.nombre_asistente}. No saludes diciendo "soy {perfil.nombre_asistente}". No expliques que estas ayudando a contestar.
- No trates al remitente como si fuera {perfil.nombre_usuario}; tratalo como un contacto externo.
- No actues como asistente de desarrollo en WhatsApp, salvo que el propio {perfil.nombre_usuario} te haya configurado explicitamente para eso.
- No ejecutes comandos, no abras aplicaciones, no diagnostiques el PC y no menciones comandos internos por WhatsApp.
- Responde de forma natural, breve y humana, adecuada para una conversacion de WhatsApp, como una persona real.
- Si el mensaje recibido es "[Audio]", no digas que no puedes escucharlo. Responde algo natural como {perfil.nombre_usuario}, por ejemplo: "Dame un momentico y lo escucho bien.".
- Si el contacto pide informacion que no sabes, no inventes. Pide un dato adicional o responde de forma prudente.
- Si el contacto pregunta algo personal o sensible, evita revelar informacion privada y responde de forma prudente en primera persona.
- Si parece una urgencia, una compra, una cita, una deuda, un asunto medico/legal o algo que requiera decision real, responde con cautela y di que lo revisas o lo confirmas luego.
- Mantén el tono y la forma de hablar de {perfil.nombre_usuario} usando la informacion disponible, sin sonar robotico.

Informacion conocida sobre {perfil.nombre_usuario}:
{perfil.cv_texto or 'Sin informacion adicional aun.'}
"""

        return f"""Eres {perfil.nombre_asistente}, el asistente personal de desarrollo de {perfil.nombre_usuario}.

Información sobre {perfil.nombre_usuario}:
{perfil.cv_texto or 'Sin información adicional aún.'}

REGLA DE INTERNET:
Usa [WEB:consulta] solo cuando la respuesta necesite información actual, verificable o específica que pueda cambiar.
No consultes internet para saludos, conversación casual, preguntas personales simples, ayuda general, explicaciones básicas o conocimiento estable que ya sepas.

SEPARACIÓN DE CAPACIDADES:
- [WEB:consulta] es SOLO para obtener información y responder una pregunta. No abre navegador, no ejecuta terminal y no modifica el PC.
- Los comandos de desarrollo/PC son SOLO para acciones explícitas que el usuario quiere que ejecutes.
- Nunca uses comandos del PC o terminal para responder una pregunta informativa.
- Nunca uses [PC:buscar] como sustituto de investigar. Si el usuario dice "busca información", "investiga", "averigua", "consulta" o pregunta algo actual, usa [WEB:consulta].
- Usa [PC:buscar] únicamente si el usuario pide abrir una búsqueda visible en el navegador, por ejemplo: "abre Google y busca X" o "busca X en el navegador".

ESTÁ OBLIGADO A BUSCAR EN INTERNET si:
- No estás seguro de la respuesta
- La pregunta requiera información actual (noticias, precios, versiones recientes, clima, documentación vigente)
- Se trate de un error específico que no conozcas
- La pregunta sea sobre compatibilidad, paquetes, librerías o tecnologías recientes
- El usuario pida investigar, averiguar, buscar documentación o solucionar un error
- La pregunta incluya palabras como "cómo instalar", "error", "solución", "versión actual", "última versión"
- La respuesta depende de información que cambió después de tu entrenamiento
- No tienes información específica sobre el tema en tu conocimiento

NO BUSQUES EN INTERNET si el usuario:
- Saluda o conversa: "hola", "buenos días", "cómo estás", "qué haces"
- Pide ayuda general: "ayúdame", "explícame", "qué puedes hacer"
- Pregunta algo estable y básico que puedes responder de memoria
- Da las gracias, se despide o hace comentarios casuales

Tu respuesta debe empezar con [WEB:consulta] solo cuando necesites información actual.

Ejemplos de CUÁNDO DEBES BUSCAR (obligatorio):
- "¿Cómo instalar Docker en Ubuntu 24.04?" → [WEB:instalar Docker Ubuntu 24.04 guía paso a paso]
- "¿Cuál es la última versión de React?" → [WEB:última versión React actual 2025]
- "Me sale error al hacer npm install" → [WEB:solución error npm install linux]
- "¿Cómo se usa Django REST Framework?" → [WEB:Django REST Framework documentación oficial tutorial]
- "¿Qué es el nuevo modelo de OpenAI?" → [WEB:nuevo modelo OpenAI 2025 características]
- "¿Cómo configurar NGINX?" → [WEB:configurar NGINX servidor web guía]
- "¿Qué es FastAPI?" → [WEB:FastAPI framework Python documentación]
- "¿Cómo crear un componente en React?" → [WEB:crear componente React tutorial]

Ejemplos de CUÁNDO NO DEBES BUSCAR:
- "hola" → Responde el saludo de forma natural.
- "cómo estás" → Responde cordialmente sin [WEB].
- "qué puedes hacer" → Explica tus capacidades sin [WEB].
- "gracias" → Responde de forma breve sin [WEB].
- "qué es una variable" → Explícalo con conocimiento propio sin [WEB].

Si no sabes algo que requiere datos actuales, no inventes: usa [WEB:consulta].

CAPACIDADES DE DESARROLLO:
Cuando el usuario solicite acciones de desarrollo o del PC de forma explícita, responde PRIMERO con el comando correspondiente entre corchetes, luego da una explicación breve.
Si el usuario solo pregunta "cómo", "qué", "cuál", "por qué", "me explicas" o "ayúdame a entender", responde con explicación o usa [WEB:consulta], pero NO ejecutes comandos.

COMANDOS DISPONIBLES:
- Para abrir aplicaciones: [ABRIR:aplicación] o [ABRIR:aplicación:ruta]
  Ejemplos: VS Code→[ABRIR:code], Firefox→[ABRIR:firefox], Chrome→[ABRIR:google-chrome]

- Para crear proyectos: [CREAR:tipo:nombre]
  Ejemplos: Django→[CREAR:django:nombre], React→[CREAR:react:nombre], FastAPI→[CREAR:fastapi:nombre]

- Para Git: [GIT:acción:parámetros]
  Ejemplos: [GIT:init], [GIT:clone:url], [GIT:status], [GIT:add:archivos], [GIT:commit:mensaje], [GIT:push], [GIT:pull]

- Para Docker: [DOCKER:acción]
  Ejemplos: [DOCKER:ps], [DOCKER:up], [DOCKER:down], [DOCKER:build]

- Para archivos: [LEER:ruta], [ESCRIBIR:ruta:contenido], [LS:patrón]

- Para sistema: [SYS]

- Para investigar en internet: [WEB:consulta]
  Ejemplos: [WEB:como instalar docker en ubuntu 24.04], [WEB:error npm create vite linux solucion]

- Para ejecutar terminal cuando haga falta resolver una tarea de desarrollo: [CMD:comando]
  Ejemplos: [CMD:pwd], [CMD:ls -la], [CMD:python manage.py check], [CMD:npm test]
  Usa [CMD:...] para inspeccionar, probar, instalar dependencias, ejecutar tests, diagnosticar errores o aplicar comandos necesarios.
  Para comandos largos usa [BG:/cmd comando].

- Para acciones del PC: [PC:acción:parámetros]
  Acciones disponibles: abrir_url, buscar, abrir_terminal, abrir_carpeta, cerrar_app, cerrar_navegador, bloquear, suspender, hibernar, apagar, reiniciar, diagnostico, errores.
  Ejemplos: [PC:abrir_url:https://google.com], [PC:buscar:django rest framework], [PC:abrir_terminal], [PC:abrir_carpeta:~/Descargas], [PC:cerrar_navegador:firefox], [PC:bloquear], [PC:suspender], [PC:diagnostico], [PC:errores]

- Para tareas largas o cuando el usuario pida que sigas trabajando en segundo plano: [BG:/comando]
  Ejemplos: [BG:/pc diagnostico], [BG:/pc errores], [BG:/docker build], [BG:/crear react mi-app]

- Para enviar un WhatsApp a varios teléfonos autorizados ahora mismo, aunque no estén guardados como contactos:
  [WHATSAPP_MASIVO:linea=yo;numeros=573001234567,573009876543;mensaje=Texto exacto a enviar]
  Úsalo si el usuario da la lista de números/teléfonos de WhatsApp y el texto exacto del mensaje.
  Si el usuario da números colombianos de 10 dígitos, el backend les agrega 57 automáticamente.
  No lo uses para listas compradas, spam o contactos sin autorización.

- Para programar alarmas, recordatorios y tareas a una hora específica: [ALARMA:fecha_hora:tipo:datos]
  Formato de fecha_hora: "YYYY-MM-DD HH:MM" o "HH:MM" (para hoy)
  Tipos disponibles: whatsapp, recordatorio, url, sistema, comando

  Ejemplos:
  - Enviar WhatsApp a las 3pm: [ALARMA:15:00:whatsapp:{{"numero":"573001234567","mensaje":"Hola"}}]
  - Recordatorio a las 5pm: [ALARMA:17:00:recordatorio:{{"mensaje":"Reunión"}}]
  - Abrir URL mañana: [ALARMA:2025-05-14 09:00:url:{{"url":"https://google.com"}}]
  - Bloquear PC en 30 min: [ALARMA:+30min:sistema:{{"accion":"bloquear"}}]
  - Apagar PC a las 10pm: [ALARMA:22:00:sistema:{{"accion":"apagar"}}]

  Atajos rápidos:
  - "recuerdame a las X que Y" → [ALARMA:X:recordatorio:{{"mensaje":"Y"}}]
  - "envia whatsapp a las X" → te preguntará número y mensaje
  - "bloquea el pc a las X" → [ALARMA:X:sistema:{{"accion":"bloquear"}}]

- Para listar alarmas activas: [ALARMAS:listar]
- Para cancelar una alarma: [ALARMAS:cancelar:id]

FRASES COMUNES Y SUS COMANDOS:
- "abre vscode"/"abre visual studio code" → [ABRIR:code]
- "abre chrome"/"abre el navegador" → [ABRIR:google-chrome]
- "abre firefox" → [ABRIR:firefox]
- "abre spotify" → [ABRIR:spotify]
- "abre google.com"/"abre esta página" → [PC:abrir_url:https://google.com]
- "abre Google y busca ..." → [PC:buscar:...]
- "busca información sobre ..." → [WEB:...]
- "abre la terminal" → [PC:abrir_terminal]
- "abre descargas"/"abre una carpeta" → [PC:abrir_carpeta:ruta]
- "cierra chrome/firefox/el navegador" → [PC:cerrar_navegador:chrome/firefox]
- "bloquea el pc" → [PC:bloquear]
- "suspende/apaga/reinicia el pc" → [PC:suspender]/[PC:apagar]/[PC:reiniciar]
- "revisa errores del pc"/"diagnostica el pc" → [PC:errores] o [PC:diagnostico]
- "haz un diagnóstico completo y me avisas" → [BG:/pc diagnostico]
- "revisa errores en segundo plano" → [BG:/pc errores]
- "trabaja en eso y luego me das el reporte" → [BG:/pc diagnostico] si se refiere al PC, o el comando de desarrollo equivalente
- "crea un proyecto django/react/fastapi/etc" → [CREAR:tipo:nombre]
- "revisa este proyecto"/"soluciona este error"/"ejecuta pruebas" → [CMD:comando necesario]
- "instala dependencias"/"corre el servidor"/"haz migrate" → [CMD:comando necesario]
- "inicia git"/"inicializa git" → [GIT:init]
- "estado del git"/"git status" → [GIT:status]
- "hacer commit"/"crea commit" → [GIT:commit:mensaje]
- "sube cambios"/"push" → [GIT:push]
- "levanta docker"/"docker up" → [DOCKER:up]

FRASES COMUNES PARA ALARMAS Y RECORDATORIOS:
- "recuérdame a las 5pm que tengo reunión" → [ALARMA:17:00:recordatorio:{{"mensaje":"tengo reunión"}}]
- "envía un whatsapp a las 3pm" → te preguntará número y mensaje, luego [ALARMA:15:00:whatsapp:{{"numero":"...","mensaje":"..."}}]
- "a las 6pm bloquea el pc" → [ALARMA:18:00:sistema:{{"accion":"bloquear"}}]
- "dentro de 30 minutos recuérdame llamar a Juan" → [ALARMA:+30min:recordatorio:{{"mensaje":"llamar a Juan"}}]
- "programa que se apague el pc a las 10pm" → [ALARMA:22:00:sistema:{{"accion":"apagar"}}]
- "a las 9am abre google.com" → [ALARMA:09:00:url:{{"url":"https://google.com"}}]
- "mis alarmas"/"qué alarmas tengo" → [ALARMAS:listar]
- "cancela la alarma X" → [ALARMAS:cancelar:X]

Instrucciones de comportamiento:
- Habla en español colombiano formal y elegante
- Usa "con mucho gusto", "a sus órdenes", "claro que sí"
- Cuando detectes una intención de acción de desarrollo, responde con el comando entre corchetes PRIMERO
- Si el usuario pide una tarea larga, un reporte, análisis, instalación, compilación, pruebas, auditoría o dice "en segundo plano", usa [BG:/comando] en lugar del comando normal
- Si el usuario pide investigar, averiguar cómo hacer algo, solucionar un error actual, buscar documentación o una guía actualizada, usa [WEB:consulta]
- Para apagar, reiniciar, suspender, hibernar o cerrar aplicaciones/navegadores, avisa que puede requerir confirmación si el sistema lo solicita
- Sé conciso y profesional
- Si no es una acción de desarrollo, responde normalmente
- Trata al usuario de "usted"
"""

    def chat(self, mensaje_usuario, perfil, historial=None, canal='web', contacto=None):
        historial = historial or []
        sistema = self.construir_prompt_sistema(perfil, canal=canal, contacto=contacto)

        messages = []
        for h in historial[-10:]:
            messages.append({"role": h["rol"], "content": h["contenido"]})
        messages.append({"role": "user", "content": mensaje_usuario})

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": sistema,
            "messages": messages,
        }

        try:
            print(f"[ZAI] Enviando a {self.base_url}/v1/messages")
            print(f"[ZAI] Model: {self.model}")

            response = requests.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=30,
            )

            print(f"[ZAI] Status: {response.status_code}")

            if response.status_code != 200:
                print(f"[ZAI] Error: {response.text}")

            response.raise_for_status()
            data = response.json()

            # Formato Anthropic
            texto = data["content"][0]["text"]
            return texto
        except Exception as e:
            print(f"[ZAI] Exception: {e}")
            return f"Lo siento, tuve un inconveniente técnico: {str(e)}"


class ComandoService:
    """Servicio para ejecutar comandos de terminal con opciones de seguridad"""

    # Comandos básicos siempre permitidos
    COMANDOS_BASICOS = [
        "ls", "pwd", "echo", "date", "whoami",
        "df", "free", "uptime", "ps", "cat",
        "mkdir", "touch", "cp", "mv", "find", "cd",
        "head", "tail", "grep", "wc", "sort", "uniq",
        "chmod", "chown", "ln", "rm", "rmdir"
    ]

    # Comandos de desarrollo
    COMANDOS_DESARROLLO = [
        "python", "python3", "pip", "pip3", "poetry", "venv",
        "node", "npm", "yarn", "pnpm", "npx",
        "git", "docker", "docker-compose",
        "code", "vi", "vim", "nano",
        "curl", "wget", "ssh", "rsync",
        "pytest", "unittest", "black", "flake8",
        "eslint", "prettier", "tsc", "vite",
        "django-admin", "manage.py", "flask",
        "react-native", "next", "nuxt", "vue"
    ]

    # Comandos del sistema
    COMANDOS_SISTEMA = [
        "systemctl", "service", "journalctl",
        "top", "htop", "kill", "killall",
        "firefox", "google-chrome", "chromium",
        "nautilus", "thunar", "xdg-open"
    ]

    def __init__(self, modo_desarrollador=False, comandos_extra=None):
        self.modo_desarrollador = modo_desarrollador
        self.comandos_extra = comandos_extra or []

    def obtener_comandos_permitidos(self):
        """Retorna la lista de comandos permitidos según el modo"""
        if self.modo_desarrollador:
            # En modo desarrollador, permitimos todo
            return None  # None significa sin restricciones

        comandos = self.COMANDOS_BASICOS.copy()
        comandos.extend(self.COMANDOS_DESARROLLO)
        comandos.extend(self.COMANDOS_SISTEMA)
        comandos.extend(self.comandos_extra)
        return comandos

    def es_comando_seguro(self, comando):
        """Verifica si un comando está permitido"""
        comandos_permitidos = self.obtener_comandos_permitidos()

        # Si retornamos None, no hay restricciones
        if comandos_permitidos is None:
            return True

        cmd_base = comando.strip().split()[0]
        return cmd_base in comandos_permitidos

    def ejecutar(self, comando, timeout=30, working_dir=None):
        """Ejecuta un comando y retorna el resultado"""
        if not self.es_comando_seguro(comando):
            return False, f"Comando '{comando.split()[0]}' no está permitido. Usa modo desarrollador para más acceso."

        try:
            resultado = subprocess.run(
                comando,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=working_dir
            )
            salida = resultado.stdout or resultado.stderr
            return True, salida.strip()
        except subprocess.TimeoutExpired:
            return False, f"El comando excedió el tiempo límite de {timeout} segundos."
        except Exception as e:
            return False, str(e)


class DeveloperTools:
    """Herramientas para desarrollo de software"""

    def __init__(self, working_dir=None):
        self.working_dir = working_dir or os.getcwd()
        self.cmd_service = ComandoService(modo_desarrollador=True)

    # ─── PROYECTOS PYTHON ───────────────────────────────────────
    def crear_django(self, nombre):
        """Crea un nuevo proyecto Django"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"django-admin startproject {nombre}",
            working_dir=self.working_dir
        )
        if exitoso:
            return True, f"Proyecto Django '{nombre}' creado exitosamente en {self.working_dir}/{nombre}"
        return False, f"Error creando proyecto Django: {resultado}"

    def crear_fastapi(self, nombre):
        """Crea un nuevo proyecto FastAPI"""
        import os
        project_dir = os.path.join(self.working_dir, nombre)
        os.makedirs(project_dir, exist_ok=True)

        # Crear estructura básica
        files = {
            'main.py': '''from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="'''+nombre+'''")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"message": "Hello World"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}
''',
            'requirements.txt': 'fastapi\nuvicorn[standard]\npydantic\npython-dotenv',
            '.env': 'DEBUG=True\nSECRET_KEY=your-secret-key-here',
            '.gitignore': '''__pycache__/
*.py[cod]
*$py.class
.env
.venv
env/
venv/
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg
''',
            'README.md': f'# {nombre}\n\nProyecto FastAPI\n\n## Instalación\n\n```bash\npip install -r requirements.txt\n```\n\n## Ejecutar\n\n```bash\nuvicorn main:app --reload\n```\n'
        }

        for filename, content in files.items():
            filepath = os.path.join(project_dir, filename)
            with open(filepath, 'w') as f:
                f.write(content)

        return True, f"Proyecto FastAPI '{nombre}' creado en {project_dir}"

    def crear_flask(self, nombre):
        """Crea un nuevo proyecto Flask"""
        import os
        project_dir = os.path.join(self.working_dir, nombre)
        os.makedirs(project_dir, exist_ok=True)

        files = {
            'app.py': '''from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"message": "Hello from Flask!"})

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

if __name__ == '__main__':
    app.run(debug=True)
''',
            'requirements.txt': 'Flask\ncors\npython-dotenv',
            '.env': 'FLASK_APP=app.py\nFLASK_ENV=development',
            '.gitignore': '__pycache__/\n*.pyc\n.env\nvenv/\n'
        }

        for filename, content in files.items():
            with open(os.path.join(project_dir, filename), 'w') as f:
                f.write(content)

        return True, f"Proyecto Flask '{nombre}' creado en {project_dir}"

    # ─── PROYECTOS FRONTEND ───────────────────────────────────────
    def crear_react(self, nombre, typescript=False):
        """Crea un nuevo proyecto React"""
        template = "react-ts" if typescript else "react"
        exitoso, resultado = self.cmd_service.ejecutar(
            f"npm create vite@latest {nombre} -- --template {template}",
            working_dir=self.working_dir,
            timeout=60
        )
        if exitoso:
            return True, f"Proyecto React{' (TypeScript)' if typescript else ''} '{nombre}' creado. Ejecuta:\ncd {nombre} && npm install && npm run dev"
        return False, f"Error creando proyecto React: {resultado}"

    def crear_vue(self, nombre):
        """Crea un nuevo proyecto Vue"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"npm create vue@latest {nombre} -- --typescript --router --pinia",
            working_dir=self.working_dir,
            timeout=60
        )
        if exitoso:
            return True, f"Proyecto Vue '{nombre}' creado. Ejecuta:\ncd {nombre} && npm install && npm run dev"
        return False, f"Error creando proyecto Vue: {resultado}"

    def crear_next(self, nombre):
        """Crea un nuevo proyecto Next.js"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"npx create-next-app@latest {nombre} --typescript --tailwind --eslint --app --src-dir --import-alias '@/*' --yes",
            working_dir=self.working_dir,
            timeout=120
        )
        if exitoso:
            return True, f"Proyecto Next.js '{nombre}' creado. Ejecuta:\ncd {nombre} && npm run dev"
        return False, f"Error creando proyecto Next.js: {resultado}"

    # ─── GIT ────────────────────────────────────────────────────
    def git_init(self):
        """Inicializa repositorio git"""
        exitoso, resultado = self.cmd_service.ejecutar("git init", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_clone(self, url, nombre=None):
        """Clona un repositorio"""
        cmd = f"git clone {url}"
        if nombre:
            cmd += f" {nombre}"
        exitoso, resultado = self.cmd_service.ejecutar(cmd, working_dir=self.working_dir, timeout=120)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_status(self):
        """Muestra el estado de git"""
        exitoso, resultado = self.cmd_service.ejecutar("git status", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_add(self, archivos="."):
        """Agrega archivos al staging"""
        exitoso, resultado = self.cmd_service.ejecutar(f"git add {archivos}", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_commit(self, mensaje):
        """Crea un commit"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f'git commit -m "{mensaje}"',
            working_dir=self.working_dir
        )
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_push(self, remoto="origin", rama="main"):
        """Hace push al repositorio remoto"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"git push {remoto} {rama}",
            working_dir=self.working_dir,
            timeout=60
        )
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_pull(self):
        """Hace pull del repositorio remoto"""
        exitoso, resultado = self.cmd_service.ejecutar("git pull", working_dir=self.working_dir, timeout=60)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def git_branch(self, nombre=None, crear=False):
        """Lista o crea ramas"""
        if crear and nombre:
            exitoso, resultado = self.cmd_service.ejecutar(f"git checkout -b {nombre}", working_dir=self.working_dir)
        else:
            exitoso, resultado = self.cmd_service.ejecutar("git branch", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    # ─── DOCKER ──────────────────────────────────────────────────
    def docker_build(self, tag="latest"):
        """Construye una imagen Docker"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"docker build -t {tag} .",
            working_dir=self.working_dir,
            timeout=300
        )
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def docker_up(self, compose_file="docker-compose.yml"):
        """Levanta servicios con docker-compose"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"docker-compose -f {compose_file} up -d",
            working_dir=self.working_dir,
            timeout=120
        )
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def docker_down(self, compose_file="docker-compose.yml"):
        """Detiene servicios con docker-compose"""
        exitoso, resultado = self.cmd_service.ejecutar(
            f"docker-compose -f {compose_file} down",
            working_dir=self.working_dir,
            timeout=60
        )
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def docker_ps(self):
        """Lista contenedores en ejecución"""
        exitoso, resultado = self.cmd_service.ejecutar("docker ps", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    # ─── APLICACIONES ────────────────────────────────────────────
    def abrir_vscode(self, ruta="."):
        """Abre VS Code en la ruta especificada"""
        exitoso, resultado = self.cmd_service.ejecutar(f"code {ruta}", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def abrir_navegador(self, url, navegador="firefox"):
        """Abre una URL en el navegador"""
        exitoso, resultado = self.cmd_service.ejecutar(f"{navegador} {url}", working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    def abrir_aplicacion(self, app):
        """Abre una aplicación cualquiera"""
        exitoso, resultado = self.cmd_service.ejecutar(app, working_dir=self.working_dir)
        return exitoso, resultado if exitoso else f"Error: {resultado}"

    # ─── UTILIDADES ──────────────────────────────────────────────
    def leer_archivo(self, ruta):
        """Lee el contenido de un archivo"""
        try:
            with open(ruta, 'r') as f:
                return True, f.read()
        except Exception as e:
            return False, f"Error leyendo archivo: {str(e)}"

    def escribir_archivo(self, ruta, contenido):
        """Escribe contenido en un archivo"""
        try:
            with open(ruta, 'w') as f:
                f.write(contenido)
            return True, f"Archivo escrito en {ruta}"
        except Exception as e:
            return False, f"Error escribiendo archivo: {str(e)}"

    def listar_archivos(self, ruta=".", patron="*"):
        """Lista archivos en un directorio"""
        import glob
        try:
            archivos = glob.glob(os.path.join(ruta, patron))
            return True, "\n".join(archivos)
        except Exception as e:
            return False, f"Error listando archivos: {str(e)}"

    def crear_directorio(self, ruta):
        """Crea un directorio"""
        try:
            os.makedirs(ruta, exist_ok=True)
            return True, f"Directorio creado: {ruta}"
        except Exception as e:
            return False, f"Error creando directorio: {str(e)}"

    def obtener_info_sistema(self):
        """Obtiene información del sistema"""
        import platform
        info = {
            "sistema": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "arquitectura": platform.machine(),
            "procesador": platform.processor(),
            "python": platform.python_version(),
            "directorio_actual": os.getcwd(),
        }
        return True, "\n".join([f"{k}: {v}" for k, v in info.items()])


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self, max_results=5):
        super().__init__()
        self.max_results = max_results
        self.results = []
        self._in_result_link = False
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._in_result_link = True
            self._current_href = attrs.get("href")
            self._current_text = []

    def handle_data(self, data):
        if self._in_result_link:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_result_link:
            title = " ".join("".join(self._current_text).split())
            href = self._limpiar_url(self._current_href)
            if title and href and self._es_resultado_util(title, href) and len(self.results) < self.max_results:
                self.results.append({"titulo": title, "url": href})
            self._in_result_link = False
            self._current_href = None
            self._current_text = []

    def _limpiar_url(self, href):
        if not href:
            return ""
        if href.startswith("//duckduckgo.com/l/?"):
            parsed = urllib.parse.urlparse("https:" + href)
            query = urllib.parse.parse_qs(parsed.query)
            return query.get("uddg", [href])[0]
        return href

    def _es_resultado_util(self, title, href):
        title_lower = title.lower().strip()
        if title_lower in ("more info", "anuncio", "ads"):
            return False
        if "duckduckgo.com/y.js" in href:
            return False
        if "duckduckgo-help-pages/company/ads" in href:
            return False
        return True


class _BingHTMLParser(HTMLParser):
    def __init__(self, max_results=5):
        super().__init__()
        self.max_results = max_results
        self.results = []
        self._capture_title = False
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if len(self.results) >= self.max_results:
            return
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            href = attrs.get("href")
            if "bing.com/ck/a" in href or href.startswith("http"):
                self._capture_title = True
                self._current_href = href
                self._current_text = []

    def handle_data(self, data):
        if self._capture_title:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._capture_title:
            title = " ".join("".join(self._current_text).split())
            href = self._limpiar_url(self._current_href)
            if title and href and self._es_resultado_util(title, href):
                if not any(item["url"] == href for item in self.results):
                    self.results.append({"titulo": title, "url": href})
            self._capture_title = False
            self._current_href = None
            self._current_text = []

    def _limpiar_url(self, href):
        if not href:
            return ""
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        encoded = query.get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
            padding = "=" * (-len(encoded) % 4)
            try:
                return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            except Exception:
                return href
        return href

    def _es_resultado_util(self, title, href):
        basura = ("bing.com/search", "javascript:", "#")
        if any(fragmento in href for fragmento in basura):
            return False
        if title.lower() in ("imágenes", "videos", "noticias", "maps", "más"):
            return False
        return True


class WebResearchService:
    """Investigación web simple para diagnosticar fallos con contexto del sistema."""

    def obtener_contexto_sistema(self):
        import platform
        return {
            "sistema": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "arquitectura": platform.machine(),
            "python": platform.python_version(),
        }

    def resumen_contexto_sistema(self):
        contexto = self.obtener_contexto_sistema()
        return ", ".join(f"{k}: {v}" for k, v in contexto.items() if v)

    def investigar(self, consulta, max_results=6, incluir_contexto=False):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time

        inicio_total = time.time()

        consultas = self.generar_consultas(consulta, incluir_contexto=incluir_contexto)
        print(f"[WEB_SEARCH] Consultas generadas: {len(consultas)}")

        resultados = []
        vistos = set()
        errores = []

        # Limitar a 2 consultas máximo para velocidad (reducido de 3)
        consultas = consultas[:2]
        print(f"[WEB_SEARCH] Consultas a probar (limitado a 2): {consultas}")

        # Hacer búsquedas en paralelo para velocidad
        inicio_busquedas = time.time()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_query = {
                executor.submit(self.buscar, query, max_results=max_results): query
                for query in consultas
            }

            for future in as_completed(future_to_query, timeout=15):
                query = future_to_query[future]
                try:
                    inicio_resultado = time.time()
                    items = future.result(timeout=8)
                    print(f"[WEB_SEARCH] Búsqueda '{query[:50]}...' devolvió {len(items)} resultados en {time.time() - inicio_resultado:.1f}s")

                    for item in items:
                        url = item.get("url", "")
                        if not url or url in vistos:
                            continue
                        if not self._resultado_relevante(item, query, consulta):
                            continue
                        vistos.add(url)
                        item["consulta"] = query
                        resultados.append(item)
                        if len(resultados) >= max_results:
                            break
                except Exception as e:
                    print(f"[WEB_SEARCH] Error en búsqueda '{query[:50]}...': {str(e)[:100]}")
                    errores.append(f"{query}: {str(e)}")

                if len(resultados) >= max_results:
                    break

        print(f"[WEB_SEARCH] Búsquedas completadas en {time.time() - inicio_busquedas:.1f}s, {len(resultados)} resultados únicos")
        print(f"[WEB_SEARCH] Tiempo total de investigación: {time.time() - inicio_total:.1f}s")

        return {
            "consulta_original": consulta,
            "consultas_probadas": consultas,
            "resultados": resultados,
            "errores": errores,
        }

    def _resultado_relevante(self, item, query, consulta_original):
        texto = f"{item.get('titulo', '')} {item.get('url', '')}".lower()
        consulta = f"{query} {consulta_original}"
        consulta_ascii = unicodedata.normalize("NFKD", consulta).encode("ascii", "ignore").decode("ascii").lower()

        if self._parece_consulta_tecnica(consulta_ascii):
            dominios_tecnicos = (
                "github.com", "stackoverflow.com", "npmjs.com", "readthedocs.io", "docs.",
                "developer.", "gitlab.com", "pypi.org", "docker.com", "nodejs.org",
            )
            terminos_tecnicos = (
                "github", "issue", "npm", "package", "library", "docs", "documentation",
                "stackoverflow", "api", "websocket", "node", "python", "linux", "error",
                "exception", "framework", "socket",
            )
            if not any(dominio in texto for dominio in dominios_tecnicos) and not any(term in texto for term in terminos_tecnicos):
                return False

        tokens = [
            token for token in re.findall(r"[a-z0-9]{3,}", consulta_ascii)
            if token not in {
                "como", "para", "con", "del", "las", "los", "una", "uno", "que",
                "the", "and", "for", "with", "fix", "error", "solution", "official",
                "documentation", "latest", "current",
            }
        ]
        if not tokens:
            return True
        coincidencias = sum(1 for token in set(tokens[:8]) if token in texto)
        return coincidencias >= 1

    def generar_consultas(self, consulta, incluir_contexto=False, max_queries=3):
        base = " ".join((consulta or "").split())
        contexto = self.resumen_contexto_sistema() if incluir_contexto else ""

        # Para velocidad, NO usar IA para generar consultas
        # Usar directamente la consulta original
        consultas = [base]

        base_ascii = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
        if self._parece_consulta_tecnica(base_ascii) and len(consultas) < 2:
            # Agregar solo 1 variante técnica para velocidad
            consultas.append(f"{base_ascii} tutorial")

        if incluir_contexto and contexto and len(consultas) < 2:
            consultas.append(f"{base} {contexto}")

        limpias = []
        vistas = set()
        for query in consultas:
            query = " ".join((query or "").split()).strip(" -;")
            if not query:
                continue
            key = query.lower()
            if key not in vistas:
                vistas.add(key)
                limpias.append(query)

        # Limitar a máximo 2 consultas para velocidad
        return limpias[:2]

    def _generar_consultas_con_ia(self, consulta, contexto="", max_queries=5):
        if not settings.ZAI_API_KEY:
            return []

        system = (
            "Eres un generador de consultas de busqueda web. "
            "Devuelve solo JSON valido con la forma {\"queries\": [\"...\"]}. "
            "Crea consultas concretas, con palabras clave, nombres propios, version si aplica, "
            "y terminos en ingles si eso ayuda. "
            "Si parece un error de programacion, libreria, CLI, servidor o framework, incluye consultas "
            "con palabras como github issue, npm package, official docs, Stack Overflow, error fix. "
            "Si un nombre puede confundirse con una marca o producto no tecnico, desambigualo con "
            "software, library, API, package, framework o el lenguaje correspondiente. "
            "No respondas la pregunta; solo crea consultas."
        )
        user = f"Pregunta o tarea: {consulta}"
        if contexto:
            user += f"\nContexto del sistema: {contexto}"

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
                    "max_tokens": 500,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=5,  # Reducido de 8 a 5 segundos
            )
            response.raise_for_status()
            texto = response.json()["content"][0]["text"]
            match = re.search(r"\{.*\}", texto, re.S)
            data = json.loads(match.group(0) if match else texto)
            queries = data.get("queries", [])
            return [q for q in queries if isinstance(q, str)][:max_queries]
        except Exception:
            return self._generar_consultas_fallback(consulta, contexto)

    def _generar_consultas_fallback(self, consulta, contexto=""):
        texto = " ".join((consulta or "").split())
        texto_ascii = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
        variantes = [
            texto,
            texto_ascii,
            f"{texto_ascii} official documentation",
            f"{texto_ascii} solution",
            f"{texto_ascii} latest current",
        ]
        if self._parece_consulta_tecnica(texto_ascii):
            variantes.extend([
                f"{texto_ascii} github issue",
                f"{texto_ascii} Stack Overflow",
                f"{texto_ascii} npm package library error fix",
                f"{texto_ascii} official docs",
            ])
        if contexto:
            variantes.append(f"{texto_ascii} {contexto}")
        return variantes

    def _parece_consulta_tecnica(self, consulta):
        lower = consulta.lower()
        claves = (
            "error", "exception", "traceback", "stack", "npm", "node", "python", "django",
            "react", "vite", "docker", "git", "api", "websocket", "socket", "library",
            "package", "framework", "linux", "ubuntu", "cli", "server", "stream",
        )
        return any(clave in lower for clave in claves)

    def buscar(self, consulta, max_results=5):
        # Búsqueda simplificada y rápida
        inicio = time.time()

        # Si parece consulta técnica, priorizar fuentes técnicas
        if self._parece_consulta_tecnica(consulta):
            try:
                # Intentar GitHub primero (más rápido)
                print(f"[WEB_SEARCH] Buscando en GitHub: {consulta[:50]}...")
                resultados_tecnicos = self._buscar_fuentes_tecnicas(consulta, max_results=max_results)
                if resultados_tecnicos:
                    print(f"[WEB_SEARCH] GitHub devolvió {len(resultados_tecnicos)} resultados en {time.time() - inicio:.1f}s")
                    return resultados_tecnicos
            except Exception as e:
                print(f"[WEB_SEARCH] GitHub falló: {str(e)[:50]}")

        # Si no hay resultados técnicos, buscar en DuckDuckGo (más rápido que Bing)
        try:
            print(f"[WEB_SEARCH] Buscando en DuckDuckGo: {consulta[:50]}...")
            resultados = self._buscar_duckduckgo_lite(consulta, max_results=max_results)
            print(f"[WEB_SEARCH] DuckDuckGo devolvió {len(resultados)} resultados en {time.time() - inicio:.1f}s")
            if resultados:
                return resultados
        except Exception as e:
            print(f"[WEB_SEARCH] DuckDuckGo falló: {str(e)[:50]}")

        # Último recurso: Bing HTML (más lento)
        try:
            print(f"[WEB_SEARCH] Buscando en Bing: {consulta[:50]}...")
            resultados = self._buscar_bing_html(consulta, max_results=max_results)
            print(f"[WEB_SEARCH] Bing devolvió {len(resultados)} resultados en {time.time() - inicio:.1f}s")
            return resultados
        except Exception as e:
            print(f"[WEB_SEARCH] Bing falló: {str(e)[:50]}")

        print(f"[WEB_SEARCH] No se encontraron resultados en {time.time() - inicio:.1f}s")
        return []

    def _buscar_fuentes_tecnicas(self, consulta, max_results=5):
        resultados = []
        vistos = set()

        # Solo buscar en GitHub (más rápido)
        for buscador in (self._buscar_github_issues,):
            try:
                print(f"[WEB_SEARCH] Ejecutando {buscador.__name__}...")
                for item in buscador(consulta, max_results=max_results):
                    url = item.get("url", "")
                    if url and url not in vistos:
                        vistos.add(url)
                        resultados.append(item)
                    if len(resultados) >= max_results:
                        print(f"[WEB_SEARCH] {buscador.__name__} devolvió suficientes resultados")
                        return resultados
            except Exception as e:
                print(f"[WEB_SEARCH] {buscador.__name__} falló: {str(e)[:50]}")
                continue

        return resultados

    def _buscar_github_issues(self, consulta, max_results=5):
        response = requests.get(
            "https://api.github.com/search/issues",
            params={"q": consulta, "per_page": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"titulo": item.get("title", "GitHub issue"), "url": item.get("html_url", "")}
            for item in data.get("items", [])
            if item.get("html_url")
        ]

    def _buscar_github_repos(self, consulta, max_results=5):
        response = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": consulta, "per_page": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"titulo": item.get("full_name", "GitHub repository"), "url": item.get("html_url", "")}
            for item in data.get("items", [])
            if item.get("html_url")
        ]

    def _buscar_npm_registry(self, consulta, max_results=5):
        response = requests.get(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": consulta, "size": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        resultados = []
        for item in data.get("objects", []):
            package = item.get("package", {})
            name = package.get("name")
            url = package.get("links", {}).get("npm")
            description = package.get("description")
            if name and url:
                titulo = f"{name} - npm"
                if description:
                    titulo = f"{titulo}: {description[:100]}"
                resultados.append({"titulo": titulo, "url": url})
        return resultados

    def _buscar_duckduckgo_lite(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _DuckDuckGoHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def _buscar_bing_html(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _BingHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def _buscar_bing_rss(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": consulta, "format": "rss"},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.text)
        resultados = []
        for item in root.findall("./channel/item"):
            titulo = item.findtext("title") or ""
            url = item.findtext("link") or ""
            if titulo and url:
                resultados.append({"titulo": titulo, "url": url})
            if len(resultados) >= max_results:
                break
        return resultados

    def _buscar_duckduckgo_html(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _DuckDuckGoHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def investigar_fallo(self, tarea, error, max_results=5):
        contexto = self.resumen_contexto_sistema()
        consulta = f"{tarea} error {error[:180]} {contexto} solucion"
        try:
            resultados = self.buscar(consulta, max_results=max_results)
        except Exception as e:
            return (
                "No pude consultar internet en este momento "
                f"({str(e)}). Contexto del sistema: {contexto}"
            )

        if not resultados:
            return f"No encontré resultados útiles en internet. Contexto del sistema: {contexto}"

        lineas = [
            "Investigué el fallo con el contexto de este equipo:",
            contexto,
            "",
            "Resultados útiles:",
        ]
        for idx, item in enumerate(resultados, 1):
            lineas.append(f"{idx}. {self._limpiar_texto_para_respuesta(item['titulo'])}")
        lineas.extend([
            "",
            "Siguiente paso recomendado: revise el resultado oficial o de documentación más pertinente, "
            "y si quiere ejecutar una alternativa, pídala explícitamente para evitar correr comandos inseguros."
        ])
        return "\n".join(lineas)

    def _limpiar_texto_para_respuesta(self, texto):
        texto = texto or ""
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


class PCActionService:
    """Acciones generales del PC para Linux/entornos de escritorio."""

    ACCIONES_CON_CONFIRMACION = {
        "apagar", "reiniciar", "suspender", "hibernar",
    }

    NAVEGADORES = {
        "firefox": ["firefox", "firefox-esr"],
        "firefoz": ["firefox", "firefox-esr"],
        "chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
        "chromium": ["chromium", "chromium-browser"],
        "brave": ["brave-browser", "brave"],
        "edge": ["microsoft-edge", "msedge"],
    }

    TERMINALES = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
    EXPLORADORES = ["nautilus", "dolphin", "thunar", "pcmanfm", "xdg-open"]

    def __init__(self, working_dir=None):
        self.working_dir = working_dir or os.getcwd()

    def requiere_confirmacion(self, accion):
        return accion in self.ACCIONES_CON_CONFIRMACION

    def _run(self, args, timeout=30):
        try:
            resultado = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.working_dir,
            )
            salida = (resultado.stdout or resultado.stderr or "").strip()
            if resultado.returncode == 0:
                return True, salida
            return False, salida or f"El comando terminó con código {resultado.returncode}."
        except FileNotFoundError:
            return False, f"No encontré la aplicación: {args[0]}"
        except subprocess.TimeoutExpired:
            return False, f"La acción excedió el tiempo límite de {timeout} segundos."
        except Exception as e:
            return False, str(e)

    def _encontrar_binario(self, candidatos):
        for candidato in candidatos:
            if shutil.which(candidato):
                return candidato
        return None

    def abrir_url(self, url):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            webbrowser.open(url, new=2)
            return True, f"Abrí la página: {url}"
        except Exception:
            return self._run(["xdg-open", url])

    def buscar_web(self, consulta, motor="google"):
        consulta = consulta.strip()
        if not consulta:
            return False, "Indique qué desea buscar."

        motores = {
            "google": "https://www.google.com/search?q={query}",
            "bing": "https://www.bing.com/search?q={query}",
            "duckduckgo": "https://duckduckgo.com/?q={query}",
            "youtube": "https://www.youtube.com/results?search_query={query}",
        }
        plantilla = motores.get(motor.lower(), motores["google"])
        url = plantilla.format(query=urllib.parse.quote_plus(consulta))
        return self.abrir_url(url)

    def abrir_app(self, app, argumentos=None):
        argumentos = argumentos or []
        return self._run([app, *argumentos], timeout=10)

    def abrir_terminal(self, ruta=None):
        terminal = self._encontrar_binario(self.TERMINALES)
        if not terminal:
            return False, "No encontré una terminal gráfica compatible."

        ruta = ruta or self.working_dir
        if terminal == "gnome-terminal":
            return self._run([terminal, "--working-directory", ruta], timeout=10)
        if terminal == "konsole":
            return self._run([terminal, "--workdir", ruta], timeout=10)
        if terminal == "xfce4-terminal":
            return self._run([terminal, "--working-directory", ruta], timeout=10)
        return self._run([terminal], timeout=10)

    def abrir_carpeta(self, ruta="."):
        ruta = os.path.abspath(os.path.join(self.working_dir, ruta))
        explorador = self._encontrar_binario(self.EXPLORADORES)
        if not explorador:
            return False, "No encontré un explorador de archivos compatible."
        return self._run([explorador, ruta], timeout=10)

    def cerrar_app(self, app):
        import signal
        import time

        patron = app.strip()
        if not patron:
            return False, "Indique el nombre del proceso que desea cerrar."

        procesos_antes = self._buscar_procesos(patron)
        if not procesos_antes:
            return False, f"No encontré procesos activos que coincidan con '{patron}'."

        for pid, _linea in procesos_antes:
            self._terminar_pid(pid, signal.SIGTERM)
        time.sleep(1.2)
        procesos_despues = self._buscar_procesos(patron)

        if not procesos_despues:
            return True, f"Cerré '{patron}'. Procesos finalizados: {len(procesos_antes)}."

        for pid, _linea in procesos_despues:
            self._terminar_pid(pid, signal.SIGKILL)
        time.sleep(0.8)
        procesos_finales = self._buscar_procesos(patron)

        if not procesos_finales:
            return True, f"Forcé el cierre de '{patron}'. Procesos finalizados: {len(procesos_antes)}."

        detalle = "\n".join(linea for _pid, linea in procesos_finales[:5])
        return False, f"Intenté cerrar '{patron}', pero sigue activo:\n{detalle}"

    def _buscar_procesos(self, patron):
        exito, salida = self._run(["pgrep", "-af", patron], timeout=10)
        if not exito or not salida:
            return []

        excluidos = self._pids_a_excluir()
        procesos = []
        for linea in salida.splitlines():
            if "pgrep -af" in linea or "pkill" in linea:
                continue
            partes = linea.split(maxsplit=1)
            if not partes or not partes[0].isdigit():
                continue
            pid = int(partes[0])
            if pid in excluidos:
                continue
            procesos.append((pid, linea))
        return procesos

    def _pids_a_excluir(self):
        pids = {os.getpid()}
        pid = os.getppid()
        while pid and pid not in pids:
            pids.add(pid)
            try:
                with open(f"/proc/{pid}/stat", "r") as stat_file:
                    contenido = stat_file.read().split()
                pid = int(contenido[3])
            except Exception:
                break
        return pids

    def _terminar_pid(self, pid, senal):
        try:
            os.kill(pid, senal)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    def cerrar_navegador(self, navegador="firefox"):
        candidatos = self.NAVEGADORES.get(navegador.lower(), [navegador])
        errores = []
        for candidato in candidatos:
            exito, resultado = self.cerrar_app(candidato)
            if exito:
                return True, resultado
            errores.append(resultado)
        return False, "\n".join(errores) or f"No pude cerrar {navegador}."

    def bloquear(self):
        comandos = [
            ["loginctl", "lock-session"],
            ["xdg-screensaver", "lock"],
            ["gnome-screensaver-command", "-l"],
        ]
        for comando in comandos:
            if shutil.which(comando[0]):
                exito, resultado = self._run(comando, timeout=10)
                if exito:
                    return True, "Bloqueé la sesión."
                ultimo_error = resultado
        return False, locals().get("ultimo_error", "No encontré un método para bloquear la sesión.")

    def suspender(self):
        return self._run(["systemctl", "suspend"], timeout=10)

    def hibernar(self):
        return self._run(["systemctl", "hibernate"], timeout=10)

    def apagar(self):
        return self._run(["systemctl", "poweroff"], timeout=10)

    def reiniciar(self):
        return self._run(["systemctl", "reboot"], timeout=10)

    def diagnostico(self):
        comandos = [
            ("Disco", ["df", "-h"]),
            ("Memoria", ["free", "-h"]),
            ("Carga", ["uptime"]),
            ("Servicios fallidos", ["systemctl", "--failed", "--no-pager"]),
        ]
        secciones = []
        for titulo, comando in comandos:
            exito, salida = self._run(comando, timeout=20)
            estado = salida if salida else "Sin salida."
            secciones.append(f"## {titulo}\n{estado}" if exito else f"## {titulo}\nError: {estado}")
        return True, "\n\n".join(secciones)

    def errores_recientes(self):
        if not shutil.which("journalctl"):
            return False, "journalctl no está disponible en este sistema."
        return self._run(["journalctl", "-p", "3", "-n", "80", "--no-pager"], timeout=20)


class BackgroundTaskManager:
    """Administrador simple de tareas en segundo plano para el dashboard local."""

    _tasks = {}
    _lock = threading.Lock()
    _loaded = False
    _storage_file = os.path.join(settings.BASE_DIR, "background_tasks.json")

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        with cls._lock:
            if cls._loaded:
                return
            try:
                if os.path.exists(cls._storage_file):
                    with open(cls._storage_file, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        cls._tasks = data
            except Exception as exc:
                print(f"[BackgroundTaskManager] No se pudo cargar historial: {exc}")
            cls._loaded = True

    @classmethod
    def _persist(cls):
        try:
            tasks = list(cls._tasks.values())
            tasks.sort(key=lambda item: item.get("creado_en", ""), reverse=True)
            data = {task["id"]: task for task in tasks[:100] if task.get("id")}
            tmp_file = cls._storage_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_file, cls._storage_file)
        except Exception as exc:
            print(f"[BackgroundTaskManager] No se pudo guardar historial: {exc}")

    @classmethod
    def crear(cls, titulo, comando, target, *args, **kwargs):
        cls._ensure_loaded()
        task_id = str(uuid.uuid4())
        ahora = datetime.now().isoformat(timespec="seconds")
        task = {
            "id": task_id,
            "titulo": titulo,
            "comando": comando,
            "estado": "pendiente",
            "resultado": "",
            "error": "",
            "creado_en": ahora,
            "iniciado_en": None,
            "finalizado_en": None,
        }
        with cls._lock:
            cls._tasks[task_id] = task
            cls._persist()

        thread = threading.Thread(
            target=cls._ejecutar,
            args=(task_id, target, args, kwargs),
            daemon=True,
        )
        thread.start()
        return task.copy()

    @classmethod
    def _ejecutar(cls, task_id, target, args, kwargs):
        cls._actualizar(task_id, estado="ejecutando", iniciado_en=datetime.now().isoformat(timespec="seconds"))
        try:
            resultado = target(*args, **kwargs)
            cls._actualizar(
                task_id,
                estado="completada",
                resultado=str(resultado or "Tarea completada sin salida."),
                finalizado_en=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            cls._actualizar(
                task_id,
                estado="error",
                error=str(exc),
                finalizado_en=datetime.now().isoformat(timespec="seconds"),
            )

    @classmethod
    def _actualizar(cls, task_id, **changes):
        cls._ensure_loaded()
        with cls._lock:
            if task_id in cls._tasks:
                cls._tasks[task_id].update(changes)
                cls._persist()

    @classmethod
    def obtener(cls, task_id):
        cls._ensure_loaded()
        with cls._lock:
            task = cls._tasks.get(task_id)
            return task.copy() if task else None

    @classmethod
    def listar(cls, limite=20):
        cls._ensure_loaded()
        with cls._lock:
            tasks = list(cls._tasks.values())
        tasks.sort(key=lambda item: item["creado_en"], reverse=True)
        return [task.copy() for task in tasks[:limite]]


class TTSService:
    """
    Servicio de Texto a Voz usando:
    - Principal: edge-tts con es-CO-GonzaloNeural (voz masculina colombiana)
    - Respaldo: pyttsx3 (offline, voces del sistema)
    """

    # Voces disponibles - Prioridad Deepgram Aura-2 (Premium AI Voices)
    VOCES_ESPANOL = [
        # DEEPGRAM AURA-2 - Premium AI Voices (Requiere API Key, ya configurada)
        # MUJERES - Voces más naturales
        ('aura-2-celeste-es', 'Celeste ✨ (Deepgram Aura - Mujer, Colombia - RECOMENDADO)'),
        ('aura-2-estrella-es', 'Estrella ✨ (Deepgram Aura - Mujer, México)'),
        ('aura-2-selena-es', 'Selena ✨ (Deepgram Aura - Mujer, Latino)'),
        ('aura-2-carina-es', 'Carina ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        ('aura-2-diana-es', 'Diana ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        # HOMBRES
        ('aura-2-nestor-es', 'Néstor ✨ (Deepgram Aura - Hombre, España)'),
        ('aura-2-sirio-es', 'Sirio ✨ (Deepgram Aura - Hombre, México)'),
        ('aura-2-javier-es', 'Javier ✨ (Deepgram Aura - Hombre, Latino)'),
        ('aura-2-aquila-es', 'Aquila ✨ (Deepgram Aura - Hombre, Codeswitching ES/EN)'),
        ('aura-2-alvaro-es', 'Álvaro ✨ (Deepgram Aura - Hombre, España)'),

        # PIPER TTS - Open Source 100% Gratis e Ilimitado (Offline, Voz Natural)
        ('piper:es_ES-mls_10246-low', 'Carlos 🎙️ (Piper TTS - Hombre, España - GRATIS ILIMITADO)'),

        # DEEPGRAM AURA-2 - Premium AI Voices (HOMBRES - Requiere API Key)
        ('aura-2-javier-es', 'Javier ✨ (Deepgram Aura - Hombre, Latino)'),
        ('aura-2-aquila-es', 'Aquila ✨ (Deepgram Aura - Hombre, Codeswitching ES/EN)'),
        ('aura-2-nestor-es', 'Néstor ✨ (Deepgram Aura - Hombre, España)'),
        ('aura-2-sirio-es', 'Sirio ✨ (Deepgram Aura - Hombre, México)'),
        ('aura-2-alvaro-es', 'Álvaro ✨ (Deepgram Aura - Hombre, España)'),

        # EDGE-TTS - Microsoft Neural Voices (HOMBRES - Gratis, requiere internet)
        # COLOMBIA
        ('es-CO-GonzaloNeural', 'Gonzalo 🇨🇴 (Hombre, Colombia)'),
        # MÉXICO
        ('es-MX-JorgeNeural', 'Jorge 🇲🇽 (Hombre, México)'),
        ('es-MX-CardenasNeural', 'Cárdenas 🇲🇽 (Hombre, México)'),
        # ESPAÑA
        ('es-ES-AlvaroNeural', 'Alvaro 🇪🇸 (Hombre, España)'),
        ('es-ES-PabloNeural', 'Pablo 🇪🇸 (Hombre, España)'),
        # ARGENTINA
        ('es-AR-ThomasNeural', 'Thomas 🇦🇷 (Hombre, Argentina)'),
        # PERÚ
        ('es-PE-AlexNeural', 'Alex 🇵🇪 (Hombre, Perú)'),
        # LATINO US
        ('es-US-AlonsoNeural', 'Alonso 🇺🇸 (Hombre, Latino US)'),
        # CHILE
        ('es-CL-LucasNeural', 'Lucas 🇨🇱 (Hombre, Chile)'),

        # GOOGLE TTS - Neutral (Gratis, fallback si Edge-TTS falla)
        ('gtts:es-co', 'Google 🇨🇴 (Colombiana - Neutral)'),
        ('gtts:es-mx', 'Google 🇲🇽 (México - Neutral)'),
        ('gtts:es-es', 'Google 🇪🇸 (España - Neutral)'),
        ('gtts:es-us', 'Google 🇺🇸 (Latino US - Neutral)'),

        # MUJERES (Opciones adicionales)
        ('es-CO-SalomeNeural', 'Salome 🇨🇴 (Mujer, Colombia)'),
        ('es-MX-DaliaNeural', 'Dalia 🇲🇽 (Mujer, México)'),
        ('es-ES-ElviraNeural', 'Elvira 🇪🇸 (Mujer, España)'),
        ('aura-2-celeste-es', 'Celeste ✨ (Deepgram Aura - Mujer, Colombia)'),
        ('aura-2-estrella-es', 'Estrella ✨ (Deepgram Aura - Mujer, México)'),
        ('aura-2-selena-es', 'Selena ✨ (Deepgram Aura - Mujer, Latino)'),
        ('aura-2-carina-es', 'Carina ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        ('aura-2-diana-es', 'Diana ✨ (Deepgram Aura - Mujer, Codeswitching)'),

        # PYTTSX3 - Offline (Último recurso, voces robóticas del sistema)
        ('pyttsx3:male', 'Voz Hombre (Offline - Sistema - Robótica)'),
        ('pyttsx3:female', 'Voz Mujer (Offline - Sistema - Robótica)'),
    ]

    @classmethod
    def obtener_voces_disponibles(cls):
        return cls.VOCES_ESPANOL

    def generar_audio(self, texto, voz=None, velocidad=1.0):
        """
        Genera audio desde texto.
        voz: Código de voz (default: aura-2-celeste-es - Deepgram Aura, Mujer Colombia)
        velocidad: 0.5 a 2.0 (default: 1.0)

        Orden de prioridad: Deepgram Aura-2 (premium) → Piper (gratis) → Edge-TTS → gTTS → pyttsx3

        NOTA: Para textos medianos/largos, se generan múltiples archivos de audio
        que deben reproducirse en secuencia.
        """
        voz_a_usar = voz or 'aura-2-celeste-es'

        print(f"[TTS] Generando audio con voz: {voz_a_usar} ({len(texto)} caracteres)")

        media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')
        os.makedirs(media_dir, exist_ok=True)

        # Dividir textos medianos/largos para evitar audios enormes que se corten al reproducir.
        limite_caracteres = 550
        if len(texto) > limite_caracteres:
            print(f"[TTS] Texto largo ({len(texto)} chars), dividiendo en partes...")
            return self._generar_audio_largo(texto, voz_a_usar, velocidad, limite_caracteres)

        # DEEPGRAM AURA-2 - Premium (requiere API key, ya configurada)
        if voz_a_usar.startswith('aura-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_deepgram_aura(texto, voz_a_usar, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Deepgram Aura falló: {e}")
                return self._generar_audio_respaldo(texto, voz_a_usar, velocidad, media_dir)

        # PIPER TTS - Open Source 100% Gratis e Ilimitado (Offline)
        if voz_a_usar.startswith('piper:'):
            modelo = voz_a_usar.replace('piper:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

        # GOOGLE TTS - Opción confiable (fallback)
        if voz_a_usar.startswith('gtts:'):
            lang = voz_a_usar.replace('gtts:', '')
            nombre = f"{uuid.uuid4()}.mp3"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_gtts(texto, lang, ruta, nombre)

        # EDGE-TTS - Microsoft (puede fallar con 403)
        if voz_a_usar.startswith('es-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz_a_usar, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Edge-TTS falló: {e}")
                # Detectar si era voz masculina por el nombre
                voces_masculinas = ['Gonzalo', 'Jorge', 'Cardenas', 'Alvaro', 'Pablo', 'Thomas', 'Alex', 'Alonso', 'Lucas', 'Javier', 'Aquila']
                era_masculina = any(v in voz_a_usar for v in voces_masculinas)

                if era_masculina:
                    print(f"[TTS] Fallback a pyttsx3 masculino (offline)")
                    nombre = f"{uuid.uuid4()}.wav"
                    ruta = os.path.join(media_dir, nombre)
                    return self._generar_con_pyttsx3(texto, ruta, nombre, 'male', velocidad)
                else:
                    print(f"[TTS] Fallback a gTTS (online)")
                    nombre = f"{uuid.uuid4()}.mp3"
                    ruta = os.path.join(media_dir, nombre)
                    return self._generar_con_gtts(texto, 'es-co', ruta, nombre)

        # PYTTSX3 - Offline (último recurso, robótica)
        if voz_a_usar.startswith('pyttsx3:'):
            genero = voz_a_usar.replace('pyttsx3:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_pyttsx3(texto, ruta, nombre, genero, velocidad)

        # Default fallback
        nombre = f"{uuid.uuid4()}.mp3"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_gtts(texto, 'es-co', ruta, nombre)

    def _generar_audio_largo(self, texto, voz, velocidad, limite_caracteres):
        """Genera múltiples archivos de audio para textos largos."""
        import re

        # Dividir por oraciones, saltos de línea y viñetas. Luego partir por palabras si una parte sigue larga.
        oraciones = [
            bloque.strip()
            for bloque in re.split(r'(?<=[.!?])\s+|\n+|(?=\s*[•\-]\s+)', texto)
            if bloque.strip()
        ]
        partes = []
        parte_actual = ""
        urls = []

        for oracion in oraciones:
            fragmentos = self._partir_texto_por_palabras(oracion, limite_caracteres)
            for fragmento in fragmentos:
                if len(parte_actual) + len(fragmento) + 1 <= limite_caracteres:
                    parte_actual += (" " if parte_actual else "") + fragmento
                else:
                    if parte_actual:
                        partes.append(parte_actual)
                    parte_actual = fragmento

        if parte_actual:
            partes.append(parte_actual)

        print(f"[TTS] Texto dividido en {len(partes)} partes")

        media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')

        for idx, parte in enumerate(partes):
            print(f"[TTS] Generando parte {idx + 1}/{len(partes)} ({len(parte)} caracteres)")

            audio_url = self._generar_audio_parte(parte, voz, velocidad, media_dir)
            if audio_url:
                urls.append(audio_url)

        # Devolver la primera URL como principal, pero incluir metadata con las demás
        if urls:
            # Guardar la lista de URLs en un archivo JSON para que el frontend sepa que hay múltiples partes
            metadata_nombre = f"{uuid.uuid4()}_parts.json"
            metadata_ruta = os.path.join(media_dir, metadata_nombre)
            with open(metadata_ruta, 'w') as f:
                json.dump({"parts": urls}, f)

            # Devolver la primera URL con un parámetro especial que indica que hay múltiples partes
            primera_url = urls[0]
            if len(urls) > 1:
                # Agregar parámetro para indicar que hay múltiples partes
                separador = '&' if '?' in primera_url else '?'
                return f"{primera_url}{separador}parts={metadata_nombre}"
            return primera_url

        return None

    def _partir_texto_por_palabras(self, texto, limite_caracteres):
        if len(texto) <= limite_caracteres:
            return [texto]

        partes = []
        actual = ""
        for palabra in texto.split():
            if len(actual) + len(palabra) + 1 <= limite_caracteres:
                actual += (" " if actual else "") + palabra
            else:
                if actual:
                    partes.append(actual)
                actual = palabra

        if actual:
            partes.append(actual)

        return partes

    def _generar_audio_parte(self, texto, voz, velocidad, media_dir):
        if voz.startswith('aura-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_deepgram_aura(texto, voz, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Deepgram Aura falló en parte: {e}")
                return self._generar_audio_respaldo(texto, voz, velocidad, media_dir)

        if voz.startswith('piper:'):
            modelo = voz.replace('piper:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

        if voz.startswith('gtts:'):
            lang = voz.replace('gtts:', '')
            nombre = f"{uuid.uuid4()}.mp3"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_gtts(texto, lang, ruta, nombre)

        if voz.startswith('es-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Edge-TTS falló en parte, usando Piper: {e}")
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

        nombre = f"{uuid.uuid4()}.wav"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

    def _generar_audio_respaldo(self, texto, voz_original, velocidad, media_dir):
        voz_respaldo = (getattr(settings, 'TTS_FALLBACK_VOICE', None) or 'piper:es_ES-mls_10246-low').strip()
        if not voz_respaldo or voz_respaldo == voz_original:
            voz_respaldo = 'piper:es_ES-mls_10246-low'

        print(f"[TTS] Usando voz de respaldo: {voz_respaldo}")

        try:
            if voz_respaldo.startswith('piper:'):
                modelo = voz_respaldo.replace('piper:', '')
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

            if voz_respaldo.startswith('gtts:'):
                lang = voz_respaldo.replace('gtts:', '')
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_gtts(texto, lang, ruta, nombre)

            if voz_respaldo.startswith('es-'):
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz_respaldo, ruta, nombre, velocidad)

            if voz_respaldo.startswith('pyttsx3:'):
                genero = voz_respaldo.replace('pyttsx3:', '')
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_pyttsx3(texto, ruta, nombre, genero, velocidad)
        except Exception as exc:
            print(f"[TTS] Voz de respaldo falló ({voz_respaldo}): {exc}")

        nombre = f"{uuid.uuid4()}.wav"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

    def _generar_con_deepgram_aura(self, texto, voz, ruta, nombre, velocidad):
        """
        Genera audio usando Deepgram Aura-2 (Premium AI Text-to-Speech).
        Requiere API Key configurada en DEEPGRAM_API_KEY.

        Voces españolas disponibles:
        - Mujeres: celeste (Colombia), estrella (México), selena (Latino), carina (España), diana (España)
        - Hombres: javier (Latino), aquila (Codeswitching), nestor (España), sirio (México), alvaro (España)
        """
        api_key = (getattr(settings, 'DEEPGRAM_API_KEY', None) or '').strip()
        if not api_key or api_key == 'tu_deepgram_api_key_aqui':
            raise ValueError("DEEPGRAM_API_KEY no configurada. Obtén una en https://console.deepgram.com/")

        try:
            # Configurar cliente Deepgram
            deepgram = DeepgramClient(api_key)

            # El modelo ya viene en formato correcto: aura-2-javier-es
            # Opciones de síntesis
            options = SpeakOptions(
                model=voz,  # Usar directamente el código de voz completo
                encoding="mp3",
            )

            # Agregar control de velocidad solo si es diferente a 1.0
            if velocidad != 1.0:
                options.speed = velocidad

            # Generar audio usando la API REST
            response = deepgram.speak.rest.v("1").save(ruta, {"text": texto}, options)

            print(f"[TTS] Deepgram Aura: Audio generado con voz {voz}")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] Deepgram Aura error: {e}")
            raise

    def _generar_con_piper(self, texto, modelo, ruta, nombre, velocidad=1.0):
        """
        Genera audio usando Piper TTS (Open Source, 100% Gratis, Ilimitado).
        Modelo: es_ES-mls_10246-low (voz masculina española)
        """
        try:
            # Ruta base de los modelos de Piper
            piper_models_dir = os.path.join(settings.BASE_DIR, 'piper_models')

            # Rutas del modelo
            model_path = os.path.join(piper_models_dir, f'{modelo}.onnx')
            config_path = os.path.join(piper_models_dir, f'{modelo}.onnx.json')

            if not os.path.exists(model_path):
                raise FileNotFoundError(f"Modelo Piper no encontrado: {model_path}")

            print(f"[TTS] Piper: Cargando modelo {modelo}")

            # Cargar el modelo de Piper
            voice = PiperVoice.load(model_path, config_path)

            # Generar audio usando synthesize_waz
            with wave.open(ruta, 'wb') as wav_file:
                voice.synthesize_wav(texto, wav_file)

            print(f"[TTS] Piper: Audio generado exitosamente")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] Piper error: {e}")
            raise

    def _generar_con_edge_tts(self, texto, voz, ruta, nombre, velocidad):
        """
        Genera audio usando edge-tts (Microsoft Edge Neural Voices).
        Requiere conexión a internet.
        """
        try:
            asyncio.run(self._edge_tts_async(texto, voz, ruta, velocidad))
            return f"{settings.MEDIA_URL}audios/{nombre}"
        except Exception as e:
            print(f"[TTS] Edge-TTS error: {e}")
            raise

    async def _edge_tts_async(self, texto, voz, ruta, velocidad):
        """Función asíncrona para edge-tts con manejo de errores"""
        import time
        # Pequeño delay para evitar bloqueos
        await asyncio.sleep(0.5)

        try:
            communicate = edge_tts.Communicate(
                text=texto,
                voice=voz,
                rate=f'+{int((velocidad - 1) * 100)}%' if velocidad != 1.0 else '+0%',
                connect_timeout=30
            )
            await communicate.save(ruta)
        except Exception as e:
            print(f"[TTS] Edge-TTS conexión falló: {e}")
            raise

    def _generar_con_pyttsx3(self, texto, ruta, nombre, genero='male', velocidad=1.0):
        """
        Genera audio usando pyttsx3 (offline, voces del sistema).
        Guarda directamente como WAV para evitar dependencia de ffmpeg.
        """
        import warnings
        warnings.filterwarnings("ignore", category=RuntimeWarning)

        try:
            engine = pyttsx3.init()

            # Configurar velocidad (pyttsx3 usa rango 50-500, siendo 200 normal)
            rate = int(200 * velocidad)
            engine.setProperty('rate', rate)

            # Configurar volumen
            engine.setProperty('volume', 0.9)

            # Intentar configurar voz por género
            voices = engine.getProperty('voices')
            selected_voice = None

            # Buscar voz en español o latinoamericana
            for voice in voices:
                voice_name = voice.name.lower()
                voice_id = voice.id.lower()

                # Prioridad: Español Latinoamérica
                if 'spanish' in voice_name or 'es_' in voice_id or 'latino' in voice_name:
                    if genero == 'male':
                        # Preferir voces masculinas
                        if any(m in voice_name for m in ['david', 'jorge', 'male', 'hombre', 'juan', 'carlos', 'miguel', 'alex']):
                            selected_voice = voice.id
                            break
                    else:
                        # Preferir voces femeninas
                        if any(f in voice_name for f in ['female', 'mujer', 'zira', 'santa', 'maria', 'elena', 'sofia']):
                            selected_voice = voice.id
                            break

            # Si no encontramos voz específica, usar la primera disponible
            if not selected_voice and voices:
                selected_voice = voices[0].id

            if selected_voice:
                engine.setProperty('voice', selected_voice)

            # Guardar directamente como WAV
            engine.save_to_file(texto, ruta)

            # Ejecutar y esperar con mejor manejo de errores
            engine.runAndWait()

            # Pequeña pausa para asegurar que el archivo se escribió
            import time
            time.sleep(0.5)

            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] pyttsx3 error: {e}")
            # Último recurso: gTTS
            return self._generar_con_gtts(texto, lang='es-co')

    def _generar_con_gtts(self, texto, lang='es-co', ruta=None, nombre=None):
        """
        Genera audio usando Google TTS (gTTS).
        lang: Código de idioma (es-co, es-mx, es-es, es-us)
        Gratis, claro, confiable. Requiere internet.
        """
        try:
            # Si no se proporciona ruta/nombre, generarlos
            if not ruta or not nombre:
                media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')
                os.makedirs(media_dir, exist_ok=True)
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)

            # Mapeo de idiomas a dominios de Google
            lang_tld_map = {
                'es-co': 'com.co',  # Colombia
                'es-mx': 'com.mx',  # México
                'es-es': 'es',      # España
                'es-us': 'us',      # Latino US
            }

            # Extraer código de idioma base (es) y dominio
            tld = lang_tld_map.get(lang, 'com.co')

            print(f"[TTS] gTTS: Generando audio con idioma {lang} (tld={tld})")

            tts = gTTS(text=texto, lang='es', tld=tld, slow=False)
            tts.save(ruta)

            print(f"[TTS] gTTS: Audio guardado en {ruta}")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] gTTS error: {e}")
            return None

    def _wav_to_mp3(self, wav_path, mp3_path):
        """Convierte WAV a MP3 usando pydub"""
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_wav(wav_path)
            audio.export(mp3_path, format='mp3', bitrate='128k')
        except:
            # Si pydub no está disponible, usar ffmpeg directamente
            subprocess.run(['ffmpeg', '-i', wav_path, '-codec:a', 'libmp3lame', '-b:a', '128k', mp3_path],
                          capture_output=True, timeout=30)


class SchedulerService:
    """
    Servicio de programación de tareas que corre en segundo plano.
    Revisa periódicamente las tareas pendientes y las ejecuta cuando llega el momento.
    """

    def __init__(self, intervalo_segundos=30):
        """
        intervalo_segundos: Cada cuánto tiempo revisa tareas pendientes (default: 30s)
        """
        self.intervalo = intervalo_segundos
        self._running = False
        self._thread = None
        self.pc_service = PCActionService()

    def iniciar(self):
        """Inicia el scheduler en segundo plano."""
        if self._running:
            print("[SCHEDULER] Ya está corriendo")
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[SCHEDULER] Iniciado (revisa cada {self.intervalo}s)")

    def detener(self):
        """Detiene el scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[SCHEDULER] Detenido")

    def _loop(self):
        """Loop principal que revisa y ejecuta tareas pendientes."""
        from asistente.models import TareaProgramada

        while self._running:
            try:
                ahora = datetime.now()

                # Buscar tareas pendientes que deben ejecutarse ahora o antes
                tareas = TareaProgramada.objects.filter(
                    estado='pendiente',
                    programado_para__lte=ahora
                )

                for tarea in tareas:
                    self._ejecutar_tarea(tarea)

            except Exception as e:
                print(f"[SCHEDULER] Error en loop: {e}")

            # Esperar hasta la siguiente revisión
            time.sleep(self.intervalo)

    def _ejecutar_tarea(self, tarea):
        """Ejecuta una tarea específica según su tipo."""
        print(f"[SCHEDULER] Ejecutando tarea: {tarea.titulo} ({tarea.tipo_accion})")

        # Marcar como ejecutando
        tarea.estado = 'ejecutando'
        tarea.save()

        try:
            if tarea.tipo_accion == 'whatsapp':
                resultado = self._enviar_whatsapp(tarea.parametros)
                tarea.marcar_ejecutada(exitoso=resultado['exito'], resultado=resultado.get('mensaje'), error=resultado.get('error'))

            elif tarea.tipo_accion == 'comando_pc':
                from asistente.services import ComandoService
                cmd_service = ComandoService(modo_desarrollador=True)
                comando = tarea.parametros.get('comando', '')
                exito, resultado = cmd_service.ejecutar(comando, timeout=60)
                tarea.marcar_ejecutada(exitoso=exito, resultado=resultado)

            elif tarea.tipo_accion == 'url':
                url = tarea.parametros.get('url', '')
                exito, resultado = self.pc_service.abrir_url(url)
                tarea.marcar_ejecutada(exitoso=exito, resultado=resultado)

            elif tarea.tipo_accion == 'sistema':
                accion = tarea.parametros.get('accion', '')
                exito, resultado = self._ejecutar_accion_sistema(accion)
                tarea.marcar_ejecutada(exitoso=exito, resultado=resultado)

            elif tarea.tipo_accion == 'recordatorio':
                # Solo marcar como completada (el frontend mostrará una notificación)
                tarea.marcar_ejecutada(
                    exitoso=True,
                    resultado=f"Recordatorio: {tarea.parametros.get('mensaje', tarea.titulo)}"
                )

            # Si tiene repetición, crear la siguiente instancia
            if tarea.repetir:
                self._programar_siguiente_instancia(tarea)

        except Exception as e:
            print(f"[SCHEDULER] Error ejecutando tarea {tarea.id}: {e}")
            tarea.marcar_ejecutada(exitoso=False, error=str(e))

    def _enviar_whatsapp(self, parametros):
        """Envía un mensaje de WhatsApp a través del baileys-service."""
        import requests

        numero = parametros.get('numero', '')
        mensaje = parametros.get('mensaje', '')
        linea = parametros.get('linea', 'principal')

        if not numero or not mensaje:
            return {'exito': False, 'error': 'Faltan número o mensaje'}

        # Normalizar número (quitar +, espacios, guiones)
        numero_limpio = ''.join(c for c in numero if c.isdigit())
        if not numero_limpio:
            return {'exito': False, 'error': 'Número inválido'}

        try:
            url_baileys = getattr(settings, 'BAILEYS_SERVICE_URL', None) or 'http://localhost:3002'

            response = requests.post(
                f"{url_baileys}/send-message",
                json={
                    'numero': numero_limpio,
                    'mensaje': mensaje,
                    'linea': linea,
                },
                timeout=10
            )

            if response.status_code == 200:
                return {'exito': True, 'mensaje': f'Mensaje enviado a {numero}'}
            else:
                return {'exito': False, 'error': f'Error baileys: {response.text}'}

        except Exception as e:
            return {'exito': False, 'error': str(e)}

    def _ejecutar_accion_sistema(self, accion):
        """Ejecuta acciones de sistema como bloquear, suspender, etc."""
        accion = accion.lower()

        if accion == 'bloquear':
            return self.pc_service.bloquear()
        elif accion == 'suspender':
            return self.pc_service.suspender()
        elif accion == 'hibernar':
            return self.pc_service.hibernar()
        elif accion == 'apagar':
            return self.pc_service.apagar()
        elif accion == 'reiniciar':
            return self.pc_service.reiniciar()
        else:
            return False, f"Acción de sistema no reconocida: {accion}"

    def _programar_siguiente_instancia(self, tarea_original):
        """Crea una nueva instancia de la tarea según el patrón de repetición."""
        from datetime import timedelta

        siguiente_hora = None
        repetir = tarea_original.repetir.lower()

        if repetir == 'diario':
            siguiente_hora = tarea_original.programado_para + timedelta(days=1)
        elif repetir == 'semanal':
            siguiente_hora = tarea_original.programado_para + timedelta(weeks=1)
        elif repetir == 'mensual':
            # Aproximación simple: 30 días
            siguiente_hora = tarea_original.programado_para + timedelta(days=30)

        if siguiente_hora:
            # Crear nueva tarea
            TareaProgramada.objects.create(
                perfil=tarea_original.perfil,
                titulo=tarea_original.titulo,
                tipo_accion=tarea_original.tipo_accion,
                parametros=tarea_original.parametros,
                programado_para=siguiente_hora,
                repetir=tarea_original.repetir
            )
            print(f"[SCHEDULER] Siguiente instancia programada para {siguiente_hora}")

    @staticmethod
    def crear_tarea(perfil, titulo, tipo_accion, parametros, programado_para, repetir=None):
        """Método estático para crear una nueva tarea programada."""
        from asistente.models import TareaProgramada

        tarea = TareaProgramada.objects.create(
            perfil=perfil,
            titulo=titulo,
            tipo_accion=tipo_accion,
            parametros=parametros,
            programado_para=programado_para,
            repetir=repetir
        )
        return tarea

    @staticmethod
    def listar_tareas_pendientes(perfil=None):
        """Lista tareas pendientes, opcionalmente filtradas por perfil."""
        from asistente.models import TareaProgramada

        qs = TareaProgramada.objects.filter(estado='pendiente')
        if perfil:
            qs = qs.filter(perfil=perfil)
        return qs.order_by('programado_para')

    @staticmethod
    def cancelar_tarea(tarea_id):
        """Cancela una tarea específica."""
        from asistente.models import TareaProgramada

        try:
            tarea = TareaProgramada.objects.get(id=tarea_id)
            tarea.cancelar()
            return True, "Tarea cancelada"
        except TareaProgramada.DoesNotExist:
            return False, "Tarea no encontrada"


# Instancia global del scheduler
_scheduler_instance = None

def obtener_scheduler():
    """Retorna la instancia global del scheduler, la crea si no existe."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SchedulerService()
        _scheduler_instance.iniciar()
    return _scheduler_instance
