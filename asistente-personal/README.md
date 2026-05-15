# Asistente Personal de Desarrollo

Asistente personal construido con Django que integra chat web, ventana flotante de escritorio, tareas de desarrollo, texto a voz y conexión con WhatsApp mediante Baileys.

El asistente usa la API de Z.AI compatible con Anthropic para generar respuestas y puede mantener memoria por sesión, responder mensajes de WhatsApp, transcribir notas de voz con Deepgram y enviar respuestas como texto o notas de voz según la configuración de cada línea.

## Características principales

- Dashboard web con resumen de actividad.
- Chat directo con memoria por sesión de navegador.
- Ventana flotante local para interactuar con el asistente desde el escritorio.
- Canal WhatsApp multi-línea usando Baileys.
- Memoria separada por línea y contacto de WhatsApp.
- Inicio automático de Baileys desde Django al conectar una línea.
- Recuperación de sesiones reales de WhatsApp desde `baileys_sessions`.
- Botón `Iniciar todo` para levantar todas las líneas guardadas.
- Eliminación definitiva de una línea con borrado de sesión, configuración y memoria.
- Controles por línea para respuestas automáticas en chats y grupos.
- Controles por línea para lectura en voz alta.
- Opción por línea para responder con notas de voz.
- Soporte para audios largos divididos en partes.
- Transcripción de notas de voz entrantes con Deepgram.
- Tareas en segundo plano y comandos de desarrollo.
- Búsqueda web asistida desde el backend.
- TTS con Deepgram Aura, Piper, Edge-TTS, gTTS y pyttsx3 como respaldo.

## Estructura general

```text
asistente-personal/
├── asistente/                 # Aplicación principal Django
├── baileys-service/           # Servicio Node.js para WhatsApp/Baileys
├── core/                      # Configuración del proyecto Django
├── media/                     # Audios y archivos generados/subidos
├── piper_models/              # Modelos locales de Piper TTS
├── static/                    # Archivos estáticos
├── templates/                 # Plantillas HTML
├── audio_visual_state.py      # Estado compartido para visualización de audio
├── background_tasks.json      # Persistencia simple de tareas en segundo plano
├── db.sqlite3                 # Base de datos local SQLite
├── manage.py                  # CLI Django
├── run_ventana.py             # Lanzador de ventana flotante
├── ventana_flotante.py        # UI flotante de escritorio
└── whatsapp_line_settings.json
```

## Requisitos

- Python 3.12 o compatible con Django 6.
- Node.js y npm para `baileys-service`.
- SQLite, incluido por defecto con Python.
- Opcional: Redis si se desea usar Celery con broker externo.
- Opcional: reproductores locales como `mpg123`, `ffplay`, `paplay`, `aplay` o soporte de `pygame`.

## Instalación

Desde la raíz del proyecto:

```bash
cd /home/juan/Distritec/private/asistente-personal
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Instalar dependencias de Baileys:

```bash
cd /home/juan/Distritec/private/asistente-personal/baileys-service
npm install
```

Volver a la raíz:

```bash
cd /home/juan/Distritec/private/asistente-personal
```

## Variables de entorno

El proyecto carga variables desde `.env` en la raíz de Django. No subas este archivo si contiene credenciales reales.

Variables principales:

```env
SECRET_KEY=...
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,0.0.0.0

ZAI_API_KEY=...
ZAI_BASE_URL=...
ZAI_MODEL=glm-4

BAILEYS_SERVICE_URL=http://localhost:3002
BAILEYS_WEBHOOK_SECRET=...
DJANGO_WEBHOOK_URL=http://localhost:8005/webhook/whatsapp/

DEEPGRAM_API_KEY=...

TTS_LANG=es
TTS_TLD=com
TTS_FALLBACK_VOICE=piper:es_ES-mls_10246-low

REDIS_URL=redis://localhost:6379/0
```

Variables útiles de WhatsApp:

```env
WHATSAPP_DEFAULT_COUNTRY_CODE=57
WHATSAPP_BULK_MAX_RECIPIENTS=50
WHATSAPP_BULK_DELAY_MIN=1.0
WHATSAPP_BULK_DELAY_MAX=1.6
DATA_UPLOAD_MAX_MEMORY_SIZE=26214400
```

## Base de datos

Aplicar migraciones:

```bash
source venv/bin/activate
python manage.py migrate
```

Crear usuario administrador si hace falta:

```bash
python manage.py createsuperuser
```

## Ejecutar Django

El proyecto usa el puerto `8005` por defecto cuando se ejecuta `runserver` sin puerto.

```bash
cd /home/juan/Distritec/private/asistente-personal
source venv/bin/activate
python manage.py runserver
```

También se puede indicar el puerto explícitamente:

```bash
python manage.py runserver 8005
```

URLs principales:

- Dashboard: `http://localhost:8005/`
- Chat: `http://localhost:8005/chat/`
- Tareas: `http://localhost:8005/tareas/`
- WhatsApp: `http://localhost:8005/whatsapp/`
- Voz: `http://localhost:8005/voz/`
- Configuración: `http://localhost:8005/configurar/`

