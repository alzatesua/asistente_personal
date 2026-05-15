#!/usr/bin/env python3
"""
Launcher para la ventana flotante del asistente
"""

import sys
import os

# Agregar el directorio actual al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ventana_flotante import VentanaAsistente

if __name__ == "__main__":
    try:
        print("🚀 Iniciando ventana flotante del asistente...")
        print("   - Presiona ESC para cerrar")
        print("   - Arrastra la ventana desde el avatar")
        print("   - Haz clic en ⌄ para expandir el input")
        app = VentanaAsistente()
        app.run()
    except KeyboardInterrupt:
        print("\n👋 Ventana cerrada")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
