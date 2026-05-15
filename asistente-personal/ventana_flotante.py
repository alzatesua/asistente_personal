#!/usr/bin/env python3
"""
Ventana flotante del asistente personal.
"""

import threading
import time
import audioop
import os
import shutil
import urllib.parse
from io import BytesIO

import pygame
import requests
import tkinter as tk

from audio_visual_state import notify_audio_start, notify_audio_stop, read_audio_state


class VentanaAsistente:
    def __init__(self):
        self.api_url = "http://localhost:8005"
        self.session_id = "ventana-flotante-local"
        self.estado = "listo"
        self.expandida = False
        self.animando = False
        self.enviando = False
        self.tareas_monitoreadas = set()
        self.voice_levels = []
        self.voice_started_at = None
        self.voice_chunk_duration = 0.08
        self.external_voice_token = None
        self.width = 360
        self.collapsed_height = 70
        self.expanded_height = 124
        self.window_x = None
        self.window_y = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0

        self.colors = {
            "bg": "#0b1220",
            "panel": "#111827",
            "panel_2": "#172033",
            "border": "#314155",
            "text": "#f8fafc",
            "muted": "#94a3b8",
            "accent": "#3b82f6",
            "accent_hover": "#2563eb",
            "ready": "#34d399",
            "working": "#fbbf24",
            "voice": "#60a5fa",
            "voice_light": "#38bdf8",
            "idle_dot": "#475569",
            "error": "#f87171",
        }

        self.root = tk.Tk()
        self.root.title("Asistente")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.configure(bg=self.colors["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        try:
            pygame.mixer.init()
            self.audio_disponible = True
        except pygame.error as exc:
            print(f"Audio no disponible: {exc}")
            self.audio_disponible = False

        self.crear_ui()
        self.aplicar_geometria(self.collapsed_height)
        self.actualizar_estado("listo")
        self.root.after(250, self.monitorear_audio_externo)

    def aplicar_geometria(self, height):
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        if self.window_x is None or self.window_y is None:
            x = max(12, (screen_width - self.width) // 2)
            y = 28
        else:
            x = self.window_x
            y = self.window_y

        x = min(max(0, x), max(0, screen_width - self.width))
        y = min(max(0, y), max(0, screen_height - height))
        self.window_x = x
        self.window_y = y
        self.root.geometry(f"{self.width}x{height}+{x}+{y}")

    def crear_ui(self):
        self.card = tk.Frame(
            self.root,
            bg=self.colors["panel"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            bd=0,
        )
        self.card.pack(fill="both", expand=True, padx=0, pady=0)

        self.header = tk.Frame(self.card, bg=self.colors["panel"])
        self.header.pack(fill="x", padx=14, pady=(11, 9))
        self.header.configure(height=48)
        self.header.configure(cursor="fleur")
        self.header.pack_propagate(False)

        self.visual_canvas = tk.Canvas(
            self.header,
            width=104,
            height=38,
            bg=self.colors["panel"],
            highlightthickness=0,
            cursor="fleur",
        )
        self.visual_canvas.place(relx=0.5, rely=0.5, anchor="center")

        self.visual_glow = self.visual_canvas.create_oval(
            15, 7, 89, 31, fill="#172033", outline=""
        )

        self.idle_dots = []
        for index in range(10):
            cx = 25 + index * 6
            dot = self.visual_canvas.create_oval(
                cx - 2,
                19 - 2,
                cx + 2,
                19 + 2,
                fill=self.colors["idle_dot"],
                outline="",
            )
            self.idle_dots.append(dot)

        self.thinking_dots = []
        for index in range(3):
            cx = 42 + index * 10
            dot = self.visual_canvas.create_oval(
                cx - 4,
                19 - 4,
                cx + 4,
                19 + 4,
                fill=self.colors["working"],
                outline="",
                state="hidden",
            )
            self.thinking_dots.append(dot)

        self.voice_bars = []
        for index in range(9):
            x = 22 + index * 7
            bar = self.visual_canvas.create_line(
                x,
                14,
                x,
                24,
                fill=self.colors["voice"],
                width=4,
                capstyle=tk.ROUND,
                state="hidden",
            )
            self.voice_bars.append(bar)

        self.btn_toggle = tk.Canvas(
            self.header,
            width=36,
            height=36,
            bg=self.colors["panel"],
            highlightthickness=0,
            cursor="hand2",
        )
        self.btn_toggle_circle = self.btn_toggle.create_oval(
            2, 2, 34, 34, fill=self.colors["panel_2"], outline=self.colors["border"], width=1
        )
        self.btn_toggle_text = self.btn_toggle.create_text(18, 18, text="⌨", fill=self.colors["text"], font=("Inter", 11, "bold"))
        self.btn_toggle.bind("<Button-1>", lambda _event: self.toggle())
        self.btn_toggle.bind("<Enter>", lambda _event: self.btn_toggle.itemconfig(self.btn_toggle_circle, fill=self.colors["accent"]))
        self.btn_toggle.bind("<Leave>", lambda _event: self.btn_toggle.itemconfig(self.btn_toggle_circle, fill=self.colors["panel_2"]))
        self.btn_toggle.place(relx=1.0, rely=0.5, anchor="e")

        self.input_panel = tk.Frame(self.card, bg=self.colors["panel"])
        self.input_panel.grid_columnconfigure(0, weight=1)

        self.input_wrap = tk.Canvas(
            self.input_panel,
            height=38,
            bg=self.colors["panel"],
            highlightthickness=0,
        )
        self.input_wrap.grid(row=0, column=0, sticky="ew", padx=(14, 8), pady=(4, 14))
        self.input_wrap.bind("<Configure>", self.dibujar_fondo_input)
        self.input_wrap.bind("<Button-1>", self.enfocar_input)

        self.input_var = tk.StringVar()
        self.entry = tk.Entry(
            self.input_wrap,
            textvariable=self.input_var,
            bg=self.colors["bg"],
            fg=self.colors["text"],
            disabledbackground=self.colors["bg"],
            disabledforeground=self.colors["muted"],
            insertbackground=self.colors["accent"],
            font=("Inter", 10),
            relief="flat",
            bd=0,
        )
        self.entry_window = self.input_wrap.create_window(
            11,
            19,
            window=self.entry,
            anchor="w",
            height=26,
        )
        self.entry.bind("<Return>", self.on_send)
        self.entry.bind("<Escape>", lambda _event: self.collapse())
        self.entry.bind("<Button-1>", self.enfocar_input)

        self.btn_send = tk.Canvas(
            self.input_panel,
            width=38,
            height=38,
            bg=self.colors["panel"],
            highlightthickness=0,
            cursor="hand2",
        )
        self.btn_send_circle = self.btn_send.create_oval(
            2, 2, 36, 36, fill=self.colors["accent"], outline="", width=0
        )
        self.btn_send_text = self.btn_send.create_text(19, 19, text="➤", fill="white", font=("Inter", 11, "bold"))
        self.btn_send.bind("<Button-1>", self.on_send)
        self.btn_send.bind("<Enter>", lambda _event: self.btn_send.itemconfig(self.btn_send_circle, fill=self.colors["accent_hover"]))
        self.btn_send.bind("<Leave>", lambda _event: self.btn_send.itemconfig(self.btn_send_circle, fill=self.colors["accent"]))
        self.btn_send.grid(row=0, column=1, padx=(0, 14), pady=(4, 14))

        self.habilitar_arrastre(self.header)
        self.habilitar_arrastre(self.visual_canvas)
        self.card.bind("<Button-1>", self.expand_and_focus)
        self.root.bind("<Escape>", lambda _event: self.close())

    def habilitar_arrastre(self, widget):
        widget.bind("<ButtonPress-1>", self.iniciar_arrastre)
        widget.bind("<B1-Motion>", self.mover_ventana)
        widget.bind("<ButtonRelease-1>", self.terminar_arrastre)

    def iniciar_arrastre(self, event):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.drag_offset_x = event.x_root - self.root.winfo_x()
        self.drag_offset_y = event.y_root - self.root.winfo_y()

    def mover_ventana(self, event):
        height = self.expanded_height if self.expandida else self.collapsed_height
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        x = min(max(0, x), max(0, screen_width - self.width))
        y = min(max(0, y), max(0, screen_height - height))
        self.window_x = x
        self.window_y = y
        self.root.geometry(f"{self.width}x{height}+{x}+{y}")

    def terminar_arrastre(self, _event):
        self.window_x = self.root.winfo_x()
        self.window_y = self.root.winfo_y()

    def enfocar_input(self, _event=None):
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.pedir_foco_sin_decoracion()

    def pedir_foco_sin_decoracion(self):
        try:
            self.root.overrideredirect(False)
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            self.entry.focus_force()
            self.entry.icursor(tk.END)
            self.root.update_idletasks()
        finally:
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.after(20, self.entry.focus_force)
            self.root.after(40, lambda: self.entry.icursor(tk.END))

    def dibujar_fondo_input(self, _event=None):
        width = max(20, self.input_wrap.winfo_width())
        self.input_wrap.delete("input-bg")
        radius = 5
        x1, y1, x2, y2 = 1, 1, width - 1, 37
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        self.input_wrap.create_polygon(
            points,
            smooth=True,
            splinesteps=8,
            fill=self.colors["bg"],
            outline=self.colors["border"],
            width=1,
            tags="input-bg",
        )
        self.input_wrap.tag_lower("input-bg")
        self.input_wrap.coords(self.entry_window, 11, 19)
        self.input_wrap.itemconfig(self.entry_window, width=max(20, width - 22))

    def ocultar_visuales_activos(self):
        for item in self.thinking_dots + self.voice_bars:
            self.visual_canvas.itemconfig(item, state="hidden")

    def animar_visual(self, frame=0):
        if not self.animando:
            self.reset_visual()
            return

        self.ocultar_visuales_activos()
        for dot in self.idle_dots:
            self.visual_canvas.itemconfig(dot, state="hidden")

        if self.estado == "procesando":
            radii = [3, 4, 6, 5, 4, 3]
            lifts = [0, -1, -3, -1, 0, 1]
            for index, dot in enumerate(self.thinking_dots):
                step = (frame + index * 2) % len(radii)
                radius = radii[step]
                cx = 42 + index * 10
                cy = 19 + lifts[step]
                self.visual_canvas.coords(dot, cx - radius, cy - radius, cx + radius, cy + radius)
                self.visual_canvas.itemconfig(dot, fill=self.colors["working"], state="normal")
            self.visual_canvas.itemconfig(self.visual_glow, fill="#241e12")
            delay = 140
        elif self.estado == "sonando":
            volume = self.volumen_actual_voz()
            patterns = [
                [7, 14, 22, 12, 27, 16, 23, 11, 18],
                [15, 24, 10, 28, 18, 25, 13, 21, 8],
                [23, 12, 26, 15, 30, 11, 24, 17, 20],
                [10, 20, 16, 25, 13, 29, 19, 26, 12],
            ]
            base_heights = patterns[frame % len(patterns)]
            for index, (bar, height) in enumerate(zip(self.voice_bars, base_heights)):
                x = 22 + index * 7
                reactive_height = max(5, min(32, height * (0.32 + volume * 0.95)))
                top = 19 - reactive_height / 2
                bottom = 19 + reactive_height / 2
                color = self.colors["voice_light"] if index % 2 else self.colors["voice"]
                self.visual_canvas.coords(bar, x, top, x, bottom)
                self.visual_canvas.itemconfig(bar, fill=color, state="normal")
            self.visual_canvas.itemconfig(self.visual_glow, fill="#10233a")
            delay = 85
        else:
            self.reset_visual()
            return

        self.root.after(delay, lambda: self.animar_visual(frame + 1))

    def reset_visual(self):
        self.ocultar_visuales_activos()
        self.visual_canvas.itemconfig(self.visual_glow, fill="#172033")
        for index, dot in enumerate(self.idle_dots):
            cx = 25 + index * 6
            self.visual_canvas.coords(dot, cx - 2, 17, cx + 2, 21)
            self.visual_canvas.itemconfig(dot, fill=self.colors["idle_dot"], state="normal")

    def preparar_niveles_voz(self, sound):
        init = pygame.mixer.get_init()
        if not init:
            self.voice_levels = []
            return

        frequency, sample_size, channels = init
        sample_width = abs(sample_size) // 8
        frame_width = sample_width * channels
        chunk_size = max(frame_width, int(frequency * self.voice_chunk_duration) * frame_width)
        raw_audio = sound.get_raw()
        levels = []

        for start in range(0, len(raw_audio), chunk_size):
            chunk = raw_audio[start:start + chunk_size]
            if len(chunk) < frame_width:
                continue
            try:
                levels.append(audioop.rms(chunk, sample_width))
            except audioop.error:
                levels = []
                break

        if not levels:
            self.voice_levels = []
            return

        peak = max(levels) or 1
        self.voice_levels = [min(1.0, max(0.08, level / peak)) for level in levels]

    def preparar_niveles_voz_desde_archivo(self, ruta_archivo):
        try:
            sound = pygame.mixer.Sound(ruta_archivo)
            self.preparar_niveles_voz(sound)
            return sound
        except Exception as exc:
            print(f"[Audio] No pude leer niveles de volumen de {ruta_archivo}: {exc}")
            self.voice_levels = []
            return None

    def monitorear_audio_externo(self):
        try:
            data = read_audio_state()
            token = data.get("token")
            estado = data.get("state")
            ruta_archivo = data.get("path")
            actualizado = float(data.get("updated_at") or 0)

            if (
                estado == "playing"
                and token
                and token != self.external_voice_token
                and ruta_archivo
                and os.path.exists(ruta_archivo)
                and time.time() - actualizado < 120
            ):
                self.external_voice_token = token
                self.preparar_niveles_voz_desde_archivo(ruta_archivo)
                started_at = float(data.get("started_at") or time.time())
                self.voice_started_at = time.monotonic() - max(0, time.time() - started_at)
                self.actualizar_estado("sonando")
            elif self.external_voice_token and (estado != "playing" or token != self.external_voice_token):
                self.external_voice_token = None
                self.voice_started_at = None
                self.voice_levels = []
                if not self.enviando and self.estado == "sonando":
                    self.actualizar_estado("listo")
        except Exception as exc:
            print(f"[Audio] Error monitoreando audio externo: {exc}")
        finally:
            self.root.after(250, self.monitorear_audio_externo)

    def volumen_actual_voz(self):
        if not self.voice_levels or self.voice_started_at is None:
            return 0.45

        elapsed = max(0, time.monotonic() - self.voice_started_at)
        index = int(elapsed / self.voice_chunk_duration)
        if index >= len(self.voice_levels):
            return 0.25
        return self.voice_levels[index]

    def toggle(self):
        if self.expandida:
            self.collapse()
        else:
            self.expand_and_focus()

    def expand_and_focus(self, _event=None):
        if self.expandida:
            self.enfocar_input()
            return

        self.expandida = True
        self.btn_toggle.itemconfig(self.btn_toggle_text, text="⌃")
        self.input_panel.pack(fill="x")
        self.aplicar_geometria(self.expanded_height)
        self.root.after(40, self.enfocar_input)
        self.root.after(120, self.enfocar_input)
        self.root.after(240, self.enfocar_input)

    def collapse(self):
        if not self.expandida:
            return

        self.expandida = False
        self.btn_toggle.itemconfig(self.btn_toggle_text, text="⌨")
        self.input_panel.pack_forget()
        self.aplicar_geometria(self.collapsed_height)

    def actualizar_estado(self, estado):
        self.root.after(0, lambda: self._aplicar_estado(estado))

    def _aplicar_estado(self, estado):
        config = {
            "listo": (self.colors["ready"], "Listo"),
            "procesando": (self.colors["working"], "Trabajando"),
            "sonando": (self.colors["voice"], ""),
            "error": (self.colors["error"], "Error"),
            "terminado": (self.colors["ready"], "Terminado"),
        }
        color, texto = config.get(estado, config["listo"])
        self.estado = estado

        if estado in ("procesando", "sonando"):
            if not self.animando:
                self.animando = True
                self.animar_visual()
        else:
            self.animando = False
            self.reset_visual()

    def set_enviando(self, enviando):
        self.root.after(0, lambda: self._set_enviando(enviando))

    def _set_enviando(self, enviando):
        self.enviando = enviando
        self.entry.config(state="normal")
        self.btn_send.config(cursor="watch" if enviando else "hand2")
        self.btn_send.itemconfig(self.btn_send_circle, fill=self.colors["border"] if enviando else self.colors["accent"])
        if self.expandida:
            self.enfocar_input()

    def on_send(self, _event=None):
        if self.enviando:
            return

        texto = self.input_var.get().strip()
        if not texto:
            self.expand_and_focus()
            return

        self.input_var.set("")
        self.expand_and_focus()
        self.set_enviando(True)
        threading.Thread(target=self.procesar, args=(texto,), daemon=True).start()

    def procesar(self, texto):
        self.actualizar_estado("procesando")
        try:
            # Aumentado timeout a 90 segundos para permitir búsquedas web y comandos largos
            response = requests.post(
                f"{self.api_url}/api/chat/",
                json={"mensaje": texto, "session_id": self.session_id, "canal": "desktop"},
                timeout=90,
            )
            response.raise_for_status()
            data = response.json()
            tarea_id = data.get("tarea_id")
            comando_pendiente = data.get("comando_pendiente")

            audio_url = data.get("audio_url")
            if audio_url and self.audio_disponible:
                self.actualizar_estado("sonando")
            else:
                self.actualizar_estado("procesando" if tarea_id else "listo")
            self.reproducir_audio(audio_url)
            if data.get("accion_pendiente") and comando_pendiente:
                self.actualizar_estado("procesando")
                self.ejecutar_accion_pendiente(comando_pendiente)
                return
            if tarea_id:
                self.set_enviando(False)
                self.monitorear_tarea(tarea_id)
            else:
                self.actualizar_estado("listo")
        except requests.exceptions.Timeout as exc:
            print(f"Timeout: La petición tardó más de 90 segundos. Esto puede ocurrir al buscar en internet o ejecutar comandos largos.")
            print(f"Detalle: {exc}")
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        except requests.exceptions.ConnectionError as exc:
            print(f"Error de conexión: Asegúrate de que el servidor Django esté corriendo en {self.api_url}")
            print(f"Detalle: {exc}")
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        except requests.exceptions.HTTPError as exc:
            print(f"Error HTTP: {exc}")
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        except Exception as exc:
            print(f"Error inesperado: {exc}")
            import traceback
            traceback.print_exc()
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        finally:
            self.set_enviando(False)

    def ejecutar_accion_pendiente(self, comando):
        try:
            # Aumentado timeout a 120 segundos para comandos muy largos
            response = requests.post(
                f"{self.api_url}/api/acciones/ejecutar/",
                json={"comando": comando, "session_id": self.session_id, "canal": "desktop"},
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()

            tarea_id = data.get("tarea_id")
            audio_url = data.get("audio_url")
            if audio_url and self.audio_disponible:
                self.actualizar_estado("sonando")
                self.reproducir_audio(audio_url)

            if tarea_id:
                self.monitorear_tarea(tarea_id)
            else:
                self.actualizar_estado("listo")
        except requests.exceptions.Timeout as exc:
            print(f"Timeout ejecutando acción: El comando tardó más de 120 segundos.")
            print(f"Comando: {comando}")
            print(f"Detalle: {exc}")
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        except requests.exceptions.ConnectionError as exc:
            print(f"Error de conexión al ejecutar acción: Asegúrate de que el servidor esté corriendo en {self.api_url}")
            print(f"Detalle: {exc}")
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))
        except Exception as exc:
            print(f"Error ejecutando acción pendiente: {exc}")
            import traceback
            traceback.print_exc()
            self.actualizar_estado("error")
            self.root.after(3000, lambda: self.actualizar_estado("listo"))

    def monitorear_tarea(self, tarea_id):
        if tarea_id in self.tareas_monitoreadas:
            return

        self.tareas_monitoreadas.add(tarea_id)
        self.actualizar_estado("procesando")
        try:
            while True:
                response = requests.get(f"{self.api_url}/api/tareas/{tarea_id}/", timeout=10)
                response.raise_for_status()
                tarea = response.json().get("tarea", {})
                estado = tarea.get("estado")

                if estado in ("completada", "error"):
                    resultado = tarea.get("resultado") or tarea.get("error") or "Sin salida."
                    print(f"Tarea {tarea_id} {estado}:\n{resultado}")
                    resumen_audio = self.obtener_resumen_audio_tarea(tarea_id)
                    if resumen_audio:
                        resumen, audio_url = resumen_audio
                        print(f"Resumen hablado:\n{resumen}")
                        if audio_url and self.audio_disponible:
                            self.actualizar_estado("sonando")
                            self.reproducir_audio(audio_url)
                    self.actualizar_estado("terminado" if estado == "completada" else "error")
                    self.root.after(4200, lambda: self.actualizar_estado("listo"))
                    break

                time.sleep(1.5)
        except Exception as exc:
            print(f"Error monitoreando tarea {tarea_id}: {exc}")
            self.actualizar_estado("error")
            self.root.after(2500, lambda: self.actualizar_estado("listo"))
        finally:
            self.tareas_monitoreadas.discard(tarea_id)

    def obtener_resumen_audio_tarea(self, tarea_id):
        try:
            # Aumentado timeout a 60 segundos para generar el resumen de voz
            response = requests.get(f"{self.api_url}/api/tareas/{tarea_id}/resumen-voz/", timeout=60)
            response.raise_for_status()
            data = response.json()
            return data.get("resumen"), data.get("audio_url")
        except requests.exceptions.Timeout:
            print(f"Timeout obteniendo resumen de tarea {tarea_id}: La generación del audio tomó más de 60 segundos")
            return None
        except Exception as exc:
            print(f"No pude obtener resumen hablado de la tarea {tarea_id}: {exc}")
            return None

    def reproducir_audio(self, audio_url):
        if not audio_url or not self.audio_disponible:
            self.root.after(900, lambda: None)
            return

        url = audio_url if not audio_url.startswith("/") else self.api_url + audio_url

        # Verificar si hay múltiples partes de audio
        if "?parts=" in url or "&parts=" in url:
            # Extraer el nombre del archivo metadata
            if "?parts=" in url:
                base_url, parts_param = url.split("?parts=", 1)
            else:
                base_url, parts_param = url.split("&parts=", 1)

            parts_metadata = parts_param.split("&")[0]  # Remover cualquier parámetro adicional
            try:
                # Descargar el metadata con las URLs de todas las partes
                metadata_url = f"{self.api_url}/media/audios/{parts_metadata}"
                response = requests.get(metadata_url, timeout=10)
                response.raise_for_status()
                data = response.json()
                urls_partes = data.get("parts", [])

                if len(urls_partes) > 1:
                    print(f"[Audio] Reproduciendo {len(urls_partes)} partes de audio en secuencia")
                    self._reproducir_audio_secuencia(urls_partes)
                    return
            except Exception as e:
                print(f"[Audio] Error obteniendo metadata de partes: {e}")
                # Continuar con la URL normal

        # Descargar el audio a un archivo temporal
        try:
            print(f"[Audio] Descargando audio...")
            audio = requests.get(url, timeout=45)
            audio.raise_for_status()

            # Guardar en archivo temporal conservando la extensión real del audio.
            import tempfile
            suffix = self._extension_audio_desde_url(url)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio.content)
                tmp_path = tmp.name

            print(f"[Audio] Audio descargado: {len(audio.content)} bytes")

            # Usar reproductor externo solo para WAV largos; aplay/paplay no reproducen MP3.
            if suffix == ".wav" and len(audio.content) > 300000:
                print(f"[Audio] Audio largo, usando reproductor externo")
                self._reproducir_con_reproductor_externo(tmp_path)
            else:
                print(f"[Audio] Audio con pygame")
                self._reproducir_con_pygame(tmp_path)

        except requests.exceptions.Timeout:
            print(f"Timeout descargando audio: {url}")
        except requests.exceptions.ConnectionError:
            print(f"Error de conexión descargando audio: {url}")
        except Exception as exc:
            print(f"Error reproduciendo audio: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            self.voice_started_at = None
            self.voice_levels = []

    def _reproducir_con_reproductor_externo(self, ruta_archivo):
        """Reproduce audio usando un reproductor externo (mpg123/ffplay)."""
        import subprocess

        self.preparar_niveles_voz_desde_archivo(ruta_archivo)
        self.voice_started_at = time.monotonic()
        visual_token = notify_audio_start(ruta_archivo, source="ventana")

        # Intentar con diferentes reproductores
        reproductores = ["mpg123", "ffplay", "aplay", "paplay", "play"]

        try:
            for reproductor in reproductores:
                if shutil.which(reproductor):
                    try:
                        print(f"[Audio] Reproduciendo con {reproductor}")
                        if reproductor == "mpg123":
                            # mpg123 es el mejor para MP3
                            subprocess.run(
                                [reproductor, "-q", ruta_archivo],
                                check=False,
                                timeout=300
                            )
                        elif reproductor == "ffplay":
                            # ffplay (de ffmpeg)
                            subprocess.run(
                                [reproductor, "-nodisp", "-autoexit", ruta_archivo],
                                check=False,
                                timeout=300
                            )
                        elif reproductor == "aplay":
                            # aplay (ALSA)
                            subprocess.run(
                                [reproductor, ruta_archivo],
                                check=False,
                                timeout=300
                            )
                        elif reproductor == "paplay":
                            # paplay (PulseAudio)
                            subprocess.run(
                                [reproductor, ruta_archivo],
                                check=False,
                                timeout=300
                            )
                        elif reproductor == "play":
                            # play (SoX)
                            subprocess.run(
                                [reproductor, ruta_archivo],
                                check=False,
                                timeout=300
                            )
                        print(f"[Audio] Reproducción completada")
                        return
                    except subprocess.TimeoutExpired:
                        print(f"[Audio] Timeout reproduciendo con {reproductor}")
                        return
                    except Exception as e:
                        print(f"[Audio] Error con {reproductor}: {e}")
                        continue
        finally:
            notify_audio_stop(visual_token)

        print(f"[Audio] No se encontró reproductor externo, intentando pygame")
        self._reproducir_con_pygame(ruta_archivo)

    def _reproducir_con_pygame(self, ruta_archivo):
        """Reproduce audio usando pygame (para audios cortos)."""
        visual_token = notify_audio_start(ruta_archivo, source="ventana")
        try:
            if ruta_archivo.lower().endswith(".mp3"):
                print(f"[Audio] Reproduciendo MP3 con pygame.mixer.music")
                self.preparar_niveles_voz_desde_archivo(ruta_archivo)
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                pygame.mixer.music.load(ruta_archivo)
                self.voice_started_at = time.monotonic()
                pygame.mixer.music.play()

                while pygame.mixer.music.get_busy():
                    self.root.after(0, lambda: None)
                    pygame.time.delay(100)

                pygame.mixer.music.unload()
                print(f"[Audio] Reproducción MP3 completada")
                return

            sound = self.preparar_niveles_voz_desde_archivo(ruta_archivo)
            self.voice_started_at = time.monotonic()
            channel = sound.play() if sound else None

            # Simular animación de voz
            while channel and channel.get_busy():
                volumen = self.volumen_actual_voz()
                # Actualizar visualización
                if volumen > 0:
                    self.root.after(0, lambda: None)  # Mantener UI responsiva
                pygame.time.delay(100)

            print(f"[Audio] Reproducción pygame completada")
        except Exception as e:
            print(f"[Audio] Error con pygame: {e}")
        finally:
            notify_audio_stop(visual_token)

    def _reproducir_audio_secuencia(self, urls_partes):
        """Reproduce múltiples archivos de audio en secuencia."""
        import tempfile

        for idx, parte_url in enumerate(urls_partes):
            url = parte_url if not parte_url.startswith("/") else self.api_url + parte_url
            try:
                print(f"[Audio] Reproduciendo parte {idx + 1}/{len(urls_partes)}")
                audio = requests.get(url, timeout=30)
                audio.raise_for_status()

                # Guardar en archivo temporal conservando la extensión real del audio.
                suffix = self._extension_audio_desde_url(url)
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(audio.content)
                    tmp_path = tmp.name

                # Reproducir según tamaño
                if suffix == ".wav" and len(audio.content) > 300000:
                    self._reproducir_con_reproductor_externo(tmp_path)
                else:
                    self._reproducir_con_pygame(tmp_path)

                # Limpiar archivo temporal
                try:
                    os.unlink(tmp_path)
                except:
                    pass

                # Pequeña pausa entre partes
                time.sleep(0.2)

            except Exception as e:
                print(f"[Audio] Error reproduciendo parte {idx + 1}: {e}")
                import traceback
                traceback.print_exc()
                continue

        self.voice_started_at = None
        self.voice_levels = []

    def _extension_audio_desde_url(self, url):
        path = urllib.parse.urlparse(url).path.lower()
        if path.endswith(".wav"):
            return ".wav"
        if path.endswith(".mp3"):
            return ".mp3"
        return ".mp3"

    def close(self):
        self.animando = False
        try:
            pygame.mixer.quit()
        finally:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    VentanaAsistente().run()
