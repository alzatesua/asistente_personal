from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Crea/actualiza el unico usuario autorizado y permite activar o desactivar su cuenta.'

    def add_arguments(self, parser):
        parser.add_argument('--username', required=True, help='Nombre del unico usuario autorizado.')
        parser.add_argument('--password', help='Nueva contrasena. Si se omite, conserva la actual.')
        parser.add_argument(
            '--active',
            choices=['yes', 'no'],
            default='yes',
            help='Activa o desactiva la cuenta del usuario.',
        )
        parser.add_argument(
            '--staff',
            action='store_true',
            help='Tambien habilita acceso al admin de Django para este usuario.',
        )

    def handle(self, *args, **options):
        username = options['username'].strip()
        password = options.get('password')
        is_active = options['active'] == 'yes'

        if not username:
            raise CommandError('El username no puede estar vacio.')

        user, created = User.objects.get_or_create(username=username)
        if password:
            user.set_password(password)
        elif created:
            raise CommandError('Debes indicar --password cuando creas el usuario por primera vez.')

        user.is_active = is_active
        user.is_staff = bool(options['staff'])
        user.is_superuser = bool(options['staff'])
        user.save()

        otros = User.objects.exclude(pk=user.pk)
        otros_actualizados = otros.update(is_active=False, is_staff=False, is_superuser=False)

        estado = 'activa' if is_active else 'desactivada'
        accion = 'creada' if created else 'actualizada'
        self.stdout.write(self.style.SUCCESS(
            f'Cuenta {accion}: {username} ({estado}). '
            f'Otros usuarios desactivados: {otros_actualizados}.'
        ))
