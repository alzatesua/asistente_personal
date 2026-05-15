# Generated manually for GonzaloNeural voice

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('asistente', '0005_alter_perfilasistente_voz_preferida'),
    ]

    operations = [
        migrations.AlterField(
            model_name='perfilasistente',
            name='voz_preferida',
            field=models.CharField(default='es-CO-GonzaloNeural', help_text='Código de voz TTS', max_length=100),
        ),
    ]
