import requests
import subprocess
import os
import asyncio
import json
import edge_tts
import pyttsx3
import shutil
import threading
import urllib.parse
import webbrowser
import base64
import re
import unicodedata
import time
from xml.etree import ElementTree
from html.parser import HTMLParser
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from gtts import gTTS
from deepgram import DeepgramClient, DeepgramClientOptions, SpeakOptions
from piper import PiperVoice
import uuid
import wave
from datetime import datetime, timedelta

class GLMService:
    CV_CONTEXT_FULL_MAX_CHARS = 9000
    CV_CONTEXT_RELEVANT_MAX_CHARS = 6500

    def __init__(self):
        self.api_key = settings.ZAI_API_KEY
        self.base_url = settings.ZAI_BASE_URL
        self.model = settings.ZAI_MODEL
        self.groq_api_key = getattr(settings, 'GROQ_API_KEY', None)
        self.groq_model_normal = getattr(settings, 'GROQ_MODEL_NORMAL', 'llama-3.1-8b-instant')

    def _limpiar_texto_cv(self, cv_texto):
        """Limpia el texto del CV eliminando notas internas y metadatos.

        Args:
            cv_texto: Texto original del CV

        Returns:
            str: Texto limpio sin notas internas
        """
        if not cv_texto:
            return ''

        import re

        texto = cv_texto

        # Eliminar notas entre *(...)* o [...]
        texto = re.sub(r'\*\([^)]*\)\*', '', texto)  # *(nota)*
        texto = re.sub(r'\[[^\]]*\*(?:.|\n)*?\*\]', '', texto)  # [*(nota)*]
        texto = re.sub(r'\[[^\]]*nota[^\]]*\]', '', texto, flags=re.IGNORECASE)  # [nota...]

        # Eliminar líneas que contengan frases típicas de notas internas
        patrones_notas = [
            r'.*nota para ti.*',
            r'.*en una situación real.*',
            r'.*adjuntarías el.*',
            r'.*esto es un ejemplo.*',
            r'.*placeholder.*',
            r'.*aquí iría.*',
            r'.*aqui iría.*',
            r'.*aquí iria.*',
            r'.*[aA]quí.*[iI]ría.*',
            r'.*\[Aquí.*',
            r'.*\[Imagen.*',
            r'.*\[PDF.*',
            r'.*\[aAquí.*PDF.*\].*',
            r'.*tarjeta de precios.*aquí.*',
            r'.*TODO:.*',
            r'.*FIXME:.*',
            r'.*XXX:.*',
            r'.*\[DEBUG\].*',
            r'.*\[INFO\].*',
        ]

        for patron in patrones_notas:
            texto = re.sub(patron, '', texto, flags=re.IGNORECASE | re.MULTILINE)

        # Eliminar líneas que contengan emojis seguidos de texto entre corchetes (comunes en plantillas)
        texto = re.sub(r'🖼️\s*\*?\[.*?\]\*?', '', texto)  # 🖼️ [texto]
        texto = re.sub(r'📄\s*\*?\[.*?\]\*?', '', texto)  # 📄 [texto]
        texto = re.sub(r'📋\s*\*?\[.*?\]\*?', '', texto)  # 📋 [texto]
        texto = re.sub(r'[📊📈📉🖼️📎📄📋]\s*\*?\[.*?\]\*?', '', texto)  # Cualquier emoji de documento + [texto]

        # Eliminar cualquier línea que sea solo un emoji seguido de corchetes
        texto = re.sub(r'^\s*[^\w\s]\s*\[.*?\]\s*$', '', texto, flags=re.MULTILINE)

        # Eliminar líneas vacías múltiples
        texto = re.sub(r'\n\s*\n\s*\n+', '\n\n', texto)

        # Limpiar espacios extra
        texto = texto.strip()

        return texto

    def _terminos_busqueda(self, texto):
        texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii").lower()
        palabras = re.findall(r'[a-z0-9]{3,}', texto)
        stopwords = {
            'que', 'como', 'para', 'por', 'con', 'del', 'los', 'las', 'una', 'uno', 'este',
            'esta', 'eso', 'esa', 'sobre', 'tiene', 'tengo', 'cual', 'cuales', 'cuando',
            'donde', 'quien', 'puede', 'puedo', 'quiero', 'necesito', 'info', 'informacion',
            'hola', 'buenas', 'gracias',
        }
        return [palabra for palabra in palabras if palabra not in stopwords]

    def _dividir_contexto_cv(self, texto):
        bloques = [bloque.strip() for bloque in re.split(r'\n\s*\n+', texto or '') if bloque.strip()]
        chunks = []
        actual = ''
        for bloque in bloques:
            if len(actual) + len(bloque) + 2 <= 1100:
                actual = f"{actual}\n\n{bloque}".strip()
            else:
                if actual:
                    chunks.append(actual)
                if len(bloque) <= 1400:
                    actual = bloque
                else:
                    for inicio in range(0, len(bloque), 1000):
                        chunks.append(bloque[inicio:inicio + 1100].strip())
                    actual = ''
        if actual:
            chunks.append(actual)
        return chunks

    def _contexto_cv_para_pregunta(self, perfil, consulta=''):
        cv_limpio = self._limpiar_texto_cv(getattr(perfil, 'cv_texto', '') or '')
        if not cv_limpio:
            return 'Sin informacion adicional aun.'

        if len(cv_limpio) <= self.CV_CONTEXT_FULL_MAX_CHARS:
            return cv_limpio

        terminos = self._terminos_busqueda(consulta)
        chunks = self._dividir_contexto_cv(cv_limpio)
        if not chunks or not terminos:
            return cv_limpio[:self.CV_CONTEXT_FULL_MAX_CHARS].rsplit('\n', 1)[0].strip()

        puntuados = []
        for idx, chunk in enumerate(chunks):
            normalizado = unicodedata.normalize("NFKD", chunk).encode("ascii", "ignore").decode("ascii").lower()
            score = 0
            for termino in terminos:
                apariciones = normalizado.count(termino)
                if apariciones:
                    score += apariciones * (3 if len(termino) >= 6 else 1)
            if score:
                puntuados.append((score, idx, chunk))

        if not puntuados:
            return cv_limpio[:self.CV_CONTEXT_FULL_MAX_CHARS].rsplit('\n', 1)[0].strip()

        seleccionados = []
        total = 0
        for _score, idx, chunk in sorted(puntuados, key=lambda item: (-item[0], item[1])):
            if total + len(chunk) > self.CV_CONTEXT_RELEVANT_MAX_CHARS and seleccionados:
                continue
            seleccionados.append((idx, chunk))
            total += len(chunk)
            if total >= self.CV_CONTEXT_RELEVANT_MAX_CHARS:
                break

        fragmentos = "\n\n---\n\n".join(chunk for _idx, chunk in sorted(seleccionados, key=lambda item: item[0]))
        return (
            "Fragmentos mas relevantes del PDF/CV para la pregunta actual:\n"
            f"{fragmentos}\n\n"
            "Si la respuesta esta en estos fragmentos, usalos con prioridad. "
            "Si falta un dato puntual, revisa tambien el resto del contexto cargado en el PDF/CV."
        )

    def _momento_colombia(self):
        ahora = timezone.localtime(timezone.now())
        hora = ahora.hour
        if 5 <= hora < 12:
            momento = 'mañana'
            saludo = 'Buenos dias'
        elif 12 <= hora < 18:
            momento = 'tarde'
            saludo = 'Buenas tardes'
        else:
            momento = 'noche'
            saludo = 'Buenas noches'

        return (
            f"Fecha y hora local de referencia: {ahora.strftime('%Y-%m-%d %H:%M')} "
            f"({getattr(settings, 'TIME_ZONE', 'America/Bogota')}, {momento}). "
            f"Si saludas, usa un saludo coherente como '{saludo}' y no mezcles mañana/tarde/noche."
        )

    def _reglas_conversacion_consultiva(self, sujeto='cliente'):
        pronombre = 'cliente' if sujeto == 'cliente' else 'usuario'
        return f"""
ESTILO DE CONVERSACION:
- Prioriza indagar antes que explicar: responde corto y haz una pregunta util para avanzar.
- Evita parrafos largos, listas extensas y explicaciones innecesarias. Si el {pronombre} no pidio detalle, no des detalle.
- Personaliza segun lo que el {pronombre} acaba de decir y segun el nicho detectado en el PDF/contexto; no uses respuestas genericas ni ejemplos de otro sector.
- Si el PDF/contexto habla de un nicho especifico (gym, inmobiliaria, clinica, restaurante, colegio, comercio, servicios profesionales, etc.), adapta las preguntas a ese nicho.
- Si el {pronombre} esta explorando una idea, pregunta primero por el objetivo o problema principal antes de mencionar productos o servicios.
- Si detectas interes real en avanzar, ofrece opciones de contacto/agendamiento de forma natural: llamada telefonica, seguir por WhatsApp o reunion por Meet/virtual, segun encaje con el contexto.
- No propongas agendar si el {pronombre} solo esta preguntando informacion; ofrece agenda solo cuando muestre interes claro, pida avanzar, cotizar, ver demo, recibir asesoria o hablar con alguien.
- Maneja cierres formales, breves y variados. No repitas siempre la misma despedida; alterna frases como "Con gusto lo revisamos.", "Quedo atento para orientarte mejor.", "Sera un gusto ayudarte a aterrizarlo.", "Gracias por contarme un poco mas.".
- Usa la hora local de Colombia para que el tono sea coherente: buenos dias en la mañana, buenas tardes en la tarde, buenas noches en la noche.
- Si haces una pregunta, que sea una sola y concreta. Ejemplo: "¿Que proceso te gustaria mejorar primero?".
- Si el {pronombre} necesita escoger canal para avanzar, pregunta algo como: "¿Prefieres que lo revisemos por llamada, WhatsApp o una reunion por Meet?".
"""

    def construir_prompt_sistema(self, perfil, canal='web', contacto=None, consulta=''):
        contacto = contacto or {}
        contexto_temporal = self._momento_colombia()
        if canal in ('whatsapp', 'facebook'):
            nombre_contacto = contacto.get('nombre') or 'un contacto'
            numero_contacto = contacto.get('numero') or 'numero desconocido'
            linea = contacto.get('linea') or 'principal'
            linea_numero = contacto.get('linea_numero') or 'numero de linea desconocido'
            canal_nombre = 'Facebook / Messenger' if canal == 'facebook' else 'WhatsApp'
            unidad = 'pagina' if canal == 'facebook' else 'linea'

            contexto_cv = self._contexto_cv_para_pregunta(perfil, consulta)

            # USAR EL PROMPT PERSONALIZADO DEL PERFIL SI EXISTE
            if perfil.prompt_sistema and perfil.prompt_sistema.strip():
                prompt_base = perfil.prompt_sistema.strip()
            else:
                # Prompt por defecto si no hay personalizado
                prompt_base = f"""Actuas como redactor invisible de {canal_nombre} para {perfil.nombre_usuario}.

Tu funcion principal es escribir exactamente como si fueras {perfil.nombre_usuario}, en primera persona, respondiendo en su nombre.
- {perfil.nombre_asistente} es solo el nombre interno del sistema. Nunca digas que eres un asistente, una IA, un bot o un sistema.
- No firmes como {perfil.nombre_asistente}. No saludes diciendo "soy {perfil.nombre_asistente}".
- No trates al remitente como si fuera {perfil.nombre_usuario}; tratalo como un contacto externo.
- Responde de forma natural, breve y humana, como una persona real."""

            return f"""{prompt_base}

Contexto del canal:
- Este mensaje viene de {nombre_contacto} ({numero_contacto}) por {canal_nombre}.
- Estas respondiendo desde la {unidad} "{linea}" ({linea_numero}).
- SOLO puedes enviar TEXTO. NO puedes enviar imagenes, PDFs, archivos ni adjuntos.
- {contexto_temporal}

RESTRICCIONES IMPORTANTES:
- Responde exactamente al mensaje del cliente, no cambies de tema.
- Si el cliente saluda, agradece, hace una pregunta simple o comenta algo casual, responde natural y humano.
- Usa la informacion del contexto (PDF/CV) abajo para datos concretos sobre {perfil.nombre_usuario}.
- NO inventes datos concretos que no esten en el contexto.
- Si el cliente pregunta por productos, servicios, precios, soluciones o tiene un problema de negocio, responde de forma consultiva:
  primero reconoce lo que necesita, luego conecta solo con servicios/productos que aparezcan en el contexto, y cierra con una pregunta breve para entender mejor su negocio.
- Si aun no sabes si una solucion encaja, investiga conversando: pregunta por su nicho, objetivo, proceso actual, problema principal, volumen aproximado o resultado esperado.
- Si el cliente dice que tiene una idea, que no sabe que necesita o algo como "no se", "no tengo claro", "quiero mejorar pero no se como", NO menciones productos por nombre todavia.
- En ese caso, primero valida la idea del cliente y pide que la explique en una frase: que quiere crear, para quien seria y que problema quiere resolver.
- Solo despues de entender esa idea puedes sugerir 2 o 3 caminos concretos basados SOLO en los productos/servicios del contexto.
- No menciones un producto especifico por nombre si el cliente no lo conoce o no ha dado suficientes datos para conectar su necesidad con ese producto; si lo mencionas, explica primero en palabras simples para que serviria.
- Si el cliente solo menciona su tipo de negocio o nicho, por ejemplo "es para un almacen", "es para un restaurante", "es para una tienda", NO recomiendes productos todavia. Pregunta primero que proceso quiere mejorar: inventario, ventas, facturacion, pedidos, cobros, atencion al cliente u otro.
- Antes de recomendar un producto por nombre necesitas al menos dos señales concretas: el nicho del cliente y el problema/objetivo que quiere resolver.
- Ejemplo correcto si dice "Es para un almacen": "Perfecto, para un almacen podemos mirar varias opciones segun lo que quieras mejorar. ¿Lo principal hoy es inventario, ventas/facturacion, pedidos o control de clientes?"
- Ejemplo incorrecto: "Necesitas NOVA..." o "Te sirve COBRAPPX..." sin que el cliente haya explicado su problema.
- Haz maximo una pregunta clara por respuesta, salvo que el cliente pida una asesoria mas completa.
- No presiones la venta; ayuda a aterrizar si tiene sentido ofrecer un servicio o si primero falta entender mejor la necesidad.
- NO hables de cosas que no te pregunten
- NO leas imagenes, NO analices archivos, NO hagas cosas que no te pidan
- NO conviertas la conversacion en agendamiento a menos que el cliente pida agendar, una cita, una reunion o confirme un horario ya propuesto.
- Responde breve, cálido y conversacional (normalmente 2 a 3 oraciones)
- Si falta un dato puntual, NO digas literalmente "No tengo esa informacion".
- Responde de forma natural: reconoce que ese dato no aparece claro y, si aplica, pide solo el dato concreto que falta.
{self._reglas_conversacion_consultiva('cliente')}

Informacion conocida sobre {perfil.nombre_usuario}:
{contexto_cv}
"""

        contexto_cv = self._contexto_cv_para_pregunta(perfil, consulta)

        # USAR EL PROMPT PERSONALIZADO DEL PERFIL SI EXISTE
        if perfil.prompt_sistema and perfil.prompt_sistema.strip():
            prompt_base = perfil.prompt_sistema.strip()
        else:
            # Prompt por defecto si no hay personalizado
            prompt_base = f"Eres {perfil.nombre_asistente}, el asistente personal de {perfil.nombre_usuario}."

        return f"""{prompt_base}

Contexto temporal:
- {contexto_temporal}

RESTRICCIONES IMPORTANTES:
- Responde exactamente al mensaje del usuario, no cambies de tema.
- Si el usuario saluda, agradece, hace una pregunta simple o comenta algo casual, responde natural y humano.
- Usa la informacion del contexto abajo para datos concretos sobre {perfil.nombre_usuario}.
- NO inventes datos concretos que no esten en el contexto.
- Si el usuario pregunta por productos, servicios, precios, soluciones o tiene un problema de negocio, responde de forma consultiva:
  primero reconoce lo que necesita, luego conecta solo con servicios/productos que aparezcan en el contexto, y cierra con una pregunta breve para entender mejor su negocio.
- Si aun no sabes si una solucion encaja, investiga conversando: pregunta por su nicho, objetivo, proceso actual, problema principal, volumen aproximado o resultado esperado.
- Si el usuario dice que tiene una idea, que no sabe que necesita o algo como "no se", "no tengo claro", "quiero mejorar pero no se como", NO menciones productos por nombre todavia.
- En ese caso, primero valida la idea del usuario y pide que la explique en una frase: que quiere crear, para quien seria y que problema quiere resolver.
- Solo despues de entender esa idea puedes sugerir 2 o 3 caminos concretos basados SOLO en los productos/servicios del contexto.
- No menciones un producto especifico por nombre si el usuario no lo conoce o no ha dado suficientes datos para conectar su necesidad con ese producto; si lo mencionas, explica primero en palabras simples para que serviria.
- Si el usuario solo menciona su tipo de negocio o nicho, por ejemplo "es para un almacen", "es para un restaurante", "es para una tienda", NO recomiendes productos todavia. Pregunta primero que proceso quiere mejorar: inventario, ventas, facturacion, pedidos, cobros, atencion al cliente u otro.
- Antes de recomendar un producto por nombre necesitas al menos dos señales concretas: el nicho del usuario y el problema/objetivo que quiere resolver.
- Ejemplo correcto si dice "Es para un almacen": "Perfecto, para un almacen podemos mirar varias opciones segun lo que quieras mejorar. ¿Lo principal hoy es inventario, ventas/facturacion, pedidos o control de clientes?"
- Ejemplo incorrecto: "Necesitas NOVA..." o "Te sirve COBRAPPX..." sin que el usuario haya explicado su problema.
- Haz maximo una pregunta clara por respuesta, salvo que el usuario pida una asesoria mas completa.
- No presiones la venta; ayuda a aterrizar si tiene sentido ofrecer un servicio o si primero falta entender mejor la necesidad.
- NO hables de cosas que no te pregunten
- NO leas imagenes, NO analices archivos, NO hagas cosas que no te pidan
- Responde breve, cálido y conversacional (normalmente 2 a 3 oraciones)
- Si falta un dato puntual, NO digas literalmente "No tengo esa informacion".
- Responde de forma natural: reconoce que ese dato no aparece claro y, si aplica, pide solo el dato concreto que falta.
{self._reglas_conversacion_consultiva('usuario')}

Información sobre {perfil.nombre_usuario}:
{contexto_cv}
"""


    def chat(self, mensaje_usuario, perfil, historial=None, canal='web', contacto=None):
        historial = historial or []
        contacto = contacto or {}
        sistema = self.construir_prompt_sistema(perfil, canal=canal, contacto=contacto, consulta=mensaje_usuario)

        messages = []
        for h in historial[-10:]:
            messages.append({"role": h["rol"], "content": h["contenido"]})
        messages.append({"role": "user", "content": mensaje_usuario})

        usar_groq_normal = self._resolver_flag_modelo(
            perfil,
            contacto,
            'usar_groq_respuestas_normales',
        )
        usar_groq_complejo = self._resolver_flag_modelo(
            perfil,
            contacto,
            'usar_groq_lexico_complejo',
        )
        requiere_modelo_complejo = self._requiere_modelo_complejo(mensaje_usuario, canal=canal)
        forzar_groq_primero = bool(
            isinstance(contacto, dict) and contacto.get('forzar_groq_primero')
        )

        if not usar_groq_normal and not usar_groq_complejo:
            return ""

        if usar_groq_normal and (forzar_groq_primero or not (usar_groq_complejo and requiere_modelo_complejo)):
            respuesta_groq = self._chat_groq(sistema, messages, self.groq_model_normal)
            if usar_groq_complejo and self._respuesta_debil(respuesta_groq, mensaje_usuario):
                motivo = "audio entrante" if forzar_groq_primero else "switch de lexico complejo"
                print(f"[GROQ] Respuesta debil; escalando a Z.AI por {motivo}")
                respuesta_zai = self._chat_zai(sistema, messages)
                return self._suavizar_si_no_sabe(respuesta_zai, canal)
            return self._suavizar_si_no_sabe(respuesta_groq, canal)

        if usar_groq_complejo and not requiere_modelo_complejo:
            return ""

        respuesta_zai = self._chat_zai(sistema, messages)
        return self._suavizar_si_no_sabe(respuesta_zai, canal)

    def _chat_zai(self, sistema, messages):
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": sistema,
            "messages": messages,
        }

        try:
            print(f"[ZAI] Enviando a {self.base_url}/v1/messages")
            print(f"[ZAI] Model: {self.model}")

            response = requests.post(
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=30,
            )

            print(f"[ZAI] Status: {response.status_code}")

            if response.status_code != 200:
                print(f"[ZAI] Error: {response.text}")

            response.raise_for_status()
            data = response.json()

            # Formato Anthropic
            texto = data["content"][0]["text"]
            return texto
        except Exception as e:
            print(f"[ZAI] Exception: {e}")
            return f"Lo siento, tuve un inconveniente técnico: {str(e)}"

    def _chat_groq(self, sistema, messages, model):
        if not (self.groq_api_key or '').strip():
            return "Lo siento, falta configurar GROQ_API_KEY en el archivo .env."

        try:
            from groq import Groq
        except ImportError:
            return "Lo siento, falta instalar el paquete groq. Ejecuta: pip install groq"

        try:
            client = Groq(api_key=self.groq_api_key)
            groq_messages = [{"role": "system", "content": sistema}]
            groq_messages.extend(messages)
            print(f"[GROQ] Model: {model}")
            completion = client.chat.completions.create(
                model=model,
                messages=groq_messages,
                temperature=0.4,
                max_tokens=2048,
            )
            return (completion.choices[0].message.content or '').strip()
        except Exception as e:
            print(f"[GROQ] Exception: {e}")
            return f"Lo siento, tuve un inconveniente técnico con Groq: {str(e)}"

    def _resolver_flag_modelo(self, perfil, contacto, campo):
        if isinstance(contacto, dict) and campo in contacto:
            return bool(contacto.get(campo))
        return bool(getattr(perfil, campo, False))

    def _suavizar_si_no_sabe(self, respuesta, canal='web'):
        texto = (respuesta or '').strip()
        if not texto:
            return texto

        lower = texto.lower()
        patrones_fuertes = (
            r'^\s*no tengo (esa )?informaci[oó]n\.?\s*$',
            r'^\s*no dispongo de (esa )?informaci[oó]n\.?\s*$',
            r'^\s*no lo s[eé]\.?\s*$',
            r'^\s*desconozco\.?\s*$',
        )
        if not any(re.search(patron, lower) for patron in patrones_fuertes):
            return texto

        if canal in ('whatsapp', 'facebook'):
            return (
                "Ese dato puntual no lo tengo fresco ahora mismo, "
                "pero lo confirmo y te cuento en un momentico."
            )

        return (
            "Ese dato puntual no lo tengo fresco ahora mismo. "
            "Lo reviso con calma y te ayudo a aterrizarlo bien."
        )

    def _requiere_modelo_complejo(self, mensaje_usuario, canal='web'):
        texto = (mensaje_usuario or '').lower()
        if len(texto) > 700:
            return True
        patrones = (
            r'\b(explica|analiza|compara|argumenta|resume|redacta)\b.*\b(detallad|profund|tecnic|juridic|academ|formal)\b',
            r'\b(lexico complejo|lenguaje complejo|terminos tecnicos|vocabulario tecnico)\b',
            r'\b(api|sdk|arquitectura|algoritmo|framework|docker|kubernetes|django|python|javascript|typescript)\b',
            r'\b(contrato|clausula|normativa|reglamento|diagnostico|estrategia|propuesta)\b',
        )
        return any(re.search(patron, texto) for patron in patrones)

    def _respuesta_debil(self, respuesta, mensaje_usuario):
        texto = (respuesta or '').strip()
        lower = texto.lower()
        if not texto:
            return True
        if len(texto) < 90 and len((mensaje_usuario or '').strip()) > 120:
            return True
        patrones = (
            r'\binconveniente t[eé]cnico\b',
            r'\bfalta configurar\b',
            r'\bfalta instalar\b',
            r'\bno tengo (informacion|información|esa informacion|esa información|datos)\b',
            r'\bno dispongo de (informacion|información|datos)\b',
            r'\bno puedo responder\b',
            r'\bno puedo confirmar\b',
            r'\bno puedo asegurar\b',
            r'\bno encuentro\b',
            r'\bno aparece\b',
            r'\bno estoy seguro\b',
            r'\bno estoy 100% seguro\b',
            r'\bno lo se\b',
            r'\bno lo sé\b',
            r'\bno se\b',
            r'\bno sé\b',
            r'\bdesconozco\b',
            r'\brespuesta insuficiente\b',
            r'\bfalta contexto\b',
        )
        return any(re.search(patron, lower) for patron in patrones)



