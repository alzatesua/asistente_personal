# RESUMEN DE CAMBIOS - SISTEMA DE AGENDAMIENTO DE CITAS

## Fecha: 2026-05-22

## Problema Principal
El sistema no guardaba las citas agendadas desde WhatsApp/Facebook.

## Causas Identificadas

### 1. Tabla de Citas No Existía
- **Error**: `django.db.utils.OperationalError: no such table: asistente_cita`
- **Solución**: Ejecutar migraciones de Django
```bash
docker exec asistente-personal-django-1 python manage.py makemigrations asistente
docker exec asistente-personal-django-1 python manage.py migrate asistente
```

### 2. Configuración de WhatsApp Deshabilitada
- **Archivo**: `whatsapp_line_settings.json`
- **Cambio**: Habilitar `responder_chats` y `leer_chats` para la línea "v"

```json
{
  "v": {
    "responder_chats": true,
    "responder_grupos": false,
    "leer_chats": true,
    "leer_grupos": false,
    "responder_voz": false
  }
}
```

### 3. Patrones de Detección Limitados
- **Archivo**: `asistente/services.py`
- **Líneas**: 2044-2053

**Patrones agregados:**
```python
# Confirmaciones con fechas
r'\bme parece bien\b', r'\bme parece\b', r'\bestá bien\b', r'\bva bien\b'
r'\bperfecto\b', r'\bexcelente\b', r'\bgenial\b'
r'\bdía\b.*(lunes|martes|miércoles|jueves|viernes|sábado|domingo)'
r'\b(lunes|martes|miércoles|jueves|viernes|sábado|domingo).*\b(día|tarde|mañana|noche)\b'
r'\b(tipo|como|a las|a la)\s+\d{1,2}\s*(am|pm|de la mañana|de la tarde|de la noche)'
r'\b\d{1,2}\s*:\s*\d{2}\b'  # "14:30"
r'\b(podemos|podría|puedo)\s+(verse|verse|hablar|quedar)\b'
```

### 4. Función perfil_desde_linea_whatsapp
- **Archivo**: `asistente/views.py`
- **Líneas**: 137-144

**Problema**: Solo buscaba perfiles con usuario asociado

**Solución**:
```python
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
```

### 5. Extracción de Datos Mejorada
- **Archivo**: `asistente/services.py`
- **Líneas**: 2248-2307

**Mejoras:**
```python
# Detectar "mañana" con errores ortográficos
maniana_pattern = re.search(r'\b(mañana|mañana|malana|manana|mnañana)\b', mensaje_lower, re.IGNORECASE)

# Detectar horas con AM/PM
tarde_match = re.search(r'(?:tipo|a las?|por la|en la)\s+(\d{1,2})\s*(?:de la)?\s*(tarde|noche|mañana|pm|am)', mensaje_lower)
```

### 6. Webhook de Facebook
- **Archivo**: `asistente/views.py`
- **Líneas**: 1972-2014

**Cambio**: Agregada detección de citas al webhook de Facebook (similar a WhatsApp)

## Archivos Modificados

1. `/home/dagi/whats-app/dagi/asistente-personal/whatsapp_line_settings.json`
2. `/home/dagi/whats-app/dagi/asistente-personal/asistente/views.py`
3. `/home/dagi/whats-app/dagi/asistente-personal/asistente/services.py`

## Comandos Ejecutados

```bash
# Crear migración para tabla de citas
docker exec asistente-personal-django-1 python manage.py makemigrations asistente

# Aplicar migración
docker exec asistente-personal-django-1 python manage.py migrate asistente

# Reiniciar contenedor
docker restart asistente-personal-django-1
```

## Funcionalidad Actual

El sistema ahora:
1. ✅ Detecta intenciones de agendamiento en mensajes
2. ✅ Extrae fecha, hora y detalles automáticamente
3. ✅ Maneja errores ortográficos comunes ("malana" → "mañana")
4. ✅ Detecta formatos de hora variados ("2 pm", "tipo 3 de la tarde", "14:30")
5. ✅ Crea la cita en la base de datos
6. ✅ Envía confirmación al usuario
7. ✅ Programa recordatorio automático (5 minutos antes)

## Mensajes de Prueba que Funcionan

- "Mañana a las 2 pm"
- "Malana a las 3 pm" (con error ortográfico)
- "Me parece bien el día lunes, tipo 2 de la tarde"
- "A las 10 am de la mañana"
- "Mañana tipo 4 de la tarde"
- "Hola, quiero agendar una cita para el martes a las 3pm"

## Notas Importantes

- El contenedor Docker debe estar corriendo: `asistente-personal-django-1`
- La base de datos SQLite está en: `/app/db/db.sqlite3`
- Para reiniciar el servicio: `docker restart asistente-personal-django-1`
