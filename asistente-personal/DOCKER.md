# Despliegue con Docker - asistente-dago.ia.dagi.co

## Arquitectura

```
┌─────────────────────────────────────────────────┐
│                    Nginx                         │
│  (HTTPS con Let's Encrypt + Proxy Reverse)      │
└──────────────┬───────────────┬──────────────────┘
               │               │
               ▼               ▼
    ┌──────────────────┐  ┌─────────────────┐
    │     Django       │  │    Baileys      │
    │  (Python 3.12)   │  │  (Node.js 20)   │
    │   Puerto 8000    │  │   Puerto 3002   │
    └────────┬─────────┘  └─────────────────┘
             │
             ▼
    ┌──────────────────┐
    │     Redis        │
    │    (Celery)      │
    │   Puerto 6379    │
    └──────────────────┘
```

## Pasos de Despliegue

### 1. Configurar variables de entorno

```bash
cp .env.production .env
nano .env  # Editar con tus API keys
```

**Variables requeridas:**
- `SECRET_KEY`: Clave secreta de Django (genera una con: `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`)
- `ZAI_API_KEY`: Tu API key de Z.AI
- `DEEPGRAM_API_KEY`: Tu API key de Deepgram (opcional)

### 2. Obtener certificado SSL (primera vez)

```bash
# Asegúrate de que el dominio apunta a tu servidor
docker-compose up -d nginx

# Obtener certificado
docker-compose run --rm certbot certonly --webroot \
  --webroot-path=/var/www/certbot \
  --email tu-email@example.com \
  --agree-tos \
  --no-eff-email \
  -d asistente-dago.ia.dagi.co

# Reiniciar nginx
docker-compose restart nginx
```

### 3. Iniciar todos los servicios

```bash
docker-compose up -d
```

### 4. Comandos útiles

```bash
# Ver logs
docker-compose logs -f django
docker-compose logs -f baileys

# Reiniciar un servicio
docker-compose restart django

# Ejecutar migraciones
docker-compose exec django python manage.py migrate

# Recolectar archivos estáticos
docker-compose exec django python manage.py collectstatic --noinput

# Actualizar la aplicación
git pull
docker-compose build
docker-compose up -d
```

## Puertos utilizados

- **80**: HTTP (redirige a HTTPS)
- **443**: HTTPS
- **3002**: Baileys (interno)
- **8000**: Django (interno)
- **6379**: Redis (interno)

## Dominio

- **Producción**: https://asistente-dago.ia.dagi.co
- **Endpoint Baileys**: https://asistente-dago.ia.dagi.co/baileys/

## Troubleshooting

### Certificado SSL no se renueva

```bash
docker-compose exec certbot certbot renew
docker-compose restart nginx
```

### Error de conexión a Baileys

Verifica que `DJANGO_WEBHOOK_URL` apunte a `https://asistente-dago.ia.dagi.co/webhook/whatsapp/`

### Archivos estáticos no cargan

```bash
docker-compose exec django python manage.py collectstatic --noinput
```