class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self, max_results=5):
        super().__init__()
        self.max_results = max_results
        self.results = []
        self._in_result_link = False
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._in_result_link = True
            self._current_href = attrs.get("href")
            self._current_text = []

    def handle_data(self, data):
        if self._in_result_link:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_result_link:
            title = " ".join("".join(self._current_text).split())
            href = self._limpiar_url(self._current_href)
            if title and href and self._es_resultado_util(title, href) and len(self.results) < self.max_results:
                self.results.append({"titulo": title, "url": href})
            self._in_result_link = False
            self._current_href = None
            self._current_text = []

    def _limpiar_url(self, href):
        if not href:
            return ""
        if href.startswith("//duckduckgo.com/l/?"):
            parsed = urllib.parse.urlparse("https:" + href)
            query = urllib.parse.parse_qs(parsed.query)
            return query.get("uddg", [href])[0]
        return href

    def _es_resultado_util(self, title, href):
        title_lower = title.lower().strip()
        if title_lower in ("more info", "anuncio", "ads"):
            return False
        if "duckduckgo.com/y.js" in href:
            return False
        if "duckduckgo-help-pages/company/ads" in href:
            return False
        return True


class _BingHTMLParser(HTMLParser):
    def __init__(self, max_results=5):
        super().__init__()
        self.max_results = max_results
        self.results = []
        self._capture_title = False
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if len(self.results) >= self.max_results:
            return
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            href = attrs.get("href")
            if "bing.com/ck/a" in href or href.startswith("http"):
                self._capture_title = True
                self._current_href = href
                self._current_text = []

    def handle_data(self, data):
        if self._capture_title:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._capture_title:
            title = " ".join("".join(self._current_text).split())
            href = self._limpiar_url(self._current_href)
            if title and href and self._es_resultado_util(title, href):
                if not any(item["url"] == href for item in self.results):
                    self.results.append({"titulo": title, "url": href})
            self._capture_title = False
            self._current_href = None
            self._current_text = []

    def _limpiar_url(self, href):
        if not href:
            return ""
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        encoded = query.get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
            padding = "=" * (-len(encoded) % 4)
            try:
                return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            except Exception:
                return href
        return href

    def _es_resultado_util(self, title, href):
        basura = ("bing.com/search", "javascript:", "#")
        if any(fragmento in href for fragmento in basura):
            return False
        if title.lower() in ("imágenes", "videos", "noticias", "maps", "más"):
            return False
        return True


