from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('asistente', '0019_tareaprogramada'),
    ]

    operations = [
        migrations.AlterField(
            model_name='conversacion',
            name='numero_whatsapp',
            field=models.CharField(max_length=80),
        ),
    ]
