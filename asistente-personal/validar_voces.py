#!/usr/bin/env python3
"""
Script para validar que todas las dependencias de TTS estén instaladas.
"""
import sys
import os

def verificar_paquetes_python():
    """Verifica que los paquetes Python de TTS estén instalados."""
    paquetes = {
        'edge-tts': 'edge-tts (Microsoft Neural Voices)',
        'gtts': 'gTTS (Google Text-to-Speech)',
        'pyttsx3': 'pyttsx3 (Voces del sistema)',
        'deepgram': 'Deepgram SDK (Aura-2 Premium)',
        'piper': 'Piper TTS (Open Source)',
    }
    
    print("📦 Verificando paquetes Python...")
    print("-" * 50)
    
    instalados = {}
    for paquete, descripcion in paquetes.items():
        try:
            __import__(paquete.replace('-', '_'))
            print(f"✅ {descripcion}: INSTALADO")
            instalados[paquete] = True
        except ImportError:
            print(f"❌ {descripcion}: NO INSTALADO")
            instalados[paquete] = False
    
    return instalados

def verificar_modelos_piper():
    """Verifica que los modelos de Piper estén descargados."""
    print("\n🎙️ Verificando modelos de Piper TTS...")
    print("-" * 50)
    
    # Buscar el directorio de modelos
    posibles_rutas = [
        'piper_models',
        'asistente-personal/piper_models',
    ]
    
    piper_dir = None
    for ruta in posibles_rutas:
        if os.path.isdir(ruta):
            piper_dir = ruta
            break
    
    if not piper_dir:
        print("❌ Directorio piper_models no encontrado")
        return False
    
    print(f"📁 Directorio: {piper_dir}")
    
    # Modelo por defecto: es_ES-mls_10246-low
    modelo_nombre = 'es_ES-mls_10246-low'
    modelo_onnx = os.path.join(piper_dir, f'{modelo_nombre}.onnx')
    modelo_json = os.path.join(piper_dir, f'{modelo_nombre}.onnx.json')
    
    if os.path.exists(modelo_onnx) and os.path.exists(modelo_json):
        print(f"✅ Modelo {modelo_nombre}: DESCARGADO")
        return True
    else:
        print(f"❌ Modelo {modelo_nombre}: NO ENCONTRADO")
        if not os.path.exists(modelo_onnx):
            print(f"   Falta: {modelo_nombre}.onnx")
        if not os.path.exists(modelo_json):
            print(f"   Falta: {modelo_nombre}.onnx.json")
        return False

def verificar_voces_sistema():
    """Verifica voces del sistema para pyttsx3."""
    print("\n🔊 Verificando voces del sistema (pyttsx3)...")
    print("-" * 50)
    
    try:
        import pyttsx3
        engine = pyttsx3.init()
        voices = engine.getProperty('voices')
        
        if voices:
            print(f"✅ Voces disponibles: {len(voices)}")
            for voice in voices[:5]:  # Mostrar primeras 5
                nombre = voice.name
                id_voz = voice.id
                print(f"   - {nombre} ({id_voz})")
            if len(voices) > 5:
                print(f"   ... y {len(voices) - 5} más")
            return True
        else:
            print("❌ No se encontraron voces del sistema")
            return False
    except Exception as e:
        print(f"❌ Error al verificar voces: {e}")
        return False

def verificar_api_key_deepgram():
    """Verifica si hay API key de Deepgram configurada."""
    print("\n🔑 Verificando API Key de Deepgram...")
    print("-" * 50)
    
    try:
        from django.conf import settings
        api_key = getattr(settings, 'DEEPGRAM_API_KEY', None)
        
        if not api_key or api_key == 'tu_deepgram_api_key_aqui':
            print("⚠️ DEEPGRAM_API_KEY no configurada o es valor por defecto")
            print("   Obtén una en: https://console.deepgram.com/")
            return False
        else:
            print(f"✅ DEEPGRAM_API_KEY configurada (empieza con: {api_key[:10]}...)")
            return True
    except Exception as e:
        print(f"⚠️ No se pudo verificar (ejecuta desde Django): {e}")
        return None

def main():
    print("=" * 50)
    print("🔍 VALIDACIÓN DE DEPENDENCIAS TTS")
    print("=" * 50)
    
    # Verificar paquetes Python
    paquetes = verificar_paquetes_python()
    
    # Verificar modelos Piper
    piper_ok = verificar_modelos_piper()
    
    # Verificar voces del sistema
    sistema_ok = verificar_voces_sistema()
    
    # Verificar API key Deepgram (opcional)
    deepgram_ok = verificar_api_key_deepgram()
    
    # Resumen
    print("\n" + "=" * 50)
    print("📊 RESUMEN")
    print("=" * 50)
    
    todo_ok = all(paquetes.values()) and piper_ok and sistema_ok
    
    if todo_ok:
        print("✅ Todas las dependencias básicas están instaladas")
        if deepgram_ok is False:
            print("⚠️ Deepgram Aura-2 requiere API key (opcional, voces premium)")
    else:
        print("❌ Faltan algunas dependencias:")
        
        if not paquetes['edge-tts']:
            print("   - Instalar: pip install edge-tts")
        if not paquetes['gtts']:
            print("   - Instalar: pip install gtts")
        if not paquetes['pyttsx3']:
            print("   - Instalar: pip install pyttsx3")
        if not paquetes['deepgram']:
            print("   - Instalar: pip install deepgram-sdk")
        if not paquetes['piper']:
            print("   - Instalar: pip install piper-tts")
        if not piper_ok:
            print("   - Descargar modelos de Piper")
        if not sistema_ok:
            print("   - Instalar voces del sistema (espeak, mbrola, etc.)")
    
    return 0 if todo_ok else 1

if __name__ == '__main__':
    sys.exit(main())