class WebResearchService:
    """Investigación web simple para diagnosticar fallos con contexto del sistema."""

    def obtener_contexto_sistema(self):
        import platform
        return {
            "sistema": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "arquitectura": platform.machine(),
            "python": platform.python_version(),
        }

    def resumen_contexto_sistema(self):
        contexto = self.obtener_contexto_sistema()
        return ", ".join(f"{k}: {v}" for k, v in contexto.items() if v)

    def investigar(self, consulta, max_results=6, incluir_contexto=False):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import time

        inicio_total = time.time()

        consultas = self.generar_consultas(consulta, incluir_contexto=incluir_contexto)
        print(f"[WEB_SEARCH] Consultas generadas: {len(consultas)}")

        resultados = []
        vistos = set()
        errores = []

        # Limitar a 2 consultas máximo para velocidad (reducido de 3)
        consultas = consultas[:2]
        print(f"[WEB_SEARCH] Consultas a probar (limitado a 2): {consultas}")

        # Hacer búsquedas en paralelo para velocidad
        inicio_busquedas = time.time()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_query = {
                executor.submit(self.buscar, query, max_results=max_results): query
                for query in consultas
            }

            for future in as_completed(future_to_query, timeout=15):
                query = future_to_query[future]
                try:
                    inicio_resultado = time.time()
                    items = future.result(timeout=8)
                    print(f"[WEB_SEARCH] Búsqueda '{query[:50]}...' devolvió {len(items)} resultados en {time.time() - inicio_resultado:.1f}s")

                    for item in items:
                        url = item.get("url", "")
                        if not url or url in vistos:
                            continue
                        if not self._resultado_relevante(item, query, consulta):
                            continue
                        vistos.add(url)
                        item["consulta"] = query
                        resultados.append(item)
                        if len(resultados) >= max_results:
                            break
                except Exception as e:
                    print(f"[WEB_SEARCH] Error en búsqueda '{query[:50]}...': {str(e)[:100]}")
                    errores.append(f"{query}: {str(e)}")

                if len(resultados) >= max_results:
                    break

        print(f"[WEB_SEARCH] Búsquedas completadas en {time.time() - inicio_busquedas:.1f}s, {len(resultados)} resultados únicos")
        print(f"[WEB_SEARCH] Tiempo total de investigación: {time.time() - inicio_total:.1f}s")

        return {
            "consulta_original": consulta,
            "consultas_probadas": consultas,
            "resultados": resultados,
            "errores": errores,
        }

    def _resultado_relevante(self, item, query, consulta_original):
        texto = f"{item.get('titulo', '')} {item.get('url', '')}".lower()
        consulta = f"{query} {consulta_original}"
        consulta_ascii = unicodedata.normalize("NFKD", consulta).encode("ascii", "ignore").decode("ascii").lower()

        if self._parece_consulta_tecnica(consulta_ascii):
            dominios_tecnicos = (
                "github.com", "stackoverflow.com", "npmjs.com", "readthedocs.io", "docs.",
                "developer.", "gitlab.com", "pypi.org", "docker.com", "nodejs.org",
            )
            terminos_tecnicos = (
                "github", "issue", "npm", "package", "library", "docs", "documentation",
                "stackoverflow", "api", "websocket", "node", "python", "linux", "error",
                "exception", "framework", "socket",
            )
            if not any(dominio in texto for dominio in dominios_tecnicos) and not any(term in texto for term in terminos_tecnicos):
                return False

        tokens = [
            token for token in re.findall(r"[a-z0-9]{3,}", consulta_ascii)
            if token not in {
                "como", "para", "con", "del", "las", "los", "una", "uno", "que",
                "the", "and", "for", "with", "fix", "error", "solution", "official",
                "documentation", "latest", "current",
            }
        ]
        if not tokens:
            return True
        coincidencias = sum(1 for token in set(tokens[:8]) if token in texto)
        return coincidencias >= 1

    def generar_consultas(self, consulta, incluir_contexto=False, max_queries=3):
        base = " ".join((consulta or "").split())
        contexto = self.resumen_contexto_sistema() if incluir_contexto else ""

        # Para velocidad, NO usar IA para generar consultas
        # Usar directamente la consulta original
        consultas = [base]

        base_ascii = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
        if self._parece_consulta_tecnica(base_ascii) and len(consultas) < 2:
            # Agregar solo 1 variante técnica para velocidad
            consultas.append(f"{base_ascii} tutorial")

        if incluir_contexto and contexto and len(consultas) < 2:
            consultas.append(f"{base} {contexto}")

        limpias = []
        vistas = set()
        for query in consultas:
            query = " ".join((query or "").split()).strip(" -;")
            if not query:
                continue
            key = query.lower()
            if key not in vistas:
                vistas.add(key)
                limpias.append(query)

        # Limitar a máximo 2 consultas para velocidad
        return limpias[:2]

    def _generar_consultas_con_ia(self, consulta, contexto="", max_queries=5):
        if not settings.ZAI_API_KEY:
            return []

        system = (
            "Eres un generador de consultas de busqueda web. "
            "Devuelve solo JSON valido con la forma {\"queries\": [\"...\"]}. "
            "Crea consultas concretas, con palabras clave, nombres propios, version si aplica, "
            "y terminos en ingles si eso ayuda. "
            "Si parece un error de programacion, libreria, CLI, servidor o framework, incluye consultas "
            "con palabras como github issue, npm package, official docs, Stack Overflow, error fix. "
            "Si un nombre puede confundirse con una marca o producto no tecnico, desambigualo con "
            "software, library, API, package, framework o el lenguaje correspondiente. "
            "No respondas la pregunta; solo crea consultas."
        )
        user = f"Pregunta o tarea: {consulta}"
        if contexto:
            user += f"\nContexto del sistema: {contexto}"

        try:
            response = requests.post(
                f"{settings.ZAI_BASE_URL}/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": settings.ZAI_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": settings.ZAI_MODEL,
                    "max_tokens": 500,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
                timeout=5,  # Reducido de 8 a 5 segundos
            )
            response.raise_for_status()
            texto = response.json()["content"][0]["text"]
            match = re.search(r"\{.*\}", texto, re.S)
            data = json.loads(match.group(0) if match else texto)
            queries = data.get("queries", [])
            return [q for q in queries if isinstance(q, str)][:max_queries]
        except Exception:
            return self._generar_consultas_fallback(consulta, contexto)

    def _generar_consultas_fallback(self, consulta, contexto=""):
        texto = " ".join((consulta or "").split())
        texto_ascii = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
        variantes = [
            texto,
            texto_ascii,
            f"{texto_ascii} official documentation",
            f"{texto_ascii} solution",
            f"{texto_ascii} latest current",
        ]
        if self._parece_consulta_tecnica(texto_ascii):
            variantes.extend([
                f"{texto_ascii} github issue",
                f"{texto_ascii} Stack Overflow",
                f"{texto_ascii} npm package library error fix",
                f"{texto_ascii} official docs",
            ])
        if contexto:
            variantes.append(f"{texto_ascii} {contexto}")
        return variantes

    def _parece_consulta_tecnica(self, consulta):
        lower = consulta.lower()
        claves = (
            "error", "exception", "traceback", "stack", "npm", "node", "python", "django",
            "react", "vite", "docker", "git", "api", "websocket", "socket", "library",
            "package", "framework", "linux", "ubuntu", "cli", "server", "stream",
        )
        return any(clave in lower for clave in claves)

    def buscar(self, consulta, max_results=5):
        # Búsqueda simplificada y rápida
        inicio = time.time()

        # Si parece consulta técnica, priorizar fuentes técnicas
        if self._parece_consulta_tecnica(consulta):
            try:
                # Intentar GitHub primero (más rápido)
                print(f"[WEB_SEARCH] Buscando en GitHub: {consulta[:50]}...")
                resultados_tecnicos = self._buscar_fuentes_tecnicas(consulta, max_results=max_results)
                if resultados_tecnicos:
                    print(f"[WEB_SEARCH] GitHub devolvió {len(resultados_tecnicos)} resultados en {time.time() - inicio:.1f}s")
                    return resultados_tecnicos
            except Exception as e:
                print(f"[WEB_SEARCH] GitHub falló: {str(e)[:50]}")

        # Si no hay resultados técnicos, buscar en DuckDuckGo (más rápido que Bing)
        try:
            print(f"[WEB_SEARCH] Buscando en DuckDuckGo: {consulta[:50]}...")
            resultados = self._buscar_duckduckgo_lite(consulta, max_results=max_results)
            print(f"[WEB_SEARCH] DuckDuckGo devolvió {len(resultados)} resultados en {time.time() - inicio:.1f}s")
            if resultados:
                return resultados
        except Exception as e:
            print(f"[WEB_SEARCH] DuckDuckGo falló: {str(e)[:50]}")

        # Último recurso: Bing HTML (más lento)
        try:
            print(f"[WEB_SEARCH] Buscando en Bing: {consulta[:50]}...")
            resultados = self._buscar_bing_html(consulta, max_results=max_results)
            print(f"[WEB_SEARCH] Bing devolvió {len(resultados)} resultados en {time.time() - inicio:.1f}s")
            return resultados
        except Exception as e:
            print(f"[WEB_SEARCH] Bing falló: {str(e)[:50]}")

        print(f"[WEB_SEARCH] No se encontraron resultados en {time.time() - inicio:.1f}s")
        return []

    def _buscar_fuentes_tecnicas(self, consulta, max_results=5):
        resultados = []
        vistos = set()

        # Solo buscar en GitHub (más rápido)
        for buscador in (self._buscar_github_issues,):
            try:
                print(f"[WEB_SEARCH] Ejecutando {buscador.__name__}...")
                for item in buscador(consulta, max_results=max_results):
                    url = item.get("url", "")
                    if url and url not in vistos:
                        vistos.add(url)
                        resultados.append(item)
                    if len(resultados) >= max_results:
                        print(f"[WEB_SEARCH] {buscador.__name__} devolvió suficientes resultados")
                        return resultados
            except Exception as e:
                print(f"[WEB_SEARCH] {buscador.__name__} falló: {str(e)[:50]}")
                continue

        return resultados

    def _buscar_github_issues(self, consulta, max_results=5):
        response = requests.get(
            "https://api.github.com/search/issues",
            params={"q": consulta, "per_page": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"titulo": item.get("title", "GitHub issue"), "url": item.get("html_url", "")}
            for item in data.get("items", [])
            if item.get("html_url")
        ]

    def _buscar_github_repos(self, consulta, max_results=5):
        response = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": consulta, "per_page": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        return [
            {"titulo": item.get("full_name", "GitHub repository"), "url": item.get("html_url", "")}
            for item in data.get("items", [])
            if item.get("html_url")
        ]

    def _buscar_npm_registry(self, consulta, max_results=5):
        response = requests.get(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": consulta, "size": max_results},
            headers={"User-Agent": "asistente-personal"},
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        data = response.json()
        resultados = []
        for item in data.get("objects", []):
            package = item.get("package", {})
            name = package.get("name")
            url = package.get("links", {}).get("npm")
            description = package.get("description")
            if name and url:
                titulo = f"{name} - npm"
                if description:
                    titulo = f"{titulo}: {description[:100]}"
                resultados.append({"titulo": titulo, "url": url})
        return resultados

    def _buscar_duckduckgo_lite(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _DuckDuckGoHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def _buscar_bing_html(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _BingHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def _buscar_bing_rss(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://www.bing.com/search",
            params={"q": consulta, "format": "rss"},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.text)
        resultados = []
        for item in root.findall("./channel/item"):
            titulo = item.findtext("title") or ""
            url = item.findtext("link") or ""
            if titulo and url:
                resultados.append({"titulo": titulo, "url": url})
            if len(resultados) >= max_results:
                break
        return resultados

    def _buscar_duckduckgo_html(self, consulta, max_results=5):
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
        }
        response = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": consulta},
            headers=headers,
            timeout=8,  # Reducido de 12 a 8
        )
        response.raise_for_status()
        parser = _DuckDuckGoHTMLParser(max_results=max_results)
        parser.feed(response.text)
        return parser.results

    def investigar_fallo(self, tarea, error, max_results=5):
        contexto = self.resumen_contexto_sistema()
        consulta = f"{tarea} error {error[:180]} {contexto} solucion"
        try:
            resultados = self.buscar(consulta, max_results=max_results)
        except Exception as e:
            return (
                "No pude consultar internet en este momento "
                f"({str(e)}). Contexto del sistema: {contexto}"
            )

        if not resultados:
            return f"No encontré resultados útiles en internet. Contexto del sistema: {contexto}"

        lineas = [
            "Investigué el fallo con el contexto de este equipo:",
            contexto,
            "",
            "Resultados útiles:",
        ]
        for idx, item in enumerate(resultados, 1):
            lineas.append(f"{idx}. {self._limpiar_texto_para_respuesta(item['titulo'])}")
        lineas.extend([
            "",
            "Siguiente paso recomendado: revise el resultado oficial o de documentación más pertinente, "
            "y si quiere ejecutar una alternativa, pídala explícitamente para evitar correr comandos inseguros."
        ])
        return "\n".join(lineas)

    def _limpiar_texto_para_respuesta(self, texto):
        texto = texto or ""
        texto = re.sub(r'https?://\S+', '', texto)
        texto = re.sub(r'www\.\S+', '', texto)
        texto = re.sub(
            r'\b[\w.-]+\.(com|co|org|net|io|dev|ai|app|edu|gov)(/\S*)?',
            '',
            texto,
            flags=re.IGNORECASE,
        )
        texto = re.sub(r'[ \t]+', ' ', texto)
        texto = re.sub(r' *\n *', '\n', texto)
        texto = re.sub(r'\n{3,}', '\n\n', texto)
        return texto.strip()


class PCActionService:
    """Acciones generales del PC para Linux/entornos de escritorio."""

    ACCIONES_CON_CONFIRMACION = {
        "apagar", "reiniciar", "suspender", "hibernar",
    }

    NAVEGADORES = {
        "firefox": ["firefox", "firefox-esr"],
        "firefoz": ["firefox", "firefox-esr"],
        "chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
        "chromium": ["chromium", "chromium-browser"],
        "brave": ["brave-browser", "brave"],
        "edge": ["microsoft-edge", "msedge"],
    }

    TERMINALES = ["gnome-terminal", "konsole", "xfce4-terminal", "xterm"]
    EXPLORADORES = ["nautilus", "dolphin", "thunar", "pcmanfm", "xdg-open"]

    def __init__(self, working_dir=None):
        self.working_dir = working_dir or os.getcwd()

    def requiere_confirmacion(self, accion):
        return accion in self.ACCIONES_CON_CONFIRMACION

    def _run(self, args, timeout=30):
        try:
            resultado = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.working_dir,
            )
            salida = (resultado.stdout or resultado.stderr or "").strip()
            if resultado.returncode == 0:
                return True, salida
            return False, salida or f"El comando terminó con código {resultado.returncode}."
        except FileNotFoundError:
            return False, f"No encontré la aplicación: {args[0]}"
        except subprocess.TimeoutExpired:
            return False, f"La acción excedió el tiempo límite de {timeout} segundos."
        except Exception as e:
            return False, str(e)

    def _encontrar_binario(self, candidatos):
        for candidato in candidatos:
            if shutil.which(candidato):
                return candidato
        return None

    def abrir_url(self, url):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            webbrowser.open(url, new=2)
            return True, f"Abrí la página: {url}"
        except Exception:
            return self._run(["xdg-open", url])

    def buscar_web(self, consulta, motor="google"):
        consulta = consulta.strip()
        if not consulta:
            return False, "Indique qué desea buscar."

        motores = {
            "google": "https://www.google.com/search?q={query}",
            "bing": "https://www.bing.com/search?q={query}",
            "duckduckgo": "https://duckduckgo.com/?q={query}",
            "youtube": "https://www.youtube.com/results?search_query={query}",
        }
        plantilla = motores.get(motor.lower(), motores["google"])
        url = plantilla.format(query=urllib.parse.quote_plus(consulta))
        return self.abrir_url(url)

    def abrir_app(self, app, argumentos=None):
        argumentos = argumentos or []
        return self._run([app, *argumentos], timeout=10)

    def abrir_terminal(self, ruta=None):
        terminal = self._encontrar_binario(self.TERMINALES)
        if not terminal:
            return False, "No encontré una terminal gráfica compatible."

        ruta = ruta or self.working_dir
        if terminal == "gnome-terminal":
            return self._run([terminal, "--working-directory", ruta], timeout=10)
        if terminal == "konsole":
            return self._run([terminal, "--workdir", ruta], timeout=10)
        if terminal == "xfce4-terminal":
            return self._run([terminal, "--working-directory", ruta], timeout=10)
        return self._run([terminal], timeout=10)

    def abrir_carpeta(self, ruta="."):
        ruta = os.path.abspath(os.path.join(self.working_dir, ruta))
        explorador = self._encontrar_binario(self.EXPLORADORES)
        if not explorador:
            return False, "No encontré un explorador de archivos compatible."
        return self._run([explorador, ruta], timeout=10)

    def cerrar_app(self, app):
        import signal
        import time

        patron = app.strip()
        if not patron:
            return False, "Indique el nombre del proceso que desea cerrar."

        procesos_antes = self._buscar_procesos(patron)
        if not procesos_antes:
            return False, f"No encontré procesos activos que coincidan con '{patron}'."

        for pid, _linea in procesos_antes:
            self._terminar_pid(pid, signal.SIGTERM)
        time.sleep(1.2)
        procesos_despues = self._buscar_procesos(patron)

        if not procesos_despues:
            return True, f"Cerré '{patron}'. Procesos finalizados: {len(procesos_antes)}."

        for pid, _linea in procesos_despues:
            self._terminar_pid(pid, signal.SIGKILL)
        time.sleep(0.8)
        procesos_finales = self._buscar_procesos(patron)

        if not procesos_finales:
            return True, f"Forcé el cierre de '{patron}'. Procesos finalizados: {len(procesos_antes)}."

        detalle = "\n".join(linea for _pid, linea in procesos_finales[:5])
        return False, f"Intenté cerrar '{patron}', pero sigue activo:\n{detalle}"

    def _buscar_procesos(self, patron):
        exito, salida = self._run(["pgrep", "-af", patron], timeout=10)
        if not exito or not salida:
            return []

        excluidos = self._pids_a_excluir()
        procesos = []
        for linea in salida.splitlines():
            if "pgrep -af" in linea or "pkill" in linea:
                continue
            partes = linea.split(maxsplit=1)
            if not partes or not partes[0].isdigit():
                continue
            pid = int(partes[0])
            if pid in excluidos:
                continue
            procesos.append((pid, linea))
        return procesos

    def _pids_a_excluir(self):
        pids = {os.getpid()}
        pid = os.getppid()
        while pid and pid not in pids:
            pids.add(pid)
            try:
                with open(f"/proc/{pid}/stat", "r") as stat_file:
                    contenido = stat_file.read().split()
                pid = int(contenido[3])
            except Exception:
                break
        return pids

    def _terminar_pid(self, pid, senal):
        try:
            os.kill(pid, senal)
            return True
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except Exception:
            return False

    def cerrar_navegador(self, navegador="firefox"):
        candidatos = self.NAVEGADORES.get(navegador.lower(), [navegador])
        errores = []
        for candidato in candidatos:
            exito, resultado = self.cerrar_app(candidato)
            if exito:
                return True, resultado
            errores.append(resultado)
        return False, "\n".join(errores) or f"No pude cerrar {navegador}."

    def bloquear(self):
        comandos = [
            ["loginctl", "lock-session"],
            ["xdg-screensaver", "lock"],
            ["gnome-screensaver-command", "-l"],
        ]
        for comando in comandos:
            if shutil.which(comando[0]):
                exito, resultado = self._run(comando, timeout=10)
                if exito:
                    return True, "Bloqueé la sesión."
                ultimo_error = resultado
        return False, locals().get("ultimo_error", "No encontré un método para bloquear la sesión.")

    def suspender(self):
        return self._run(["systemctl", "suspend"], timeout=10)

    def hibernar(self):
        return self._run(["systemctl", "hibernate"], timeout=10)

    def apagar(self):
        return self._run(["systemctl", "poweroff"], timeout=10)

    def reiniciar(self):
        return self._run(["systemctl", "reboot"], timeout=10)

    def diagnostico(self):
        comandos = [
            ("Disco", ["df", "-h"]),
            ("Memoria", ["free", "-h"]),
            ("Carga", ["uptime"]),
            ("Servicios fallidos", ["systemctl", "--failed", "--no-pager"]),
        ]
        secciones = []
        for titulo, comando in comandos:
            exito, salida = self._run(comando, timeout=20)
            estado = salida if salida else "Sin salida."
            secciones.append(f"## {titulo}\n{estado}" if exito else f"## {titulo}\nError: {estado}")
        return True, "\n\n".join(secciones)

    def errores_recientes(self):
        if not shutil.which("journalctl"):
            return False, "journalctl no está disponible en este sistema."
        return self._run(["journalctl", "-p", "3", "-n", "80", "--no-pager"], timeout=20)


