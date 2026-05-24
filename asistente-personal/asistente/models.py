from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone
from datetime import timedelta


SECCIONES_PERMITIDAS_DEFAULT = [
    'chat',
    'tareas',
    'citas',
    'whatsapp',
    'facebook',
    'voz',
    'configurar',
]


SECCIONES_DISPONIBLES = [
    ('chat', 'Chat'),
    ('tareas', 'Tareas'),
    ('citas', 'Citas'),
    ('whatsapp', 'WhatsApp'),
    ('facebook', 'Facebook'),
    ('voz', 'Voz'),
    ('configurar', 'Perfil'),
    ('usuarios', 'Usuarios'),
]


def secciones_permitidas_default():
    return list(SECCIONES_PERMITIDAS_DEFAULT)


class PerfilAsistente(models.Model):
    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='perfil_asistente',
        null=True,
        blank=True,
    )
    nombre_asistente = models.CharField(max_length=100, default='Asistente')
    nombre_usuario = models.CharField(max_length=100)
    cv_archivo = models.FileField(upload_to='cvs/', null=True, blank=True)
    cv_texto = models.TextField(null=True, blank=True)
    prompt_sistema = models.TextField(null=True, blank=True)
    voz_preferida = models.CharField(
        max_length=100,
        default='aura-2-celeste-es',
        help_text='Código de voz TTS (Deepgram Aura-2, Piper gratis, Edge-TTS, u Offline)'
    )
    voz_velocidad = models.FloatField(default=1.0, help_text='Velocidad de reproducción (0.5 - 2.0)')
    comandos_personalizados = models.TextField(null=True, blank=True, help_text='JSON con comandos adicionales permitidos')
    meta_graph_api_version = models.CharField(max_length=20, default='v25.0', blank=True)
    meta_page_id = models.CharField(max_length=100, blank=True, default='')
    meta_page_access_token = models.TextField(blank=True, default='')
    meta_app_secret = models.CharField(max_length=255, blank=True, default='')
    meta_verify_token = models.CharField(max_length=255, blank=True, default='')
    meta_webhook_url = models.URLField(max_length=500, blank=True, default='')
    secciones_permitidas = models.JSONField(default=secciones_permitidas_default, blank=True)
    usar_groq_respuestas_normales = models.BooleanField(
        default=False,
        help_text='Usar Groq para respuestas normales del bot'
    )
    usar_groq_lexico_complejo = models.BooleanField(
        default=False,
        help_text='Escalar a Groq para respuestas complejas o cuando el modelo base responda debil'
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Perfil del Asistente'
        verbose_name_plural = 'Perfiles del Asistente'

    def __str__(self):
        return f"{self.nombre_asistente} - {self.nombre_usuario}"

    def puede_ver_seccion(self, seccion):
        return seccion in (self.secciones_permitidas or [])


class Conversacion(models.Model):
    perfil = models.ForeignKey(PerfilAsistente, on_delete=models.CASCADE, related_name='conversaciones')
    numero_whatsapp = models.CharField(max_length=80)
    nombre_contacto = models.CharField(max_length=100, null=True, blank=True)
    creada_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.nombre_contacto or self.numero_whatsapp}"


class Mensaje(models.Model):
    TIPO_CHOICES = [
        ('texto', 'Texto'),
        ('voz', 'Voz'),
        ('imagen', 'Imagen'),
    ]
    ORIGEN_CHOICES = [
        ('entrante', 'Entrante'),
        ('saliente', 'Saliente'),
    ]

    conversacion = models.ForeignKey(Conversacion, on_delete=models.CASCADE, related_name='mensajes')
    tipo = models.CharField(max_length=10, choices=TIPO_CHOICES, default='texto')
    origen = models.CharField(max_length=10, choices=ORIGEN_CHOICES)
    contenido = models.TextField()
    audio_url = models.CharField(max_length=500, null=True, blank=True)
    leido = models.BooleanField(default=False)
    respondido = models.BooleanField(default=False)
    creado_en = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['creado_en']

    def __str__(self):
        return f"[{self.origen}] {self.tipo}: {self.contenido[:50]}"


