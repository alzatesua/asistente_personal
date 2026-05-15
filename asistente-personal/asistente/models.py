from django.db import models
from django.core.exceptions import ValidationError
from datetime import datetime

class PerfilAsistente(models.Model):
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
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Perfil del Asistente'

    def __str__(self):
        return f"{self.nombre_asistente} - {self.nombre_usuario}"


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
        if self.programado_para and self.programado_para <= datetime.now():
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
        self.ejecutado_en = datetime.now()
        self.save()

    def cancelar(self):
        """Cancelar la tarea."""
        self.estado = 'cancelada'
        self.save()
