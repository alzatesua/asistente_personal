from django.contrib import admin
from .models import PerfilAsistente, Conversacion, Mensaje, ComandoEjecutado


@admin.register(PerfilAsistente)
class PerfilAdmin(admin.ModelAdmin):
    list_display = ['nombre_asistente', 'nombre_usuario', 'usuario', 'actualizado_en']
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