class ComandoEjecutado(models.Model):
    mensaje = models.ForeignKey(Mensaje, on_delete=models.CASCADE, related_name='comandos')
    comando = models.TextField()
    resultado = models.TextField(null=True, blank=True)
    exitoso = models.BooleanField(default=False)
    ejecutado_en = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{'✅' if self.exitoso else '❌'} {self.comando[:60]}"


class TareaProgramada(models.Model):
    """Modelo para alarmas, recordatorios y tareas programadas en segundo plano."""

    TIPO_ACCION_CHOICES = [
        ('recordatorio', 'Recordatorio simple'),
        ('whatsapp', 'Enviar mensaje WhatsApp'),
        ('comando_pc', 'Ejecutar comando PC'),
        ('url', 'Abrir URL'),
        ('sistema', 'Acción de sistema (bloquear, suspender, etc.)'),
    ]

    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('ejecutando', 'Ejecutando'),
        ('completada', 'Completada'),
        ('fallida', 'Fallida'),
        ('cancelada', 'Cancelada'),
    ]

    perfil = models.ForeignKey(PerfilAsistente, on_delete=models.CASCADE, related_name='tareas_programadas', null=True, blank=True)
    titulo = models.CharField(max_length=200)
    tipo_accion = models.CharField(max_length=20, choices=TIPO_ACCION_CHOICES)

    # JSON con parámetros específicos según el tipo de acción
    # Para whatsapp: {"numero": "573001234567", "mensaje": "Hola"}
    # Para comando_pc: {"comando": "codigo ..."}
    # Para url: {"url": "https://google.com"}
    # Para sistema: {"accion": "bloquear|suspender|apagar|reiniciar"}
    parametros = models.JSONField(default=dict)

    programado_para = models.DateTimeField(db_index=True)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')

    # Resultado de la ejecución
    resultado = models.TextField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)

    # Timestamps
    creado_en = models.DateTimeField(auto_now_add=True)
    ejecutado_en = models.DateTimeField(null=True, blank=True)

    # Repetición opcional
    repetir = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text='Patrón de repetición: "diario", "semanal", "mensual", o expresión cron'
    )

    class Meta:
        ordering = ['programado_para']
        verbose_name = 'Tarea Programada'
        verbose_name_plural = 'Tareas Programadas'
        indexes = [
            models.Index(fields=['estado', 'programado_para']),
        ]

    def __str__(self):
        return f"[{self.get_tipo_accion_display()}] {self.titulo} - {self.programado_para.strftime('%Y-%m-%d %H:%M')}"

    def clean(self):
        """Validar que programado_para sea en el futuro y parámetros según tipo."""
        if self.programado_para and self.programado_para <= timezone.now():
            raise ValidationError({'programado_para': 'La hora debe ser en el futuro'})

        # Validar parámetros según tipo
        if self.tipo_accion == 'whatsapp':
            if 'numero' not in self.parametros or 'mensaje' not in self.parametros:
                raise ValidationError({'parametros': 'Para whatsapp se requiere "numero" y "mensaje"'})
        elif self.tipo_accion == 'url':
            if 'url' not in self.parametros:
                raise ValidationError({'parametros': 'Para url se requiere "url"'})
        elif self.tipo_accion in ['comando_pc', 'sistema']:
            if 'comando' not in self.parametros and 'accion' not in self.parametros:
                raise ValidationError({'parametros': f'Para {self.tipo_accion} se requiere "comando" o "accion"'})

    def marcar_ejecutada(self, exitoso=True, resultado=None, error=None):
        """Marcar la tarea como ejecutada."""
        self.estado = 'completada' if exitoso else 'fallida'
        self.resultado = resultado
        self.error = error
        self.ejecutado_en = timezone.now()
        self.save()

    def cancelar(self):
        """Cancelar la tarea."""
        self.estado = 'cancelada'
        self.save()


