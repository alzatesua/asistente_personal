from django.contrib import admin
from .models import PerfilAsistente, Conversacion, Mensaje, ComandoEjecutado

@admin.register(PerfilAsistente)
class PerfilAdmin(admin.ModelAdmin):
    list_display = ['nombre_asistente', 'nombre_usuario', 'actualizado_en']

@admin.register(Conversacion)
class ConversacionAdmin(admin.ModelAdmin):
    list_display = ['numero_whatsapp', 'nombre_contacto', 'perfil', 'creada_en']

@admin.register(Mensaje)
class MensajeAdmin(admin.ModelAdmin):
    list_display = ['conversacion', 'tipo', 'origen', 'contenido', 'creado_en']
    list_filter = ['tipo', 'origen']

@admin.register(ComandoEjecutado)
class ComandoAdmin(admin.ModelAdmin):
    list_display = ['comando', 'exitoso', 'ejecutado_en']
    list_filter = ['exitoso']