## WhatsApp y Baileys

Baileys no necesita iniciarse manualmente para el flujo normal. Django lo arranca bajo demanda cuando se presiona `Conectar` en `/whatsapp/`.

El servicio queda escuchando en:

```text
http://localhost:3002
```

Flujo recomendado:

1. Inicia Django en `8005`.
2. Abre `/whatsapp/`.
3. Agrega una línea o espera a que se recuperen las sesiones guardadas.
4. Presiona `Conectar` en una línea o `Iniciar todo`.
5. Escanea el QR si la sesión no está vinculada.

La pantalla de WhatsApp puede:

- Recuperar sesiones reales desde `baileys-service/baileys_sessions`.
- Mostrar estado por línea.
- Mostrar QR cuando sea necesario.
- Configurar respuestas automáticas en chats y grupos.
- Activar o desactivar lectura en voz alta.
- Activar o desactivar respuestas como nota de voz.
- Borrar memoria conversacional.
- Borrar sesión del teléfono.
- Eliminar definitivamente una línea con el botón de basurita.

La eliminación definitiva de una línea borra:

- Sesión remota en Baileys.
- Carpeta local de credenciales.
- Configuración en `whatsapp_line_settings.json`.
- Conversaciones y mensajes asociados a `linea:*`.
- Referencia local en el navegador.

## Ventana flotante

Para abrir la ventana flotante:

```bash
cd /home/juan/Distritec/private/asistente-personal
source venv/bin/activate
python run_ventana.py
```

La ventana usa el backend local en `http://localhost:8005`.

## Texto a voz

El sistema puede generar audio con varios proveedores:

- Deepgram Aura, si `DEEPGRAM_API_KEY` está configurada.
- Piper TTS local.
- Edge-TTS.
- gTTS.
- pyttsx3 como último respaldo.

Los textos largos se dividen automáticamente en partes para evitar cortes bruscos y permitir reproducción o envío secuencial.

## Endpoints útiles

WhatsApp:

```text
POST /api/whatsapp/conectar-linea/
GET  /api/whatsapp/estado-linea/<linea>/
GET  /api/whatsapp/qr-linea/<linea>/
POST /api/whatsapp/desconectar-linea/<linea>/
GET  /api/whatsapp/sesiones/
POST /api/whatsapp/iniciar-todo/
POST /api/whatsapp/borrar-memoria/
POST /api/whatsapp/borrar-sesion/
POST /api/whatsapp/eliminar-linea/
GET  /api/whatsapp/config-lineas/
POST /api/whatsapp/config-lineas/
GET  /api/whatsapp/estadisticas/
```

Chat y mensajes:

```text
POST /api/chat/
GET  /api/chat/historial/
GET  /api/mensajes/
```

Voz:

```text
GET  /api/voces/
GET  /api/voz/actual/
POST /api/voz/actualizar/
```

Tareas y acciones:

```text
GET  /api/tareas/
GET  /api/tareas/<tarea_id>/
GET  /api/tareas/<tarea_id>/resumen-voz/
POST /api/acciones/ejecutar/
POST /api/terminal/ejecutar/
GET  /api/terminal/permisos/
POST /api/devtools/
```

## Comandos internos del asistente

El asistente reconoce comandos de desarrollo como:

```text
/git status
/git add
/git commit <mensaje>
/git push
/docker ps
/docker up
/docker down
/crear django <nombre>
/crear react <nombre>
/abrir code <ruta>
/pc diagnostico
/cmd <comando>
/bg <comando>
/web <consulta>
```

En WhatsApp, el asistente responde como contacto externo y no debe exponer comandos internos.

## Archivos generados y datos locales

Estos archivos contienen estado local o datos sensibles:

```text
.env
db.sqlite3
media/
background_tasks.json
whatsapp_line_settings.json
baileys-service/.env
baileys-service/baileys_sessions/
```

Evita compartirlos si contienen credenciales, sesiones reales o datos personales.

## Verificación rápida

Comprobar configuración Django:

```bash
source venv/bin/activate
python manage.py check
```

Probar que Django responde:

```bash
curl -I http://localhost:8005/whatsapp/
```

Probar sesiones de WhatsApp desde Django:

```bash
curl http://localhost:8005/api/whatsapp/sesiones/
```

## Notas de seguridad

- Mantén `BAILEYS_WEBHOOK_SECRET` sincronizado entre Django y Baileys.
- No publiques sesiones de `baileys_sessions`.
- No publiques `.env`.
- Revisa permisos antes de habilitar comandos libres con `/cmd`.
- Usa `DEBUG=False` y configura `ALLOWED_HOSTS` antes de exponer el servicio fuera de la máquina local.
