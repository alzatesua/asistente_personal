#!/usr/bin/env python
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from asistente.services import TTSService

def test_deepgram():
    """Prueba las voces de Deepgram Aura-2"""
    tts = TTSService()

    # Texto de prueba
    texto = "Hola, soy una prueba de voz de Deepgram Aura-2. ¿Cómo estás?"

    # Voces de Deepgram a probar
    voces_prueba = [
        'aura-2-celeste-es',  # Mujer, Colombia
        'aura-2-estrella-es',  # Mujer, México
        'aura-2-nestor-es',    # Hombre, España
        'aura-2-javier-es',    # Hombre, Latino
    ]

    print("=" * 60)
    print("PRUEBA DE DEEPGRAM AURA-2")
    print("=" * 60)

    for voz in voces_prueba:
        print(f"\n🎤 Probando voz: {voz}")
        try:
            audio_url = tts.generar_audio(texto, voz=voz)
            print(f"✅ Exitoso: {audio_url}")
        except Exception as e:
            print(f"❌ Error: {e}")

    print("\n" + "=" * 60)
    print("PRUEBA COMPLETADA")
    print("=" * 60)

if __name__ == '__main__':
    test_deepgram()
