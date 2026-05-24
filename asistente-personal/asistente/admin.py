from django.contrib import admin
from .models import PerfilAsistente, Conversacion, Mensaje, ComandoEjecutado, TareaProgramada, Cita


@admin.register(PerfilAsistente)
class PerfilAdmin(admin.ModelAdmin):
    list_display = [
        'nombre_asistente',
        'nombre_usuario',
        'usuario',
        'usar_groq_respuestas_normales',
        'usar_groq_lexico_complejo',
        'secciones_permitidas',
        'actualizado_en',
    ]
    search_fields = ['nombre_asistente', 'nombre_usuario', 'usuario__username', 'usuario__email']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(usuario=request.user)


@admin.register(Conversacion)
class ConversacionAdmin(admin.ModelAdmin):
    list_display = ['numero_whatsapp', 'nombre_contacto', 'perfil', 'creada_en']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(perfil__usuario=request.user)


@admin.register(Mensaje)
class MensajeAdmin(admin.ModelAdmin):
    list_display = ['conversacion', 'tipo', 'origen', 'contenido', 'creado_en']
    list_filter = ['tipo', 'origen']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(conversacion__perfil__usuario=request.user)


@admin.register(ComandoEjecutado)
class ComandoAdmin(admin.ModelAdmin):
    list_display = ['comando', 'exitoso', 'ejecutado_en']
    list_filter = ['exitoso']

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(mensaje__conversacion__perfil__usuario=request.user)


@admin.register(TareaProgramada)
class TareaProgramadaAdmin(admin.ModelAdmin):
    list_display = ['titulo', 'tipo_accion', 'estado', 'programado_para', 'perfil']
    list_filter = ['tipo_accion', 'estado', 'creado_en']
    search_fields = ['titulo', 'perfil__nombre_usuario']
    date_hierarchy = 'programado_para'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(perfil__usuario=request.user)


@admin.register(Cita)
class CitaAdmin(admin.ModelAdmin):
    list_display = ['titulo', 'fecha_hora', 'estado', 'ubicacion', 'perfil']
    list_filter = ['estado', 'tipo_ubicacion', 'creado_en']
    search_fields = ['titulo', 'descripcion', 'ubicacion', 'perfil__nombre_usuario']
    date_hierarchy = 'fecha_hora'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(perfil__usuario=request.user)
