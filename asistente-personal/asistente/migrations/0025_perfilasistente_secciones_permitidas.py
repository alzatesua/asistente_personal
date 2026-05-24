from django.db import migrations, models
import asistente.models


class Migration(migrations.Migration):

    dependencies = [
        ('asistente', '0024_cita_unique_active_slot'),
    ]

    operations = [
        migrations.AddField(
            model_name='perfilasistente',
            name='secciones_permitidas',
            field=models.JSONField(blank=True, default=asistente.models.secciones_permitidas_default),
        ),
    ]
