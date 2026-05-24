from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('asistente', '0025_perfilasistente_secciones_permitidas'),
    ]

    operations = [
        migrations.AddField(
            model_name='perfilasistente',
            name='usar_groq_respuestas_normales',
            field=models.BooleanField(default=False, help_text='Usar Groq para respuestas normales del bot'),
        ),
        migrations.AddField(
            model_name='perfilasistente',
            name='usar_groq_lexico_complejo',
            field=models.BooleanField(default=False, help_text='Escalar a Groq para respuestas complejas o cuando el modelo base responda debil'),
        ),
    ]
