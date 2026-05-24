from django.db import migrations, models
from django.db.models import Q


def cancelar_citas_duplicadas_exactas(apps, schema_editor):
    Cita = apps.get_model('asistente', 'Cita')
    citas_activas = (
        Cita.objects
        .exclude(estado='cancelada')
        .order_by('perfil_id', 'fecha_hora', 'creado_en', 'id')
    )

    citas_vistas = set()
    ids_duplicados = []

    for cita in citas_activas.iterator():
        clave = (cita.perfil_id, cita.fecha_hora)
        if clave in citas_vistas:
            ids_duplicados.append(cita.id)
        else:
            citas_vistas.add(clave)

    if ids_duplicados:
        Cita.objects.filter(id__in=ids_duplicados).update(estado='cancelada')


class Migration(migrations.Migration):

    dependencies = [
        ('asistente', '0023_cita'),
    ]

    operations = [
        migrations.RunPython(cancelar_citas_duplicadas_exactas, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='cita',
            constraint=models.UniqueConstraint(
                condition=~Q(estado='cancelada'),
                fields=('perfil', 'fecha_hora'),
                name='cita_unica_por_perfil_fecha_hora_activa',
            ),
        ),
    ]