class Cita(models.Model):
    """Modelo para citas, reuniones y agendamientos."""

    ESTADO_CHOICES = [
        ('pendiente', 'Pendiente'),
        ('confirmada', 'Confirmada'),
        ('completada', 'Completada'),
        ('cancelada', 'Cancelada'),
    ]

    TIPO_UBICACION_CHOICES = [
        ('presencial', 'Presencial'),
        ('virtual', 'Virtual'),
        ('telefonica', 'Telefónica'),
    ]

    perfil = models.ForeignKey(PerfilAsistente, on_delete=models.CASCADE, related_name='citas')
    conversacion = models.ForeignKey(Conversacion, on_delete=models.CASCADE, related_name='citas', null=True, blank=True)

    # Información básica
    titulo = models.CharField(max_length=200)
    descripcion = models.TextField(null=True, blank=True)

    # Fecha y hora
    fecha_hora = models.DateTimeField(db_index=True)
    duracion_minutos = models.PositiveIntegerField(default=60, help_text='Duración en minutos')

    # Ubicación
    ubicacion = models.CharField(max_length=200, null=True, blank=True)
    tipo_ubicacion = models.CharField(max_length=20, choices=TIPO_UBICACION_CHOICES, default='presencial')

    # Estado y recordatorios
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default='pendiente')
    recordatorio_enviado = models.BooleanField(default=False)
    recordatorio_minutos_antes = models.PositiveIntegerField(default=5)

    # Metadatos
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    # Tarea de recordatorio asociada
    tarea_recordatorio = models.ForeignKey(TareaProgramada, on_delete=models.SET_NULL, null=True, blank=True, related_name='cita_asociada')

    class Meta:
        ordering = ['fecha_hora']
        verbose_name = 'Cita'
        verbose_name_plural = 'Citas'
        indexes = [
            models.Index(fields=['estado', 'fecha_hora']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['perfil', 'fecha_hora'],
                condition=~Q(estado='cancelada'),
                name='cita_unica_por_perfil_fecha_hora_activa',
            ),
        ]

    def __str__(self):
        fecha_local = timezone.localtime(self.fecha_hora)
        return f"[{self.get_estado_display()}] {self.titulo} - {fecha_local.strftime('%Y-%m-%d %H:%M')}"

    def marcar_confirmada(self):
        """Marcar la cita como confirmada."""
        self.estado = 'confirmada'
        self.save()

    def marcar_completada(self):
        """Marcar la cita como completada."""
        self.estado = 'completada'
        self.save()

    def marcar_cancelada(self):
        """Marcar la cita como cancelada."""
        self.estado = 'cancelada'
        # Cancelar también la tarea de recordatorio si existe
        if self.tarea_recordatorio and self.tarea_recordatorio.estado == 'pendiente':
            self.tarea_recordatorio.cancelar()
        self.save()

    def crear_recordatorio(self):
        """Crear tarea de recordatorio automático."""
        from asistente.services import SchedulerService

        if self.tarea_recordatorio:
            return self.tarea_recordatorio

        scheduler = SchedulerService()
        hora_recordatorio = self.fecha_hora - timedelta(minutes=self.recordatorio_minutos_antes)

        # Obtener número de WhatsApp de la conversación
        numero_whatsapp = None
        if self.conversacion:
            numero_whatsapp = self.conversacion.numero_whatsapp

        # Extraer línea si el número tiene formato "linea:numero"
        linea = 'principal'
        if numero_whatsapp and ':' in numero_whatsapp:
            partes = numero_whatsapp.split(':')
            linea = partes[0]
            numero_whatsapp = partes[1] if len(partes) > 1 else partes[0]

        fecha_local = timezone.localtime(self.fecha_hora)
        mensaje_recordatorio = (
            f"📅 Recordatorio de cita: {self.titulo}\n"
            f"🕐 En {self.recordatorio_minutos_antes} minutos ({fecha_local.strftime('%d/%m %H:%M')})\n"
        )
        if self.ubicacion:
            mensaje_recordatorio += f"📍 {self.ubicacion}\n"

        tarea = TareaProgramada.objects.create(
            perfil=self.perfil,
            titulo=f"Recordatorio: {self.titulo}",
            tipo_accion='whatsapp',
            parametros={
                'numero': numero_whatsapp or '',
                'mensaje': mensaje_recordatorio,
                'linea': linea,
            },
            programado_para=hora_recordatorio,
            estado='pendiente',
        )

        self.tarea_recordatorio = tarea
        self.save()
        return tarea
