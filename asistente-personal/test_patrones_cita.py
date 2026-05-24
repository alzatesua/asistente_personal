#!/usr/bin/env python3
"""Script para probar los patrones de detección de citas."""

import re

PATRONES_AGENDAMIENTO = [
    r'\bagendar\b', r'\bagenda\b', r'\bprogramar\b', r'\bprograma\b',
    r'\breservar\b', r'\breserva\b', r'\bcita\b', r'\breuni(ón|on)\b',
    r'\bqued(a|amos)\b', r'\bconfirm(a|ar)\b.*\bcita\b',
    r'\bquiero\b.*\bver(te)?me\b', r'\bhablemos\b.*\bel\b',
    r'\bcoordinar\b', r'\bcoordina\b', r'\bquedarme\b',
    r'\bsacar\b.*\bcita\b', r'\bmarcar\b.*\bcita\b',
    r'\bfij(ar|arme)\b.*\bcita\b', r'\bconcert(ar|arme)\b.*\bcita\b',
]

def detectar_intencion_agendamiento(mensaje):
    """Detecta si el mensaje expresa intención de agendar una cita."""
    if not mensaje:
        return False

    mensaje_lower = mensaje.lower()
    for patron in PATRONES_AGENDAMIENTO:
        if re.search(patron, mensaje_lower):
            print(f"✅ Patrón detectado: {patron}")
            return True
    return False

# Tests de ejemplo
test_messages = [
    "quiero agendar una cita mañana",
    "coordinemos una reunión",
    "¿podemos quedar para vernos?",
    "necesito sacar cita médica",
    "vamos a coordinar una cita",
    "quiero coordinar una reunión contigo",
    "hablemos del tema",
    "dame un momentico",
]

print("=== Pruebas de detección de intención de agendamiento ===\n")
for msg in test_messages:
    resultado = detectar_intencion_agendamiento(msg)
    print(f"Mensaje: '{msg}'")
    print(f"Detectado: {resultado}\n")
    print("-" * 50)