class BackgroundTaskManager:
    """Administrador simple de tareas en segundo plano para el dashboard local."""

    _tasks = {}
    _lock = threading.Lock()
    _loaded = False
    _storage_file = os.path.join(settings.BASE_DIR, "background_tasks.json")

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        with cls._lock:
            if cls._loaded:
                return
            try:
                if os.path.exists(cls._storage_file):
                    with open(cls._storage_file, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    if isinstance(data, dict):
                        cls._tasks = data
            except Exception as exc:
                print(f"[BackgroundTaskManager] No se pudo cargar historial: {exc}")
            cls._loaded = True

    @classmethod
    def _persist(cls):
        try:
            tasks = list(cls._tasks.values())
            tasks.sort(key=lambda item: item.get("creado_en", ""), reverse=True)
            data = {task["id"]: task for task in tasks[:100] if task.get("id")}
            tmp_file = cls._storage_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_file, cls._storage_file)
        except Exception as exc:
            print(f"[BackgroundTaskManager] No se pudo guardar historial: {exc}")

    @classmethod
    def crear(cls, titulo, comando, target, *args, owner_id=None, **kwargs):
        cls._ensure_loaded()
        task_id = str(uuid.uuid4())
        ahora = timezone.now().isoformat(timespec="seconds")
        task = {
            "id": task_id,
            "titulo": titulo,
            "comando": comando,
            "estado": "pendiente",
            "resultado": "",
            "error": "",
            "owner_id": str(owner_id) if owner_id is not None else None,
            "creado_en": ahora,
            "iniciado_en": None,
            "finalizado_en": None,
        }
        with cls._lock:
            cls._tasks[task_id] = task
            cls._persist()

        thread = threading.Thread(
            target=cls._ejecutar,
            args=(task_id, target, args, kwargs),
            daemon=True,
        )
        thread.start()
        return task.copy()

    @classmethod
    def _ejecutar(cls, task_id, target, args, kwargs):
        cls._actualizar(task_id, estado="ejecutando", iniciado_en=timezone.now().isoformat(timespec="seconds"))
        try:
            resultado = target(*args, **kwargs)
            cls._actualizar(
                task_id,
                estado="completada",
                resultado=str(resultado or "Tarea completada sin salida."),
                finalizado_en=timezone.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            cls._actualizar(
                task_id,
                estado="error",
                error=str(exc),
                finalizado_en=timezone.now().isoformat(timespec="seconds"),
            )

    @classmethod
    def _actualizar(cls, task_id, **changes):
        cls._ensure_loaded()
        with cls._lock:
            if task_id in cls._tasks:
                cls._tasks[task_id].update(changes)
                cls._persist()

    @classmethod
    def obtener(cls, task_id, owner_id=None):
        cls._ensure_loaded()
        with cls._lock:
            task = cls._tasks.get(task_id)
            if task and owner_id is not None and task.get("owner_id") != str(owner_id):
                return None
            return task.copy() if task else None

    @classmethod
    def listar(cls, limite=20, owner_id=None):
        cls._ensure_loaded()
        with cls._lock:
            tasks = list(cls._tasks.values())
        if owner_id is not None:
            tasks = [task for task in tasks if task.get("owner_id") == str(owner_id)]
        tasks.sort(key=lambda item: item["creado_en"], reverse=True)
        return [task.copy() for task in tasks[:limite]]


class TTSService:
    """
    Servicio de Texto a Voz usando:
    - Principal: edge-tts con es-CO-GonzaloNeural (voz masculina colombiana)
    - Respaldo: pyttsx3 (offline, voces del sistema)
    """

    # Voces disponibles - Prioridad Deepgram Aura-2 (Premium AI Voices)
    VOCES_ESPANOL = [
        # DEEPGRAM AURA-2 - Premium AI Voices (Requiere API Key, ya configurada)
        # MUJERES - Voces más naturales
        ('aura-2-celeste-es', 'Celeste ✨ (Deepgram Aura - Mujer, Colombia - RECOMENDADO)'),
        ('aura-2-estrella-es', 'Estrella ✨ (Deepgram Aura - Mujer, México)'),
        ('aura-2-selena-es', 'Selena ✨ (Deepgram Aura - Mujer, Latino)'),
        ('aura-2-carina-es', 'Carina ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        ('aura-2-diana-es', 'Diana ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        # HOMBRES
        ('aura-2-nestor-es', 'Néstor ✨ (Deepgram Aura - Hombre, España)'),
        ('aura-2-sirio-es', 'Sirio ✨ (Deepgram Aura - Hombre, México)'),
        ('aura-2-javier-es', 'Javier ✨ (Deepgram Aura - Hombre, Latino)'),
        ('aura-2-aquila-es', 'Aquila ✨ (Deepgram Aura - Hombre, Codeswitching ES/EN)'),
        ('aura-2-alvaro-es', 'Álvaro ✨ (Deepgram Aura - Hombre, España)'),

        # PIPER TTS - Open Source 100% Gratis e Ilimitado (Offline, Voz Natural)
        ('piper:es_ES-mls_10246-low', 'Carlos 🎙️ (Piper TTS - Hombre, España - GRATIS ILIMITADO)'),

        # DEEPGRAM AURA-2 - Premium AI Voices (HOMBRES - Requiere API Key)
        ('aura-2-javier-es', 'Javier ✨ (Deepgram Aura - Hombre, Latino)'),
        ('aura-2-aquila-es', 'Aquila ✨ (Deepgram Aura - Hombre, Codeswitching ES/EN)'),
        ('aura-2-nestor-es', 'Néstor ✨ (Deepgram Aura - Hombre, España)'),
        ('aura-2-sirio-es', 'Sirio ✨ (Deepgram Aura - Hombre, México)'),
        ('aura-2-alvaro-es', 'Álvaro ✨ (Deepgram Aura - Hombre, España)'),

        # EDGE-TTS - Microsoft Neural Voices (HOMBRES - Gratis, requiere internet)
        # COLOMBIA
        ('es-CO-GonzaloNeural', 'Gonzalo 🇨🇴 (Hombre, Colombia)'),
        # MÉXICO
        ('es-MX-JorgeNeural', 'Jorge 🇲🇽 (Hombre, México)'),
        ('es-MX-CardenasNeural', 'Cárdenas 🇲🇽 (Hombre, México)'),
        # ESPAÑA
        ('es-ES-AlvaroNeural', 'Alvaro 🇪🇸 (Hombre, España)'),
        ('es-ES-PabloNeural', 'Pablo 🇪🇸 (Hombre, España)'),
        # ARGENTINA
        ('es-AR-ThomasNeural', 'Thomas 🇦🇷 (Hombre, Argentina)'),
        # PERÚ
        ('es-PE-AlexNeural', 'Alex 🇵🇪 (Hombre, Perú)'),
        # LATINO US
        ('es-US-AlonsoNeural', 'Alonso 🇺🇸 (Hombre, Latino US)'),
        # CHILE
        ('es-CL-LucasNeural', 'Lucas 🇨🇱 (Hombre, Chile)'),

        # GOOGLE TTS - Neutral (Gratis, fallback si Edge-TTS falla)
        ('gtts:es-co', 'Google 🇨🇴 (Colombiana - Neutral)'),
        ('gtts:es-mx', 'Google 🇲🇽 (México - Neutral)'),
        ('gtts:es-es', 'Google 🇪🇸 (España - Neutral)'),
        ('gtts:es-us', 'Google 🇺🇸 (Latino US - Neutral)'),

        # MUJERES (Opciones adicionales)
        ('es-CO-SalomeNeural', 'Salome 🇨🇴 (Mujer, Colombia)'),
        ('es-MX-DaliaNeural', 'Dalia 🇲🇽 (Mujer, México)'),
        ('es-ES-ElviraNeural', 'Elvira 🇪🇸 (Mujer, España)'),
        ('aura-2-celeste-es', 'Celeste ✨ (Deepgram Aura - Mujer, Colombia)'),
        ('aura-2-estrella-es', 'Estrella ✨ (Deepgram Aura - Mujer, México)'),
        ('aura-2-selena-es', 'Selena ✨ (Deepgram Aura - Mujer, Latino)'),
        ('aura-2-carina-es', 'Carina ✨ (Deepgram Aura - Mujer, Codeswitching)'),
        ('aura-2-diana-es', 'Diana ✨ (Deepgram Aura - Mujer, Codeswitching)'),

        # PYTTSX3 - Offline (Último recurso, voces robóticas del sistema)
        ('pyttsx3:male', 'Voz Hombre (Offline - Sistema - Robótica)'),
        ('pyttsx3:female', 'Voz Mujer (Offline - Sistema - Robótica)'),
    ]

    @classmethod
    def obtener_voces_disponibles(cls):
        return cls.VOCES_ESPANOL

    def generar_audio(self, texto, voz=None, velocidad=1.0):
        """
        Genera audio desde texto.
        voz: Código de voz (default: aura-2-celeste-es - Deepgram Aura, Mujer Colombia)
        velocidad: 0.5 a 2.0 (default: 1.0)

        Orden de prioridad: Deepgram Aura-2 (premium) → Piper (gratis) → Edge-TTS → gTTS → pyttsx3

        NOTA: Para textos medianos/largos, se generan múltiples archivos de audio
        que deben reproducirse en secuencia.
        """
        voz_a_usar = voz or 'aura-2-celeste-es'

        print(f"[TTS] Generando audio con voz: {voz_a_usar} ({len(texto)} caracteres)")

        media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')
        os.makedirs(media_dir, exist_ok=True)

        # Dividir textos medianos/largos para evitar audios enormes que se corten al reproducir.
        limite_caracteres = 550
        if len(texto) > limite_caracteres:
            print(f"[TTS] Texto largo ({len(texto)} chars), dividiendo en partes...")
            return self._generar_audio_largo(texto, voz_a_usar, velocidad, limite_caracteres)

        # DEEPGRAM AURA-2 - Premium (requiere API key, ya configurada)
        if voz_a_usar.startswith('aura-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_deepgram_aura(texto, voz_a_usar, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Deepgram Aura falló: {e}")
                return self._generar_audio_respaldo(texto, voz_a_usar, velocidad, media_dir)

        # PIPER TTS - Open Source 100% Gratis e Ilimitado (Offline)
        if voz_a_usar.startswith('piper:'):
            modelo = voz_a_usar.replace('piper:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

        # GOOGLE TTS - Opción confiable (fallback)
        if voz_a_usar.startswith('gtts:'):
            lang = voz_a_usar.replace('gtts:', '')
            nombre = f"{uuid.uuid4()}.mp3"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_gtts(texto, lang, ruta, nombre)

        # EDGE-TTS - Microsoft (puede fallar con 403)
        if voz_a_usar.startswith('es-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz_a_usar, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Edge-TTS falló: {e}")
                # Detectar si era voz masculina por el nombre
                voces_masculinas = ['Gonzalo', 'Jorge', 'Cardenas', 'Alvaro', 'Pablo', 'Thomas', 'Alex', 'Alonso', 'Lucas', 'Javier', 'Aquila']
                era_masculina = any(v in voz_a_usar for v in voces_masculinas)

                if era_masculina:
                    print(f"[TTS] Fallback a pyttsx3 masculino (offline)")
                    nombre = f"{uuid.uuid4()}.wav"
                    ruta = os.path.join(media_dir, nombre)
                    return self._generar_con_pyttsx3(texto, ruta, nombre, 'male', velocidad)
                else:
                    print(f"[TTS] Fallback a gTTS (online)")
                    nombre = f"{uuid.uuid4()}.mp3"
                    ruta = os.path.join(media_dir, nombre)
                    return self._generar_con_gtts(texto, 'es-co', ruta, nombre)

        # PYTTSX3 - Offline (último recurso, robótica)
        if voz_a_usar.startswith('pyttsx3:'):
            genero = voz_a_usar.replace('pyttsx3:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_pyttsx3(texto, ruta, nombre, genero, velocidad)

        # Default fallback
        nombre = f"{uuid.uuid4()}.mp3"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_gtts(texto, 'es-co', ruta, nombre)

    def _generar_audio_largo(self, texto, voz, velocidad, limite_caracteres):
        """Genera múltiples archivos de audio para textos largos."""
        import re

        # Dividir por oraciones, saltos de línea y viñetas. Luego partir por palabras si una parte sigue larga.
        oraciones = [
            bloque.strip()
            for bloque in re.split(r'(?<=[.!?])\s+|\n+|(?=\s*[•\-]\s+)', texto)
            if bloque.strip()
        ]
        partes = []
        parte_actual = ""
        urls = []

        for oracion in oraciones:
            fragmentos = self._partir_texto_por_palabras(oracion, limite_caracteres)
            for fragmento in fragmentos:
                if len(parte_actual) + len(fragmento) + 1 <= limite_caracteres:
                    parte_actual += (" " if parte_actual else "") + fragmento
                else:
                    if parte_actual:
                        partes.append(parte_actual)
                    parte_actual = fragmento

        if parte_actual:
            partes.append(parte_actual)

        print(f"[TTS] Texto dividido en {len(partes)} partes")

        media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')

        for idx, parte in enumerate(partes):
            print(f"[TTS] Generando parte {idx + 1}/{len(partes)} ({len(parte)} caracteres)")

            audio_url = self._generar_audio_parte(parte, voz, velocidad, media_dir)
            if audio_url:
                urls.append(audio_url)

        # Devolver la primera URL como principal, pero incluir metadata con las demás
        if urls:
            # Guardar la lista de URLs en un archivo JSON para que el frontend sepa que hay múltiples partes
            metadata_nombre = f"{uuid.uuid4()}_parts.json"
            metadata_ruta = os.path.join(media_dir, metadata_nombre)
            with open(metadata_ruta, 'w') as f:
                json.dump({"parts": urls}, f)

            # Devolver la primera URL con un parámetro especial que indica que hay múltiples partes
            primera_url = urls[0]
            if len(urls) > 1:
                # Agregar parámetro para indicar que hay múltiples partes
                separador = '&' if '?' in primera_url else '?'
                return f"{primera_url}{separador}parts={metadata_nombre}"
            return primera_url

        return None

    def _partir_texto_por_palabras(self, texto, limite_caracteres):
        if len(texto) <= limite_caracteres:
            return [texto]

        partes = []
        actual = ""
        for palabra in texto.split():
            if len(actual) + len(palabra) + 1 <= limite_caracteres:
                actual += (" " if actual else "") + palabra
            else:
                if actual:
                    partes.append(actual)
                actual = palabra

        if actual:
            partes.append(actual)

        return partes

    def _generar_audio_parte(self, texto, voz, velocidad, media_dir):
        if voz.startswith('aura-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_deepgram_aura(texto, voz, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Deepgram Aura falló en parte: {e}")
                return self._generar_audio_respaldo(texto, voz, velocidad, media_dir)

        if voz.startswith('piper:'):
            modelo = voz.replace('piper:', '')
            nombre = f"{uuid.uuid4()}.wav"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

        if voz.startswith('gtts:'):
            lang = voz.replace('gtts:', '')
            nombre = f"{uuid.uuid4()}.mp3"
            ruta = os.path.join(media_dir, nombre)
            return self._generar_con_gtts(texto, lang, ruta, nombre)

        if voz.startswith('es-'):
            try:
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz, ruta, nombre, velocidad)
            except Exception as e:
                print(f"[TTS] Edge-TTS falló en parte, usando Piper: {e}")
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

        nombre = f"{uuid.uuid4()}.wav"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

    def _generar_audio_respaldo(self, texto, voz_original, velocidad, media_dir):
        voz_respaldo = (getattr(settings, 'TTS_FALLBACK_VOICE', None) or 'piper:es_ES-mls_10246-low').strip()
        if not voz_respaldo or voz_respaldo == voz_original:
            voz_respaldo = 'piper:es_ES-mls_10246-low'

        print(f"[TTS] Usando voz de respaldo: {voz_respaldo}")

        try:
            if voz_respaldo.startswith('piper:'):
                modelo = voz_respaldo.replace('piper:', '')
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_piper(texto, modelo, ruta, nombre, velocidad)

            if voz_respaldo.startswith('gtts:'):
                lang = voz_respaldo.replace('gtts:', '')
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_gtts(texto, lang, ruta, nombre)

            if voz_respaldo.startswith('es-'):
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_edge_tts(texto, voz_respaldo, ruta, nombre, velocidad)

            if voz_respaldo.startswith('pyttsx3:'):
                genero = voz_respaldo.replace('pyttsx3:', '')
                nombre = f"{uuid.uuid4()}.wav"
                ruta = os.path.join(media_dir, nombre)
                return self._generar_con_pyttsx3(texto, ruta, nombre, genero, velocidad)
        except Exception as exc:
            print(f"[TTS] Voz de respaldo falló ({voz_respaldo}): {exc}")

        nombre = f"{uuid.uuid4()}.wav"
        ruta = os.path.join(media_dir, nombre)
        return self._generar_con_piper(texto, 'es_ES-mls_10246-low', ruta, nombre, velocidad)

    def _generar_con_deepgram_aura(self, texto, voz, ruta, nombre, velocidad):
        """
        Genera audio usando Deepgram Aura-2 (Premium AI Text-to-Speech).
        Requiere API Key configurada en DEEPGRAM_API_KEY.

        Voces españolas disponibles:
        - Mujeres: celeste (Colombia), estrella (México), selena (Latino), carina (España), diana (España)
        - Hombres: javier (Latino), aquila (Codeswitching), nestor (España), sirio (México), alvaro (España)
        """
        api_key = (getattr(settings, 'DEEPGRAM_API_KEY', None) or '').strip()
        if not api_key or api_key == 'tu_deepgram_api_key_aqui':
            raise ValueError("DEEPGRAM_API_KEY no configurada. Obtén una en https://console.deepgram.com/")

        try:
            # Configurar cliente Deepgram
            deepgram = DeepgramClient(api_key)

            # El modelo ya viene en formato correcto: aura-2-javier-es
            # Opciones de síntesis
            options = SpeakOptions(
                model=voz,  # Usar directamente el código de voz completo
                encoding="mp3",
            )

            # Agregar control de velocidad solo si es diferente a 1.0
            if velocidad != 1.0:
                options.speed = velocidad

            # Generar audio usando la API REST
            response = deepgram.speak.rest.v("1").save(ruta, {"text": texto}, options)

            print(f"[TTS] Deepgram Aura: Audio generado con voz {voz}")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] Deepgram Aura error: {e}")
            raise

    def _generar_con_piper(self, texto, modelo, ruta, nombre, velocidad=1.0):
        """
        Genera audio usando Piper TTS (Open Source, 100% Gratis, Ilimitado).
        Modelo: es_ES-mls_10246-low (voz masculina española)
        """
        try:
            # Ruta base de los modelos de Piper
            piper_models_dir = os.path.join(settings.BASE_DIR, 'piper_models')

            # Rutas del modelo
            model_path = os.path.join(piper_models_dir, f'{modelo}.onnx')
            config_path = os.path.join(piper_models_dir, f'{modelo}.onnx.json')

            if not os.path.exists(model_path):
                raise FileNotFoundError(f"Modelo Piper no encontrado: {model_path}")

            print(f"[TTS] Piper: Cargando modelo {modelo}")

            # Cargar el modelo de Piper
            voice = PiperVoice.load(model_path, config_path)

            # Generar audio usando synthesize_waz
            with wave.open(ruta, 'wb') as wav_file:
                voice.synthesize_wav(texto, wav_file)

            print(f"[TTS] Piper: Audio generado exitosamente")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] Piper error: {e}")
            raise

    def _generar_con_edge_tts(self, texto, voz, ruta, nombre, velocidad):
        """
        Genera audio usando edge-tts (Microsoft Edge Neural Voices).
        Requiere conexión a internet.
        """
        try:
            asyncio.run(self._edge_tts_async(texto, voz, ruta, velocidad))
            return f"{settings.MEDIA_URL}audios/{nombre}"
        except Exception as e:
            print(f"[TTS] Edge-TTS error: {e}")
            raise

    async def _edge_tts_async(self, texto, voz, ruta, velocidad):
        """Función asíncrona para edge-tts con manejo de errores"""
        import time
        # Pequeño delay para evitar bloqueos
        await asyncio.sleep(0.5)

        try:
            communicate = edge_tts.Communicate(
                text=texto,
                voice=voz,
                rate=f'+{int((velocidad - 1) * 100)}%' if velocidad != 1.0 else '+0%',
                connect_timeout=30
            )
            await communicate.save(ruta)
        except Exception as e:
            print(f"[TTS] Edge-TTS conexión falló: {e}")
            raise

    def _generar_con_pyttsx3(self, texto, ruta, nombre, genero='male', velocidad=1.0):
        """
        Genera audio usando pyttsx3 (offline, voces del sistema).
        Guarda directamente como WAV para evitar dependencia de ffmpeg.
        """
        import warnings
        warnings.filterwarnings("ignore", category=RuntimeWarning)

        try:
            engine = pyttsx3.init()

            # Configurar velocidad (pyttsx3 usa rango 50-500, siendo 200 normal)
            rate = int(200 * velocidad)
            engine.setProperty('rate', rate)

            # Configurar volumen
            engine.setProperty('volume', 0.9)

            # Intentar configurar voz por género
            voices = engine.getProperty('voices')
            selected_voice = None

            # Buscar voz en español o latinoamericana
            for voice in voices:
                voice_name = voice.name.lower()
                voice_id = voice.id.lower()

                # Prioridad: Español Latinoamérica
                if 'spanish' in voice_name or 'es_' in voice_id or 'latino' in voice_name:
                    if genero == 'male':
                        # Preferir voces masculinas
                        if any(m in voice_name for m in ['david', 'jorge', 'male', 'hombre', 'juan', 'carlos', 'miguel', 'alex']):
                            selected_voice = voice.id
                            break
                    else:
                        # Preferir voces femeninas
                        if any(f in voice_name for f in ['female', 'mujer', 'zira', 'santa', 'maria', 'elena', 'sofia']):
                            selected_voice = voice.id
                            break

            # Si no encontramos voz específica, usar la primera disponible
            if not selected_voice and voices:
                selected_voice = voices[0].id

            if selected_voice:
                engine.setProperty('voice', selected_voice)

            # Guardar directamente como WAV
            engine.save_to_file(texto, ruta)

            # Ejecutar y esperar con mejor manejo de errores
            engine.runAndWait()

            # Pequeña pausa para asegurar que el archivo se escribió
            import time
            time.sleep(0.5)

            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] pyttsx3 error: {e}")
            # Último recurso: gTTS
            return self._generar_con_gtts(texto, lang='es-co')

    def _generar_con_gtts(self, texto, lang='es-co', ruta=None, nombre=None):
        """
        Genera audio usando Google TTS (gTTS).
        lang: Código de idioma (es-co, es-mx, es-es, es-us)
        Gratis, claro, confiable. Requiere internet.
        """
        try:
            # Si no se proporciona ruta/nombre, generarlos
            if not ruta or not nombre:
                media_dir = os.path.join(settings.MEDIA_ROOT, 'audios')
                os.makedirs(media_dir, exist_ok=True)
                nombre = f"{uuid.uuid4()}.mp3"
                ruta = os.path.join(media_dir, nombre)

            # Mapeo de idiomas a dominios de Google
            lang_tld_map = {
                'es-co': 'com.co',  # Colombia
                'es-mx': 'com.mx',  # México
                'es-es': 'es',      # España
                'es-us': 'us',      # Latino US
            }

            # Extraer código de idioma base (es) y dominio
            tld = lang_tld_map.get(lang, 'com.co')

            print(f"[TTS] gTTS: Generando audio con idioma {lang} (tld={tld})")

            tts = gTTS(text=texto, lang='es', tld=tld, slow=False)
            tts.save(ruta)

            print(f"[TTS] gTTS: Audio guardado en {ruta}")
            return f"{settings.MEDIA_URL}audios/{nombre}"

        except Exception as e:
            print(f"[TTS] gTTS error: {e}")
            return None

    def _wav_to_mp3(self, wav_path, mp3_path):
        """Convierte WAV a MP3 usando pydub"""
        try:
            from pydub import AudioSegment
            audio = AudioSegment.from_wav(wav_path)
            audio.export(mp3_path, format='mp3', bitrate='128k')
        except:
            # Si pydub no está disponible, usar ffmpeg directamente
            subprocess.run(['ffmpeg', '-i', wav_path, '-codec:a', 'libmp3lame', '-b:a', '128k', mp3_path],
                          capture_output=True, timeout=30)


class SchedulerService:
    """
    Servicio de programación de tareas que corre en segundo plano.
    Revisa periódicamente las tareas pendientes y las ejecuta cuando llega el momento.
    """

    def __init__(self, intervalo_segundos=30):
        """
        intervalo_segundos: Cada cuánto tiempo revisa tareas pendientes (default: 30s)
        """
        self.intervalo = intervalo_segundos
        self._running = False
        self._thread = None
        self.pc_service = PCActionService()

    def iniciar(self):
        """Inicia el scheduler en segundo plano."""
        if self._running:
            print("[SCHEDULER] Ya está corriendo")
            return

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[SCHEDULER] Iniciado (revisa cada {self.intervalo}s)")

    def detener(self):
        """Detiene el scheduler."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("[SCHEDULER] Detenido")

    def _loop(self):
        """Loop principal que revisa y ejecuta tareas pendientes."""
        from asistente.models import TareaProgramada

        while self._running:
            try:
                ahora = timezone.now()

                # Buscar tareas pendientes que deben ejecutarse ahora o antes
                tareas = TareaProgramada.objects.filter(
                    estado='pendiente',
                    programado_para__lte=ahora
                )

                for tarea in tareas:
                    self._ejecutar_tarea(tarea)

                # Revisar citas que necesitan recordatorio
                self._revisar_recordatorios_citas()

            except Exception as e:
                print(f"[SCHEDULER] Error en loop: {e}")

            # Esperar hasta la siguiente revisión
            time.sleep(self.intervalo)

    def _ejecutar_tarea(self, tarea):
        """Ejecuta una tarea específica según su tipo."""
        print(f"[SCHEDULER] Ejecutando tarea: {tarea.titulo} ({tarea.tipo_accion})")

        if tarea.tipo_accion == 'whatsapp' and tarea.parametros.get('numeros'):
            tarea.estado = 'ejecutando'
            tarea.save()
            threading.Thread(
                target=self._ejecutar_campana_whatsapp,
                args=(tarea.id,),
                daemon=True,
            ).start()
            return

        # Marcar como ejecutando
        tarea.estado = 'ejecutando'
        tarea.save()

        try:
            if tarea.tipo_accion == 'whatsapp':
                resultado = self._enviar_whatsapp(tarea.parametros, tarea.perfil)
                tarea.marcar_ejecutada(exitoso=resultado['exito'], resultado=resultado.get('mensaje'), error=resultado.get('error'))

            elif tarea.tipo_accion == 'url':
                url = tarea.parametros.get('url', '')
                exito, resultado = self.pc_service.abrir_url(url)
                tarea.marcar_ejecutada(exitoso=exito, resultado=resultado)

            elif tarea.tipo_accion == 'sistema':
                accion = tarea.parametros.get('accion', '')
                exito, resultado = self._ejecutar_accion_sistema(accion)
                tarea.marcar_ejecutada(exitoso=exito, resultado=resultado)

            elif tarea.tipo_accion == 'recordatorio':
                # Solo marcar como completada (el frontend mostrará una notificación)
                tarea.marcar_ejecutada(
                    exitoso=True,
                    resultado=f"Recordatorio: {tarea.parametros.get('mensaje', tarea.titulo)}"
                )

            # Si tiene repetición, crear la siguiente instancia
            if tarea.repetir:
                self._programar_siguiente_instancia(tarea)

        except Exception as e:
            print(f"[SCHEDULER] Error ejecutando tarea {tarea.id}: {e}")
            tarea.marcar_ejecutada(exitoso=False, error=str(e))

    def _ejecutar_campana_whatsapp(self, tarea_id):
        """Ejecuta campañas masivas sin bloquear el loop principal del scheduler."""
        from asistente.models import TareaProgramada

        try:
            tarea = TareaProgramada.objects.get(id=tarea_id)
            parametros = dict(tarea.parametros or {})
            parametros['_tarea_id'] = tarea.id
            resultado = self._enviar_whatsapp(parametros, tarea.perfil)
            tarea.marcar_ejecutada(
                exitoso=resultado['exito'],
                resultado=resultado.get('mensaje'),
                error=resultado.get('error'),
            )
            if tarea.repetir:
                self._programar_siguiente_instancia(tarea)
        except Exception as e:
            print(f"[SCHEDULER] Error ejecutando campaña WhatsApp {tarea_id}: {e}")
            try:
                tarea = TareaProgramada.objects.get(id=tarea_id)
                tarea.marcar_ejecutada(exitoso=False, error=str(e))
            except Exception:
                pass

    def _enviar_whatsapp(self, parametros, perfil=None):
        """Envía un mensaje de WhatsApp a través del baileys-service."""

        numeros = parametros.get('numeros') or parametros.get('destinatarios')
        if numeros:
            return self._enviar_whatsapp_masivo(parametros, perfil)

        numero = parametros.get('numero', '')
        mensaje = parametros.get('mensaje', '')
        linea = parametros.get('linea', 'principal')

        if not numero or not mensaje:
            return {'exito': False, 'error': 'Faltan número o mensaje'}

        # Normalizar número (quitar +, espacios, guiones)
        numero_limpio = ''.join(c for c in numero if c.isdigit())
        if not numero_limpio:
            return {'exito': False, 'error': 'Número inválido'}

        try:
            response, url_baileys = self._post_baileys(
                'send-message',
                json={
                    'numero': numero_limpio,
                    'mensaje': mensaje,
                    'linea': linea,
                },
                timeout=10
            )

            if response.status_code == 200:
                return {'exito': True, 'mensaje': f'Mensaje enviado a {numero}'}
            else:
                return {'exito': False, 'error': f'Error baileys ({url_baileys}): {response.text}'}

        except Exception as e:
            return {'exito': False, 'error': str(e)}

    def _urls_baileys(self):
        configurada = (getattr(settings, 'BAILEYS_SERVICE_URL', None) or 'http://localhost:3002').rstrip('/')
        urls = [configurada]
        for fallback in ('http://localhost:3002', 'http://localhost:3003'):
            if fallback not in urls and configurada.startswith('http://localhost:'):
                urls.append(fallback)
        return urls

    def _post_baileys(self, endpoint, json, timeout=60):
        errores = []
        for url_baileys in self._urls_baileys():
            try:
                response = requests.post(f"{url_baileys}/{endpoint}", json=json, timeout=timeout)
                return response, url_baileys
            except requests.RequestException as exc:
                errores.append(f"{url_baileys}: {exc}")
        raise RuntimeError("No pude conectar con Baileys. Intentos: " + " | ".join(errores))

    def _limpiar_numero_whatsapp(self, numero):
        limpio = ''.join(c for c in str(numero or '') if c.isdigit())
        codigo_pais = ''.join(c for c in str(getattr(settings, 'WHATSAPP_DEFAULT_COUNTRY_CODE', '57')) if c.isdigit()) or '57'
        if len(limpio) == 10 and limpio.startswith('3'):
            return f"{codigo_pais}{limpio}"
        if len(limpio) == 11 and limpio.startswith('03'):
            return f"{codigo_pais}{limpio[1:]}"
        return limpio

    def _preparar_mensaje_whatsapp_masivo(self, parametros, perfil):
        modo = (parametros.get('modo_contenido') or 'mensaje').strip().lower()
        if modo == 'prompt':
            prompt = (parametros.get('prompt') or '').strip()
            if not prompt:
                raise ValueError('Falta el prompt para generar el mensaje')
            if not perfil:
                raise ValueError('No hay perfil configurado para generar el mensaje')
            instruccion = (
                "Redacta un único mensaje de WhatsApp listo para enviar. "
                "No incluyas explicación, asunto, comillas ni alternativas. "
                f"Instrucción del usuario: {prompt}"
            )
            return GLMService().chat(instruccion, perfil, [], canal='whatsapp').strip()
        return (parametros.get('mensaje') or '').strip()

    def _enviar_whatsapp_masivo(self, parametros, perfil=None):
        """Envía una campaña de WhatsApp a varios números con delay configurable."""
        from asistente.models import Conversacion, Mensaje
        import random

        linea = (parametros.get('linea') or 'principal').strip() or 'principal'
        linea_publica = (parametros.get('linea_publica') or linea).strip() or linea
        formato = (parametros.get('formato') or 'texto').strip().lower()
        numeros_raw = parametros.get('numeros') or []
        if isinstance(numeros_raw, str):
            numeros_raw = re.split(r'[\s,;]+', numeros_raw)

        vistos = set()
        numeros = []
        for item in numeros_raw:
            limpio = self._limpiar_numero_whatsapp(item)
            if limpio and limpio not in vistos:
                numeros.append(limpio)
                vistos.add(limpio)

        if not numeros:
            return {'exito': False, 'error': 'No hay números válidos para enviar'}

        mensaje = self._preparar_mensaje_whatsapp_masivo(parametros, perfil)
        if not mensaje:
            return {'exito': False, 'error': 'El mensaje quedó vacío'}

        try:
            delay_min = float(parametros.get('delay_min', 1) or 1)
            delay_max = float(parametros.get('delay_max', delay_min) or delay_min)
        except (TypeError, ValueError):
            delay_min, delay_max = 1, 1
        delay_min = max(0, delay_min)
        delay_max = max(delay_min, delay_max)
        delay_unit = (parametros.get('delay_unit') or 'segundos').strip().lower()
        delay_factor = 60 if delay_unit == 'minutos' else 1
        delay_min_seconds = delay_min * delay_factor
        delay_max_seconds = delay_max * delay_factor

        audio_url = None
        if formato == 'audio':
            if not perfil:
                return {'exito': False, 'error': 'No hay perfil configurado para generar audio'}
            audio_url = TTSService().generar_audio(
                mensaje,
                voz=perfil.voz_preferida,
                velocidad=perfil.voz_velocidad,
            )

        enviados = []
        fallidos = []
        pausas = []
        total = len(numeros)

        def actualizar_progreso(actual, numero_actual='', estado_extra='ejecutando'):
            try:
                tarea_id = parametros.get('_tarea_id')
                if not tarea_id:
                    return
                from asistente.models import TareaProgramada
                tarea_progreso = TareaProgramada.objects.get(id=tarea_id)
                nuevos_parametros = dict(tarea_progreso.parametros or {})
                nuevos_parametros['progreso'] = {
                    'actual': actual,
                    'total': total,
                    'enviados': len(enviados),
                    'fallidos': len(fallidos),
                    'numero_actual': numero_actual,
                    'estado': estado_extra,
                }
                tarea_progreso.parametros = nuevos_parametros
                tarea_progreso.save(update_fields=['parametros'])
            except Exception as exc:
                print(f"[SCHEDULER] No pude actualizar progreso WhatsApp: {exc}")

        actualizar_progreso(0, '', 'iniciando')
        for idx, numero in enumerate(numeros, 1):
            actualizar_progreso(idx - 1, numero, 'enviando')
            try:
                endpoint = 'send-audio' if formato == 'audio' else 'send-message'
                payload = {'numero': numero, 'linea': linea}
                if formato == 'audio':
                    payload['audio_url'] = audio_url
                else:
                    payload['mensaje'] = mensaje

                response, url_usada = self._post_baileys(endpoint, json=payload, timeout=60)
                if response.status_code == 200:
                    enviados.append(numero)
                    if perfil:
                        conversacion, _ = Conversacion.objects.get_or_create(
                            perfil=perfil,
                            numero_whatsapp=f"{linea_publica}:{numero}"[:80],
                        )
                        Mensaje.objects.create(
                            conversacion=conversacion,
                            tipo='voz' if formato == 'audio' else 'texto',
                            origen='saliente',
                            contenido=mensaje,
                            audio_url=audio_url,
                            respondido=True,
                        )
                else:
                    fallidos.append(f"{numero}: Error baileys ({url_usada}): {response.text[:220]}")
            except Exception as exc:
                fallidos.append(f"{numero}: {exc}")

            actualizar_progreso(idx, numero, 'pausando' if idx < total else 'finalizando')
            if idx < len(numeros) and delay_max_seconds > 0:
                pausa = random.uniform(delay_min_seconds, delay_max_seconds)
                pausas.append(round(pausa, 2))
                time.sleep(pausa)

        actualizar_progreso(total, '', 'terminada')

        resumen = (
            f"Campaña WhatsApp por línea {linea_publica}. "
            f"Formato: {formato}. Enviados: {len(enviados)} de {len(numeros)}. "
            f"Espera configurada: {delay_min}-{delay_max} {delay_unit}."
        )
        if pausas:
            resumen += f"\nPausas aplicadas (segundos): {', '.join(str(p) for p in pausas[:20])}"
        if fallidos:
            resumen += "\nFallidos:\n" + "\n".join(fallidos[:30])

        return {
            'exito': len(enviados) > 0 and len(fallidos) == 0,
            'mensaje': resumen,
            'error': None if not fallidos else resumen,
        }

    def _ejecutar_accion_sistema(self, accion):
        """Ejecuta acciones de sistema como bloquear, suspender, etc."""
        accion = accion.lower()

        if accion == 'bloquear':
            return self.pc_service.bloquear()
        elif accion == 'suspender':
            return self.pc_service.suspender()
        elif accion == 'hibernar':
            return self.pc_service.hibernar()
        elif accion == 'apagar':
            return self.pc_service.apagar()
        elif accion == 'reiniciar':
            return self.pc_service.reiniciar()
        else:
            return False, f"Acción de sistema no reconocida: {accion}"

    def _programar_siguiente_instancia(self, tarea_original):
        """Crea una nueva instancia de la tarea según el patrón de repetición."""
        from datetime import timedelta

        siguiente_hora = None
        repetir = tarea_original.repetir.lower()

        if repetir == 'diario':
            siguiente_hora = tarea_original.programado_para + timedelta(days=1)
        elif repetir == 'semanal':
            siguiente_hora = tarea_original.programado_para + timedelta(weeks=1)
        elif repetir == 'mensual':
            # Aproximación simple: 30 días
            siguiente_hora = tarea_original.programado_para + timedelta(days=30)

        if siguiente_hora:
            # Crear nueva tarea
            TareaProgramada.objects.create(
                perfil=tarea_original.perfil,
                titulo=tarea_original.titulo,
                tipo_accion=tarea_original.tipo_accion,
                parametros=tarea_original.parametros,
                programado_para=siguiente_hora,
                repetir=tarea_original.repetir
            )
            print(f"[SCHEDULER] Siguiente instancia programada para {siguiente_hora}")

    def _revisar_recordatorios_citas(self):
        """Revisa y envía recordatorios de citas que están próximas."""
        from asistente.models import Cita
        from datetime import timedelta

        ahora = timezone.now()
        ventana = ahora + timedelta(minutes=5)

        # Buscar citas que necesitan recordatorio
        citas = Cita.objects.filter(
            estado__in=['pendiente', 'confirmada'],
            recordatorio_enviado=False,
            fecha_hora__lte=ventana,
        ).exclude(fecha_hora__lte=ahora)

        for cita in citas:
            try:
                minutos_restantes = int((cita.fecha_hora - ahora).total_seconds() / 60)

                # Enviar recordatorio si está dentro del tiempo configurado
                if minutos_restantes <= cita.recordatorio_minutos_antes:
                    print(f"[SCHEDULER] Enviando recordatorio para cita: {cita.titulo}")

                    # Usar la tarea de recordatorio asociada si existe
                    if cita.tarea_recordatorio and cita.tarea_recordatorio.estado == 'pendiente':
                        self._ejecutar_tarea(cita.tarea_recordatorio)
                    else:
                        # Crear y enviar recordatorio al momento
                        from .services import CitaService
                        exito, mensaje = CitaService().enviar_recordatorio_cita(cita.id)
                        if not exito:
                            print(f"[SCHEDULER] Error enviando recordatorio: {mensaje}")

                    cita.recordatorio_enviado = True
                    cita.save()

            except Exception as e:
                print(f"[SCHEDULER] Error enviando recordatorio para cita {cita.id}: {e}")

    @staticmethod
    def crear_tarea(perfil, titulo, tipo_accion, parametros, programado_para, repetir=None):
        """Método estático para crear una nueva tarea programada."""
        from asistente.models import TareaProgramada

        tarea = TareaProgramada.objects.create(
            perfil=perfil,
            titulo=titulo,
            tipo_accion=tipo_accion,
            parametros=parametros,
            programado_para=programado_para,
            repetir=repetir
        )
        return tarea

    @staticmethod
    def listar_tareas_pendientes(perfil=None):
        """Lista tareas pendientes, opcionalmente filtradas por perfil."""
        from asistente.models import TareaProgramada

        qs = TareaProgramada.objects.filter(estado='pendiente')
        if perfil:
            qs = qs.filter(perfil=perfil)
        return qs.order_by('programado_para')

    @staticmethod
    def cancelar_tarea(tarea_id):
        """Cancela una tarea específica."""
        from asistente.models import TareaProgramada

        try:
            tarea = TareaProgramada.objects.get(id=tarea_id)
            tarea.cancelar()
            return True, "Tarea cancelada"
        except TareaProgramada.DoesNotExist:
            return False, "Tarea no encontrada"


# Instancia global del scheduler
_scheduler_instance = None

def obtener_scheduler():
    """Retorna la instancia global del scheduler, la crea si no existe."""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = SchedulerService()
        _scheduler_instance.iniciar()
    return _scheduler_instance


class CitaService:
    """Servicio para gestionar citas, detección de intención y recordatorios."""

    def __init__(self):
        self.glm_service = GLMService()

    def _normalizar_fecha_hora(self, fecha_hora):
        """Devuelve un datetime aware usando la zona horaria configurada."""
        if fecha_hora and timezone.is_naive(fecha_hora):
            return timezone.make_aware(fecha_hora)
        return fecha_hora

    def _rango_dia_local(self, fecha_hora):
        """Devuelve el rango UTC/aware que cubre el dia local de fecha_hora."""
        fecha_local = timezone.localtime(self._normalizar_fecha_hora(fecha_hora)).date()
        inicio_local = datetime.combine(fecha_local, datetime.min.time())
        inicio = self._normalizar_fecha_hora(inicio_local)
        fin = inicio + timedelta(days=1)
        return inicio, fin

    def _normalizar_inicio_cita(self, fecha_hora):
        fecha_hora = self._normalizar_fecha_hora(fecha_hora)
        if not fecha_hora:
            return fecha_hora
        return fecha_hora.replace(second=0, microsecond=0)

    def _formatear_fecha_cita(self, fecha_hora, formato="%d/%m %H:%M"):
        return timezone.localtime(self._normalizar_fecha_hora(fecha_hora)).strftime(formato)

    def _ajustar_hora_ambigua(self, datos, texto_contextual=''):
        """Evita convertir horas ambiguas como "a las 5" en 05:00 por accidente."""
        hora = datos.get("hora")
        if not hora:
            return datos

        match = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', str(hora))
        if not match:
            return datos

        hora_num = int(match.group(1))
        minuto = int(match.group(2))
        texto = (texto_contextual or '').lower()
        menciona_tarde = bool(re.search(r'\b(pm|p\.m\.|tarde|noche)\b', texto))
        menciona_manana = bool(re.search(
            r'\b(am|a\.m\.|madrugada)\b|\b(de|por)\s+la\s+(mañana|manana)\b',
            texto,
        ))

        if 1 <= hora_num <= 7 and menciona_tarde and not menciona_manana:
            datos["hora"] = f"{hora_num + 12:02d}:{minuto:02d}"
            return datos

        if 1 <= hora_num <= 7 and not menciona_tarde and not menciona_manana:
            datos["completo"] = False
            datos["fecha_hora"] = None
            datos["motivo_incompleto"] = "Hora ambigua: falta aclarar si es de la mañana o de la tarde."

        return datos

    # Patrones para detectar intención de agendamiento
    PATRONES_AGENDAMIENTO = [
        r'\b(quiero|quisiera|necesito|puedo|podemos|me puedes|me gustaria|me gustaría)\b.*\b(agendar|programar|reservar|cita|reuni(ón|on))\b',
        r'\b(agendar|programar|reservar|sacar|marcar|fijar|concertar)\b.*\b(cita|reuni(ón|on)|llamada|meet|agenda)\b',
        r'\b(cita|reuni(ón|on)|llamada|meet)\b.*\b(agendar|programar|reservar|coordinar|cuadrar|confirmar)\b',
        r'\bconfirm(a|ar)\b.*\b(cita|reuni(ón|on)|llamada|meet|horario)\b',
        r'\bquiero\b.*\bver(te)?me\b', r'\bhablemos\b.*\bel\b',
        r'\bsacar\b.*\bcita\b', r'\bmarcar\b.*\bcita\b',
        r'\bfij(ar|arme)\b.*\bcita\b', r'\bconcert(ar|arme)\b.*\bcita\b',
        r'\b(podemos|podría|puedo)\s+(vernos|hablar|quedar|reunirnos)\b.*\b(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo|mañana|hoy|hora|am|pm)\b',
    ]

    def detectar_intencion_agendamiento(self, mensaje, conversacion=None):
        """Detecta si el mensaje expresa intención de agendar una cita.

        Args:
            mensaje: Texto del mensaje a analizar

        Returns:
            bool: True si se detecta intención de agendar
        """
        print(f"[CITA_SERVICE] 🔧🔧 detectar_intencion_agendamiento LLAMADO con mensaje: '{mensaje[:100] if mensaje else '(vacio)'}'")

        if not mensaje:
            print("[CITA_SERVICE] detectar_intencion_agendamiento: mensaje vacío")
            return False

        mensaje_lower = mensaje.lower()
        contexto_conversacion = self._resumen_conversacion_para_cita(conversacion, limite=10)
        texto_contextual = f"{contexto_conversacion}\nCliente: {mensaje}".strip()
        texto_contextual_lower = texto_contextual.lower()
        print(f"[CITA_SERVICE] 🔍 Analizando mensaje para intención de agendamiento: '{mensaje[:100]}...'")
        print(f"[CITA_SERVICE] Longitud del mensaje: {len(mensaje)} caracteres")

        hay_solicitud_explicita = self._mensaje_pide_agendar_explicitamente(mensaje_lower)
        espera_dato_cita = self._conversacion_espera_dato_cita(conversacion)

        # Primero verificar con patrones rápidos de regex, pero solo patrones de agenda claros.
        for patron in self.PATRONES_AGENDAMIENTO:
            if re.search(patron, mensaje_lower):
                print(f"[CITA_SERVICE] ✅ Patrón detectado: {patron}")
                return True

        if espera_dato_cita and self._mensaje_es_fragmento_de_cita(mensaje_lower):
            print("[CITA_SERVICE] ✅ Intención detectada por contexto conversacional reciente")
            return True

        # Si no detecta con regex, usar IA para analizar mejor el mensaje
        # Solo si menciona día de la semana, hora, o palabras relacionadas
        menciones_dias = any(dia in texto_contextual_lower for dia in ['lunes', 'martes', 'miércoles', 'miercoles', 'jueves', 'viernes', 'sábado', 'sabado', 'domingo', 'mañana', 'hoy'])
        menciones_hora = any(patron in texto_contextual_lower for patron in ['am', 'pm', 'de la mañana', 'de la tarde', 'de la noche', ':']) or bool(re.search(r'\d{1,2}\s*:\s*\d{2}', texto_contextual))
        menciones_agendar = any(palabra in texto_contextual_lower for palabra in ['agendar', 'cita', 'reunion', 'reunión', 'quedar', 'hablaremos', 'verse', 'versemos', 'meet'])

        if hay_solicitud_explicita and (menciones_dias or menciones_hora):
            print(f"[CITA_SERVICE] ✅ Detecta día + hora, usando IA para confirmar intención")
            # Usar IA para confirmar si es una cita
            return self._confirmar_intencion_con_ia(texto_contextual)

        if hay_solicitud_explicita and menciones_agendar:
            print(f"[CITA_SERVICE] ✅ Detecta conversación de cita con fragmento útil")
            return self._confirmar_intencion_con_ia(texto_contextual)

        print("[CITA_SERVICE] ❌ No se detectó ningún patrón de agendamiento")
        return False

    def _mensaje_pide_agendar_explicitamente(self, mensaje_lower):
        patrones = (
            r'\b(agendar|agenda|programar|reservar|sacar|marcar|fijar|concertar|coordinar|cuadrar)\b',
            r'\b(cita|reuni(ón|on)|llamada|meet|google meet|zoom|teams)\b',
            r'\b(vernos|reunirnos|hablemos|hablar)\b.*\b(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo|mañana|hoy|hora|am|pm)\b',
        )
        return any(re.search(patron, mensaje_lower) for patron in patrones)

    def _conversacion_espera_dato_cita(self, conversacion):
        if not conversacion:
            return False

        ultimo = conversacion.mensajes.filter(origen='saliente').order_by('-creado_en').first()
        if not ultimo:
            return False

        texto = (ultimo.contenido or '').lower()
        patrones = (
            r'\bpara agendar\b',
            r'\bd[ií]a y la hora\b',
            r'\bhora exacta\b',
            r'\botro horario\b',
            r'\bhorarios disponibles\b',
            r'\bte gustar[ií]a agendar\b',
            r'\bde la mañana o de la tarde\b',
        )
        return any(re.search(patron, texto) for patron in patrones)

    def _mensaje_es_fragmento_de_cita(self, mensaje_lower):
        fragmentos = (
            r'\b(a que horas|a qué horas|que horas|qué horas|hora)\b',
            r'\b(meet|google meet|zoom|teams|presencial|virtual)\b',
            r'\b(metro|oficina|local|sede|direccion|dirección|ubicacion|ubicación)\b',
            r'\b(listo|dale|va|ok|perfecto|confirmo|confirmado|si|sí)\b',
            r'\b(no jajaj|mejor|entonces)\b',
        )
        return any(re.search(patron, mensaje_lower) for patron in fragmentos)

    def _contexto_indica_agendamiento(self, texto_contextual_lower, mensaje_lower):
        if not texto_contextual_lower:
            return False

        claves_cita = (
            'agendar', 'agenda', 'cita', 'reunion', 'reunión', 'meet', 'google meet',
            'vernos', 'vernos', 'hablemos', 'hablar', 'quedar', 'disponible',
            'horario', 'hora exacta', 'día y la hora', 'dia y la hora',
        )
        hay_contexto_cita = any(clave in texto_contextual_lower for clave in claves_cita)
        hay_fecha = any(dia in texto_contextual_lower for dia in (
            'lunes', 'martes', 'miércoles', 'miercoles', 'jueves', 'viernes',
            'sábado', 'sabado', 'domingo', 'mañana', 'pasado mañana', 'hoy',
        ))
        hay_hora = bool(re.search(
            r'\b(?:a las?|tipo|sobre las?)\s+\d{1,2}\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b',
            texto_contextual_lower,
        ))
        fragmento_actual = self._mensaje_es_fragmento_de_cita(mensaje_lower)

        return hay_contexto_cita and (hay_fecha or hay_hora or fragmento_actual)

    def _confirmar_intencion_con_ia(self, mensaje):
        """Usa IA para confirmar si el mensaje expresa intención de agendar.

        Args:
            mensaje: Texto del mensaje a analizar

        Returns:
            bool: True si la IA confirma que es intención de agendar
        """
        import json

        prompt = f"""Analiza el siguiente mensaje y responde ÚNICAMENTE con JSON válido:

Mensaje: "{mensaje}"

Responde con este formato exacto (sin texto adicional):
{{"es_cita": true/false}}

Considera que es una cita si:
- Menciona un día/hora específico para encontrarse
- Propone una reunión, cita o quedada
- Confirma un horario

Responde con false si:
- Es solo un saludo
- Pregunta información general
- No menciona día/hora para encontrarse
"""

        try:
            messages = [
                {"role": "system", "content": "Responde solo con JSON válido, sin texto adicional."},
                {"role": "user", "content": prompt}
            ]

            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.glm_service.api_key,
                "anthropic-version": "2023-06-01",
            }

            payload = {
                "model": self.glm_service.model,
                "max_tokens": 100,
                "messages": messages,
            }

            response = requests.post(
                f"{self.glm_service.base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=10,
            )

            if response.status_code == 200:
                data = response.json()
                texto = data["content"][0]["text"].strip()

                # Limpiar markdown
                if texto.startswith("```json"):
                    texto = texto[7:]
                if texto.startswith("```"):
                    texto = texto[3:]
                if texto.endswith("```"):
                    texto = texto[:-3]

                resultado = json.loads(texto.strip())
                es_cita = resultado.get("es_cita", False)

                print(f"[CITA_SERVICE] IA confirmó intención de cita: {es_cita}")
                return es_cita

        except Exception as e:
            print(f"[CITA_SERVICE] Error confirmando intención con IA: {e}")

        return False

    def _resumen_conversacion_para_cita(self, conversacion, limite=8):
        if not conversacion:
            return ""

        lineas = []
        mensajes = conversacion.mensajes.order_by('-creado_en')[:limite]
        for mensaje in reversed(list(mensajes)):
            contenido = re.sub(r'\s+', ' ', mensaje.contenido or '').strip()
            if not contenido or contenido == '[Audio]':
                continue
            rol = 'Cliente' if mensaje.origen == 'entrante' else 'Respuesta'
            if len(contenido) > 180:
                contenido = contenido[:177].rsplit(' ', 1)[0] + '...'
            lineas.append(f"{rol}: {contenido}")

        return "\n".join(lineas)

    def extraer_datos_cita(self, mensaje, perfil, conversacion=None):
        """Usa IA para extraer datos estructurados de la cita.

        Args:
            mensaje: Texto del mensaje
            perfil: PerfilAsistente del usuario

        Returns:
            dict: Datos extraídos de la cita con claves:
                - titulo: str
                - fecha_hora: datetime
                - duracion_minutos: int
                - ubicacion: str | None
                - descripcion: str | None
                - completo: bool (si se pudo extraer fecha y hora)
        """
        from datetime import datetime, timedelta
        import json

        print(f"[CITA_SERVICE] extraer_datos_cita llamado con mensaje: '{mensaje[:100]}...'")

        ahora = datetime.now()

        # Calcular fecha de mañana para el ejemplo
        manana = ahora + timedelta(days=1)

        # Few-shot: ejemplo concreto
        ejemplo_fecha = (ahora + timedelta(days=1)).strftime('%Y-%m-%d')

        contexto_conversacion = self._resumen_conversacion_para_cita(conversacion)

        prompt_extraccion = f"""Extrae los datos de una cita usando el ultimo mensaje y la conversación reciente.

Ultimo mensaje del cliente: "{mensaje}"

Hoy es: {ahora.strftime('%Y-%m-%d')} (mañana será {manana.strftime('%Y-%m-%d')})

Conversacion reciente con el cliente:
{contexto_conversacion or 'Sin historial reciente.'}

Reglas:
- Usa la conversación reciente para completar datos que el ultimo mensaje trae fragmentados.
- Si el cliente corrige el lugar, conserva la corrección más reciente. Ejemplo: "No jajaj en el meet" significa ubicación virtual/Google Meet, no presencial.
- Si el cliente dice "Metro" dentro de una conversación de cita, úsalo como ubicación si no hay una corrección posterior.
- Si el cliente pregunta "A qué horas", no inventes una hora; marca "completo": false si no hay hora acordada.
- Si el cliente confirma con "sí", "listo", "dale", "ok", "perfecto" o similar, usa la fecha/hora propuesta o acordada más reciente en la conversación.
- "titulo" debe describir el tipo de encuentro en pocas palabras.
- "descripcion" debe explicar de que se tratara la reunion segun lo conversado con el cliente, no solo repetir la hora.
- Si no hay tema claro, usa una descripcion breve basada en el ultimo mensaje del cliente.
- Mantén "descripcion" en maximo 180 caracteres.

EJEMPLO:
Mensaje: "mañana a las 11"
Respuesta: {{"titulo": "Cita", "fecha": "{ejemplo_fecha}", "hora": "11:00", "duracion_minutos": 60, "ubicacion": null, "descripcion": "Reunion para continuar la conversacion con el cliente.", "completo": true}}

Ahora responde al mensaje anterior. JSON sin markdown:
"""

        try:
            system_prompt = "Eres un extractor de información que responde solo con JSON válido. No incluyas markdown, explicaciones ni texto adicional."

            # Crear mensajes para la API
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_extraccion},
            ]

            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.glm_service.api_key,
                "anthropic-version": "2023-06-01",
            }

            payload = {
                "model": self.glm_service.model,
                "max_tokens": 2000,
                "messages": messages,
            }

            response = requests.post(
                f"{self.glm_service.base_url}/v1/messages",
                headers=headers,
                json=payload,
                timeout=15,
            )

            if response.status_code == 200:
                data = response.json()
                texto_respuesta = data["content"][0]["text"].strip()

                print(f"[CITA_SERVICE] 🔍 Respuesta cruda IA: {texto_respuesta}")

                # Limpiar markdown si existe
                if texto_respuesta.startswith("```json"):
                    texto_respuesta = texto_respuesta[7:]
                if texto_respuesta.startswith("```"):
                    texto_respuesta = texto_respuesta[3:]
                if texto_respuesta.endswith("```"):
                    texto_respuesta = texto_respuesta[:-3]

                datos_extraidos = json.loads(texto_respuesta.strip())

                print(f"[CITA_SERVICE] Datos extraídos JSON: {datos_extraidos}")
                datos_extraidos = self._ajustar_hora_ambigua(
                    datos_extraidos,
                    f"{contexto_conversacion}\n{mensaje}",
                )

                # Convertir fecha y hora a datetime
                if datos_extraidos.get("fecha") and datos_extraidos.get("hora") and not datos_extraidos.get("motivo_incompleto"):
                    fecha_hora_str = f"{datos_extraidos['fecha']} {datos_extraidos['hora']}"
                    try:
                        fecha_hora = datetime.strptime(fecha_hora_str, "%Y-%m-%d %H:%M")
                        datos_extraidos["fecha_hora"] = fecha_hora
                        datos_extraidos["completo"] = True
                        print(f"[CITA_SERVICE] ✅ Fecha y hora parseadas: {fecha_hora}")
                    except ValueError as e:
                        print(f"[CITA_SERVICE] ❌ Error parseando fecha/hora: {e}")
                        datos_extraidos["completo"] = False
                else:
                    print(f"[CITA_SERVICE] ❌ Falta fecha u hora. fecha={datos_extraidos.get('fecha')}, hora={datos_extraidos.get('hora')}")
                    datos_extraidos["completo"] = False

                return datos_extraidos

        except Exception as e:
            print(f"[CITA_SERVICE] ❌ Error extrayendo datos: {e}")
            import traceback
            traceback.print_exc()

        # Fallback: intentar extracción básica con regex
        print(f"[CITA_SERVICE] 🔄 Usando fallback para extracción")
        return self._extraccion_fallback(mensaje, ahora)

    def _extraccion_fallback(self, mensaje, ahora):
        """Extracción básica de datos cuando la IA falla."""
        from datetime import timedelta, datetime as dt

        datos = {
            "titulo": "Cita agendada",
            "fecha": None,
            "hora": None,
            "duracion_minutos": 60,
            "ubicacion": None,
            "descripcion": mensaje[:200],
            "completo": False,
            "fecha_hora": None,
        }

        mensaje_lower = mensaje.lower()

        # Extraer título básico
        if "reunión" in mensaje_lower or "reunion" in mensaje_lower:
            datos["titulo"] = "Reunión"
        elif "cita" in mensaje_lower:
            datos["titulo"] = "Cita"
        elif "llamada" in mensaje_lower:
            datos["titulo"] = "Llamada"
        elif "quedada" in mensaje_lower:
            datos["titulo"] = "Quedada"
        elif "negocio" in mensaje_lower:
            datos["titulo"] = "Reunión de negocio"

        if 'meet' in mensaje_lower or 'google meet' in mensaje_lower:
            datos["ubicacion"] = "Google Meet"
            datos["titulo"] = "Reunión virtual"
        elif re.search(r'\bmetro\b', mensaje_lower):
            datos["ubicacion"] = "Metro"

        # Mapeo de días de la semana
        dias_semana = {
            'lunes': 0, 'martes': 1, 'miércoles': 2, 'miercoles': 2,
            'jueves': 3, 'viernes': 4, 'sábado': 5, 'sabado': 5,
            'domingo': 6
        }

        # Detectar día de la semana
        for dia, numero_dia in dias_semana.items():
            if dia in mensaje_lower:
                hoy = ahora.weekday()
                dias_a_sumar = (numero_dia - hoy) % 7
                if dias_a_sumar == 0:
                    dias_a_sumar = 7  # Si es hoy, ir a la próxima semana
                fecha = ahora + timedelta(days=dias_a_sumar)
                datos["fecha"] = fecha.strftime("%Y-%m-%d")
                break

        # Detectar fecha relativa tradicional (con errores ortográficos comunes)
        # Buscar "mañana" con variaciones comunes
        maniana_pattern = re.search(r'\b(mañana|mañana|malana|manana|mnañana)\b', mensaje_lower, re.IGNORECASE)
        if maniana_pattern and not datos.get("fecha"):
            fecha = ahora + timedelta(days=1)
            datos["fecha"] = fecha.strftime("%Y-%m-%d")
        elif "pasado mañana" in mensaje_lower and not datos.get("fecha"):
            fecha = ahora + timedelta(days=2)
            datos["fecha"] = fecha.strftime("%Y-%m-%d")

        # Extraer hora con varios formatos
        # Formato HH:MM
        hora_match = re.search(r'(\d{1,2}):(\d{2})', mensaje)
        if hora_match:
            hora = int(hora_match.group(1))
            minuto = int(hora_match.group(2))
            datos["hora"] = f"{hora:02d}:{minuto:02d}"
        else:
            # Detectar formato "tipo X de la tarde/mañana/noche", "a las X pm/am", etc.
            tarde_match = re.search(r'(?:tipo|a las?|por la|en la)\s+(\d{1,2})\s*(?:de la)?\s*(tarde|noche|mañana|pm|am)', mensaje_lower)
            if tarde_match:
                hora_num = int(tarde_match.group(1))
                periodo = tarde_match.group(2)

                if periodo in ('mañana', 'am') and hora_num <= 12:
                    datos["hora"] = f"{hora_num:02d}:00"
                elif periodo == 'tarde' or (periodo == 'pm' and hora_num < 12):
                    # Convertir a formato 24h (1-12 de la tarde -> 13:00-23:00)
                    hora_24 = hora_num + 12
                    if hora_24 == 24:
                        hora_24 = 12  # 12 de la tarde = 12:00, no 24:00
                    datos["hora"] = f"{hora_24:02d}:00"
                elif periodo == 'noche' or periodo == 'pm':
                    # Convertir a formato 24h (1-12 de la noche -> 18:00-23:00)
                    hora_24 = hora_num + 12 if hora_num < 12 else hora_num
                    datos["hora"] = f"{hora_24:02d}:00"
            else:
                # Detectar "a las X" sin periodo - asumir AM (mañana) por defecto
                solo_hora_match = re.search(r'a las?\s+(\d{1,2})\b', mensaje_lower)
                if solo_hora_match:
                    hora_num = int(solo_hora_match.group(1))
                    if 1 <= hora_num <= 12:
                        datos["hora"] = f"{hora_num:02d}:00"
                    elif 13 <= hora_num <= 23:
                        datos["hora"] = f"{hora_num:02d}:00"

        # Crear fecha_hora si tenemos fecha y hora
        datos = self._ajustar_hora_ambigua(datos, mensaje)
        if datos.get("fecha") and datos.get("hora") and not datos.get("motivo_incompleto"):
            fecha_hora_str = f"{datos['fecha']} {datos['hora']}"
            try:
                datos["fecha_hora"] = dt.strptime(fecha_hora_str, "%Y-%m-%d %H:%M")
                datos["completo"] = True
            except ValueError:
                pass

        print(f"[CITA_FALLBACK] 📋 Resultado fallback: {datos}")

        return datos

    def verificar_conflicto_cita(self, perfil, fecha_hora, duracion_minutos, cita_id=None):
        """Verifica si existe un conflicto con otra cita ya agendada.

        Args:
            perfil: PerfilAsistente del usuario
            fecha_hora: datetime de la cita propuesta
            duracion_minutos: duración de la cita propuesta
            cita_id: ID de la cita a excluir (para ediciones)

        Returns:
            dict: Información del conflicto con claves:
                - tiene_conflicto: bool
                - cita_conflicto: Cita o None
                - motivo: str con la razón del conflicto
        """
        from asistente.models import Cita
        from datetime import timedelta

        # Asegurar que fecha_hora tenga timezone awareness
        hora_inicio = self._normalizar_inicio_cita(fecha_hora)

        hora_fin = hora_inicio + timedelta(minutes=duracion_minutos)
        inicio_dia, fin_dia = self._rango_dia_local(hora_inicio)

        print(f"[CITA_SERVICE] verificar_conflicto_cita:")
        print(f"  - hora_inicio: {hora_inicio}")
        print(f"  - hora_fin: {hora_fin}")
        print(f"  - duracion: {duracion_minutos} min")

        # Buscar citas del mismo día que solapen con el horario propuesto
        # Consideramos TODAS las citas excepto las canceladas
        # (pendiente, confirmada, completada) para evitar duplicados
        citas_conflicto = Cita.objects.filter(
            perfil=perfil,
            fecha_hora__gte=inicio_dia,
            fecha_hora__lt=fin_dia,
        ).exclude(
            estado='cancelada'
        )

        # Si estamos editando una cita, excluirla de la búsqueda
        if cita_id:
            citas_conflicto = citas_conflicto.exclude(id=cita_id)

        print(f"[CITA_SERVICE] Citas encontradas en el día: {citas_conflicto.count()}")

        # PRIMERO: Verificar si hay una cita exactamente a la misma hora
        cita_exacta = None
        for cita in citas_conflicto:
            cita_inicio_local = timezone.localtime(cita.fecha_hora).replace(second=0, microsecond=0)
            hora_inicio_local = timezone.localtime(hora_inicio).replace(second=0, microsecond=0)
            if cita_inicio_local == hora_inicio_local:
                cita_exacta = cita
                break

        if cita_exacta:
            print(f"[CITA_SERVICE] ¡CITA EXACTA DETECTADA a la misma hora!")
            return {
                'tiene_conflicto': True,
                'cita_conflicto': cita_exacta,
                'motivo': f'Ya tienes una cita agendada exactamente a esa hora: {cita_exacta.titulo} '
                         f'el {self._formatear_fecha_cita(cita_exacta.fecha_hora)}. '
                         f'Por favor, elige otro horario.',
                'todas_conflictos': [cita_exacta]
            }

        citas_en_conflicto = []
        for cita in citas_conflicto:
            cita_inicio = self._normalizar_inicio_cita(cita.fecha_hora)
            cita_fin = cita.fecha_hora + timedelta(minutes=cita.duracion_minutos)

            print(f"[CITA_SERVICE] Verificando cita existente: {cita.titulo}")
            print(f"  - estado: {cita.estado}")
            print(f"  - cita_inicio: {cita_inicio}")
            print(f"  - cita_fin: {cita_fin}")

            # Verificar si hay solapamiento de horarios
            # Hay conflicto si: (nuestra_inicio < cita_fin) AND (nuestra_fin > cita_inicio)
            if hora_inicio < cita_fin and hora_fin > cita_inicio:
                print(f"[CITA_SERVICE] ¡CONFLICTO DETECTADO!")
                citas_en_conflicto.append(cita)
            else:
                print(f"[CITA_SERVICE] Sin conflicto con esta cita")

        if citas_en_conflicto:
            # Priorizar citas confirmadas sobre pendientes
            cita_conflicto = next(
                (c for c in citas_en_conflicto if c.estado == 'confirmada'),
                citas_en_conflicto[0]
            )
            estado_str = "confirmada" if cita_conflicto.estado == 'confirmada' else "pendiente"
            return {
                'tiene_conflicto': True,
                'cita_conflicto': cita_conflicto,
                'motivo': f'Ya tienes una cita {estado_str} agendada: {cita_conflicto.titulo} '
                         f'el {self._formatear_fecha_cita(cita_conflicto.fecha_hora)} '
                         f'durante {cita_conflicto.duracion_minutos} minutos.',
                'todas_conflictos': citas_en_conflicto
            }

        return {
            'tiene_conflicto': False,
            'cita_conflicto': None,
            'motivo': '',
            'todas_conflictos': []
        }

    def calcular_horarios_disponibles(self, perfil, fecha, duracion_minutos=60,
                                      hora_inicio="08:00", hora_fin="18:00",
                                      intervalo_minutos=30):
        """Calcula horarios disponibles para un día específico.

        Args:
            perfil: PerfilAsistente del usuario
            fecha: date del día a consultar
            duracion_minutos: duración requerida para la cita
            hora_inicio: hora de inicio del día laboral (formato "HH:MM")
            hora_fin: hora de fin del día laboral (formato "HH:MM")
            intervalo_minutos: intervalo entre posibles horarios

        Returns:
            list: Lista de datetime con horarios disponibles
        """
        from asistente.models import Cita
        from datetime import datetime, timedelta, time

        # Convertir string a time
        hora_inicio_time = datetime.strptime(hora_inicio, "%H:%M").time()
        hora_fin_time = datetime.strptime(hora_fin, "%H:%M").time()

        # Crear datetime para inicio y fin del día
        inicio_dia = self._normalizar_fecha_hora(datetime.combine(fecha, hora_inicio_time))
        fin_dia = self._normalizar_fecha_hora(datetime.combine(fecha, hora_fin_time))

        # Obtener todas las citas del día (excepto canceladas)
        # Incluimos pendientes, confirmadas y completadas para evitar solapamientos
        citas_dia = Cita.objects.filter(
            perfil=perfil,
            fecha_hora__gte=inicio_dia,
            fecha_hora__lt=inicio_dia + timedelta(days=1),
        ).exclude(
            estado='cancelada'
        ).order_by('fecha_hora')

        # Marcar los horarios ocupados
        horarios_ocupados = []
        for cita in citas_dia:
            cita_inicio = cita.fecha_hora
            cita_fin = cita.fecha_hora + timedelta(minutes=cita.duracion_minutos)
            horarios_ocupados.append((cita_inicio, cita_fin))

        # Generar posibles horarios
        horarios_disponibles = []
        hora_actual = inicio_dia

        while hora_actual + timedelta(minutes=duracion_minutos) <= fin_dia:
            hora_fin_propuesta = hora_actual + timedelta(minutes=duracion_minutos)

            # Verificar si este horario choca con alguna cita existente
            disponible = True
            for cita_inicio, cita_fin in horarios_ocupados:
                # Hay conflicto si: (hora_actual < cita_fin) AND (hora_fin_propuesta > cita_inicio)
                if hora_actual < cita_fin and hora_fin_propuesta > cita_inicio:
                    disponible = False
                    break

            if disponible:
                horarios_disponibles.append(hora_actual)

            # Avanzar al siguiente intervalo
            hora_actual = hora_actual + timedelta(minutes=intervalo_minutos)

        return horarios_disponibles

    def formatear_horarios_disponibles(self, horarios):
        """Formatea una lista de horarios disponibles para mostrar al usuario.

        Args:
            horarios: list de datetime

        Returns:
            str: Mensaje formateado con horarios disponibles
        """
        if not horarios:
            return "No hay horarios disponibles para este día."

        from django.utils import timezone

        # Agrupar por mañana/tarde
        manana = []
        tarde = []

        for h in horarios:
            hora = h.hour
            if hora < 13:
                manana.append(h.strftime("%H:%M"))
            else:
                tarde.append(h.strftime("%H:%M"))

        lineas = []
        if manana:
            lineas.append(f"*Mañana:* {', '.join(manana[:10])}")
        if tarde:
            lineas.append(f"*Tarde:* {', '.join(tarde[:10])}")

        if len(manana) > 10 or len(tarde) > 10:
            lineas.append(f"\nY más horarios disponibles...")

        return "\n".join(lineas)

    def crear_cita(self, conversacion, datos_cita, linea_whatsapp=None, validar_conflicto=True):
        """Crea una cita en la base de datos con sus datos.

        Args:
            conversacion: Conversacion asociada
            datos_cita: dict con los datos de la cita
            linea_whatsapp: str opcional con la línea de WhatsApp
            validar_conflicto: bool para validar conflictos antes de crear

        Returns:
            tuple: (Cita o None, dict con resultado)
                El dict tiene: {'exito': bool, 'mensaje': str, 'conflicto': dict o None}
        """
        from asistente.models import Cita

        fecha_hora = self._normalizar_inicio_cita(datos_cita.get("fecha_hora"))
        duracion = datos_cita.get("duracion_minutos", 60)

        print(f"[CITA_SERVICE] crear_cita llamado - fecha_hora: {fecha_hora}, duracion: {duracion}")
        print(f"[CITA_SERVICE] validar_conflicto: {validar_conflicto}")

        try:
            with transaction.atomic():
                # Bloquear las citas del día mientras validamos y creamos. Así dos
                # mensajes simultáneos no pueden reservar el mismo espacio a la vez.
                if validar_conflicto and fecha_hora:
                    list(Cita.objects.select_for_update().filter(
                        perfil=conversacion.perfil,
                        fecha_hora__gte=self._rango_dia_local(fecha_hora)[0],
                        fecha_hora__lt=self._rango_dia_local(fecha_hora)[1],
                    ).exclude(estado='cancelada'))

                    # ─── VALIDACIÓN DE DISPONIBILIDAD DEL DÍA ─────────────────
                    horarios_disponibles = self.calcular_horarios_disponibles(
                        conversacion.perfil,
                        fecha_hora.date(),
                        duracion
                    )

                    print(f"[CITA_SERVICE] Horarios disponibles ese día: {len(horarios_disponibles)}")

                    # Si no hay horarios disponibles, el día está completo
                    if not horarios_disponibles:
                        mensaje = (
                            "⚠️ *El día está completamente ocupado*\n\n"
                            "No hay horarios disponibles para agendar en esa fecha. "
                            "Por favor, selecciona otro día diferente."
                        )
                        return None, {
                            'exito': False,
                            'mensaje': mensaje,
                            'conflicto': None,
                            'dia_completo': True
                        }

                    resultado_conflicto = self.verificar_conflicto_cita(
                        conversacion.perfil,
                        fecha_hora,
                        duracion
                    )

                    print(f"[CITA_SERVICE] resultado_conflicto: {resultado_conflicto}")

                    if resultado_conflicto['tiene_conflicto']:
                        mensaje = f"{resultado_conflicto['motivo']}\n\n"
                        mensaje += "*Horarios disponibles ese día:*\n"
                        mensaje += self.formatear_horarios_disponibles(horarios_disponibles)
                        mensaje += "\n\n¿Te gustaría agendar en otro horario?"

                        return None, {
                            'exito': False,
                            'mensaje': mensaje,
                            'conflicto': resultado_conflicto,
                            'horarios_disponibles': horarios_disponibles
                        }

                # Crear la cita solo después de validar disponibilidad.
                cita = Cita.objects.create(
                    perfil=conversacion.perfil,
                    conversacion=conversacion,
                    titulo=datos_cita.get("titulo", "Cita agendada"),
                    descripcion=datos_cita.get("descripcion"),
                    fecha_hora=fecha_hora,
                    duracion_minutos=duracion,
                    ubicacion=datos_cita.get("ubicacion"),
                    tipo_ubicacion='virtual' if datos_cita.get("ubicacion") and any(
                        palabra in datos_cita["ubicacion"].lower() for palabra in ["zoom", "meet", "teams", "virtual", "online"]
                    ) else 'presencial',
                    estado='pendiente',
                    recordatorio_minutos_antes=5,
                )
        except IntegrityError:
            resultado_conflicto = self.verificar_conflicto_cita(
                conversacion.perfil,
                fecha_hora,
                duracion
            )
            mensaje = (
                "Ese horario acaba de ser reservado por otra cita. "
                "Por favor, elige otro horario."
            )
            if resultado_conflicto.get('motivo'):
                mensaje = f"{resultado_conflicto['motivo']}"
            return None, {
                'exito': False,
                'mensaje': mensaje,
                'conflicto': resultado_conflicto,
            }

        # Crear recordatorio automático
        cita.crear_recordatorio()

        print(f"[CITA_SERVICE] Cita creada: {cita.titulo} - {cita.fecha_hora}")

        return cita, {
            'exito': True,
            'mensaje': 'Cita creada exitosamente',
            'conflicto': None
        }

    def enviar_recordatorio_cita(self, cita_id):
        """Envía un recordatorio por WhatsApp para una cita.

        Args:
            cita_id: ID de la cita

        Returns:
            tuple: (exito: bool, mensaje: str)
        """
        from asistente.models import Cita

        try:
            cita = Cita.objects.get(id=cita_id)
        except Cita.DoesNotExist:
            return False, "Cita no encontrada"

        if not cita.conversacion:
            return False, "La cita no tiene conversación asociada"

        # Extraer número de WhatsApp
        numero_whatsapp = cita.conversacion.numero_whatsapp
        linea = 'principal'
        if ':' in numero_whatsapp:
            partes = numero_whatsapp.split(':')
            linea = partes[0]
            numero_whatsapp = partes[1] if len(partes) > 1 else partes[0]

        mensaje = (
            f"Recordatorio de cita: {cita.titulo}\n"
            f"En {cita.recordatorio_minutos_antes} minutos ({self._formatear_fecha_cita(cita.fecha_hora)})\n"
        )
        if cita.ubicacion:
            mensaje += f"📍 {cita.ubicacion}\n"

        try:
            scheduler = obtener_scheduler()
            resultado = scheduler._enviar_whatsapp({
                'numero': numero_whatsapp,
                'mensaje': mensaje,
                'linea': linea,
            }, cita.perfil)

            if resultado.get('exito'):
                cita.recordatorio_enviado = True
                cita.save()
                return True, "Recordatorio enviado"
            else:
                return False, resultado.get('error', 'Error desconocido')

        except Exception as e:
            return False, str(e)

    def formatear_confirmacion_cita(self, cita):
        """Genera un mensaje de confirmación formateado.

        Args:
            cita: Objeto Cita

        Returns:
            str: Mensaje de confirmación formateado
        """
        cierres = [
            "Queda confirmada. Gracias por compartirnos tu disponibilidad.",
            "Listo, queda reservado ese espacio. Sera un gusto atenderte.",
            "Perfecto, dejamos ese espacio separado para revisarlo con calma.",
        ]
        cierre = cierres[cita.id % len(cierres)] if getattr(cita, 'id', None) else cierres[0]

        mensaje = f"{cierre}\n\n"
        mensaje += f"{cita.titulo}\n"
        mensaje += f"{self._formatear_fecha_cita(cita.fecha_hora)}\n"

        if cita.ubicacion:
            mensaje += f"📍 {cita.ubicacion}\n"

        if cita.duracion_minutos:
            mensaje += f"Duración: {cita.duracion_minutos} minutos\n"

        mensaje += f"\nTe enviaremos un recordatorio {cita.recordatorio_minutos_antes} minutos antes."

        return mensaje
