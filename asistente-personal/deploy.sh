#!/bin/bash

# Script de despliegue para asistente-dago.ia.dagi.co

set -e

DOMAIN="asistente-dago.ia.dagi.co"
EMAIL=""  # Agregar email para Let's Encrypt

echo "=== Iniciando despliegue ==="

# 1. Crear archivo .env si no existe
if [ ! -f .env ]; then
    echo "Creando .env desde .env.production..."
    cp .env.production .env
    echo "⚠️  EDITA .env y agrega tus API keys antes de continuar"
    echo "Presiona Enter cuando hayas editado el archivo..."
    read
fi

# 2. Crear directorios necesarios
mkdir -p staticfiles media

# 3. Iniciar servicios
echo "Iniciando contenedores..."
docker-compose up -d django redis baileys nginx

# 4. Esperar a que nginx esté listo
sleep 5

# 5. Obtener certificado SSL
if [ -z "$EMAIL" ]; then
    echo "⚠️  Agrega tu email en el script deploy.sh para Let's Encrypt"
    echo "Certificado NO generado. Ejecuta manualmente:"
    echo "docker-compose run --rm certbot certonly --webroot --webroot-path=/var/www/certbot --email TU_EMAIL --agree-tos --no-eff-email -d $DOMAIN"
else
    docker-compose run --rm certbot certonly --webroot --webroot-path=/var/www/certbot --email $EMAIL --agree-tos --no-eff-email -d $DOMAIN
    docker-compose restart nginx
fi

echo "=== Despliegue completado ==="
echo "Tu aplicación debería estar en: https://$DOMAIN"
