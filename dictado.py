#!/usr/bin/env python3
"""
Dictado por voz — widget flotante

Conecta al node-gateway en ws://HOST:PORT/ws y transcribe voz a texto
en tiempo real. El texto puede enviarse al cursor activo o copiarse
al portapapeles.

Comandos de voz:
  "listo enviar"  → escribe el texto transcrito donde esté el cursor
  "listo copiar"  → copia al portapapeles

Requisitos del sistema:
  sudo apt install python3-tk xdotool xclip portaudio19-dev

Atajo por defecto: Ctrl+Space (configurable en ⚙)
"""

import configparser
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

import re

import numpy as np
from PIL import Image, ImageDraw
import pyperclip
import pystray
import sounddevice as sd
import tkinter as tk
import websocket
from pynput import keyboard as pynput_kb
from Xlib import X as _X, XK as _XK
from Xlib import display as _Xdisplay
from Xlib import error as _Xerror

# ── Constantes ─────────────────────────────────────────────────────
DEFAULT_HOST   = "xeon"
DEFAULT_PORT   = "8085"
DEFAULT_HOTKEY = "<ctrl>+<space>"
SAMPLE_RATE    = 16000
BLOCK_FRAMES   = 4096

CONFIG_DIR  = Path.home() / ".config" / "vosk-dictado"
CONFIG_FILE = CONFIG_DIR / "config.ini"
LOG_FILE    = CONFIG_DIR / "dictado.log"

CMD_SEND = "listo enviar"
CMD_COPY = "listo copiar"

# ── Paleta ─────────────────────────────────────────────────────────
BG     = "#1a1f2e"
BG2    = "#232a3e"
BG3    = "#2d3650"
FG     = "#e2e8f0"
FG_DIM = "#64748b"
ACCENT = "#3b82f6"
RED    = "#ef4444"
GREEN  = "#22c55e"
GRAY   = "#475569"


# ── Hotkey global via XGrabKey (no necesita root ni Record extension) ────────

_MOD_MAP = {
    "ctrl":    _X.ControlMask,
    "control": _X.ControlMask,
    "alt":     _X.Mod1Mask,
    "shift":   _X.ShiftMask,
    "super":   _X.Mod4Mask,
    "cmd":     _X.Mod4Mask,
}


def _parse_combo(combo: str):
    """Convierte '<ctrl>+<space>' en (modmask, keysym)."""
    parts = [p.strip("<>").lower() for p in combo.split("+")]
    mod = 0
    keysym = None
    for p in parts:
        if p in _MOD_MAP:
            mod |= _MOD_MAP[p]
        else:
            for candidate in (f"XK_{p}", f"XK_{p.capitalize()}", f"XK_{p.upper()}"):
                ks = getattr(_XK, candidate, None)
                if ks is not None:
                    keysym = ks
                    break
    return mod, keysym


class _XGrabKeyWatcher(threading.Thread):
    """Escucha una combinación de teclas usando XGrabKey sobre la root window."""

    def __init__(self, combo: str, callback) -> None:
        super().__init__(daemon=True, name="xgrabkey")
        self._combo    = combo
        self._callback = callback
        self._active   = True
        self._disp     = None
        self._keycode  = None
        self._mod      = 0

    def run(self) -> None:
        try:
            self._disp = _Xdisplay.Display()
            root = self._disp.screen().root
            mod, keysym = _parse_combo(self._combo)
            if keysym is None:
                log.warning("[xgrabkey] No se reconoció la tecla en %r", self._combo)
                return
            self._mod     = mod
            self._keycode = self._disp.keysym_to_keycode(keysym)
            # Registrar ignorando NumLock y CapsLock
            for extra in (0, _X.Mod2Mask, _X.LockMask, _X.Mod2Mask | _X.LockMask):
                try:
                    root.grab_key(self._keycode, mod | extra, True,
                                  _X.GrabModeAsync, _X.GrabModeAsync)
                except _Xerror.error:
                    pass
            while self._active:
                try:
                    # Bloqueante hasta el próximo evento X
                    evt = self._disp.next_event()
                    if evt.type == _X.KeyPress and evt.detail == self._keycode:
                        self._callback()
                except _Xerror.ConnectionClosedError:
                    break
                except Exception:
                    break
        except Exception as exc:
            log.error("[xgrabkey] Error al registrar %r: %s", self._combo, exc)

    def stop(self) -> None:
        self._active = False
        if self._disp:
            try:
                root = self._disp.screen().root
                root.ungrab_key(_X.AnyKey, _X.AnyModifier)
                self._disp.flush()
                self._disp.close()
            except Exception:
                pass
            self._disp = None


# ── Configuración ──────────────────────────────────────────────────

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["server"] = {"host": DEFAULT_HOST, "port": DEFAULT_PORT}
    cfg["hotkey"] = {"combo": DEFAULT_HOTKEY}
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE, encoding="utf-8")
    return cfg


def save_config(cfg: configparser.ConfigParser) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        cfg.write(fh)


def _setup_logging() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )


_setup_logging()
log = logging.getLogger("dictado")


# ── Aplicación ─────────────────────────────────────────────────────

class DictadoApp:
    def __init__(self) -> None:
        self.config = load_config()

        # Estado
        self.recording    = False
        self.ws_app       = None
        self.ws_conn      = None
        self.audio_stream = None
        self.transcript   = ""
        self.partial      = ""
        self._audio_closing = False
        self._audio_level   = 0.0   # RMS 0.0-1.0, actualizado desde hilo de audio

        self._audio_q  = queue.Queue(maxsize=200)
        self._ui_queue = queue.Queue()
        self._hotkey_listener = None
        self._drag_x = 0
        self._drag_y = 0
        self._tray_icon = None

        self._build_ui()
        self._start_hotkey_listener()
        self._poll_ui_queue()
        self._poll_vu()  # animación VU meter
        self._start_tray()   # icono en bandeja del sistema
        # El stream de audio se inicia la primera vez que se graba

    # ── Propiedad: URL del WebSocket ────────────────────────────────

    @property
    def ws_url(self) -> str:
        h = self.config.get("server", "host", fallback=DEFAULT_HOST)
        p = self.config.get("server", "port", fallback=DEFAULT_PORT)
        return f"ws://{h}:{p}/ws"

    @property
    def health_url(self) -> str:
        h = self.config.get("server", "host", fallback=DEFAULT_HOST)
        p = self.config.get("server", "port", fallback=DEFAULT_PORT)
        return f"http://{h}:{p}/health"

    # ── Construcción de la interfaz ─────────────────────────────────

    def _build_ui(self) -> None:
        r = tk.Tk()
        r.title("Dictado")
        r.overrideredirect(True)          # sin borde/título del sistema
        r.attributes("-topmost", True)
        r.configure(bg=BG, highlightbackground=BG3, highlightthickness=1)
        r.geometry("310x260+80+80")           # ancho x alto + posición
        r.resizable(False, False)
        self.root = r

        # ── Cabecera arrastrable ─────────────────────────────────────
        hdr = tk.Frame(r, bg=BG2, pady=4)
        hdr.pack(fill="x")
        for w in (hdr,):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>",     self._drag_motion)

        self.dot = tk.Label(hdr, text="●", fg=GRAY, bg=BG2, font=("monospace", 9))
        self.dot.pack(side="left", padx=(8, 4))
        self.dot.bind("<ButtonPress-1>", self._drag_start)
        self.dot.bind("<B1-Motion>",     self._drag_motion)

        # Botones PRIMERO con side="right" → siempre clickeables aunque state_lbl sea largo
        # Controles: ×/─ ocultan ventana; ⚙ settings
        for sym, hover_fg, action in [
            ("×", RED, lambda: self.root.withdraw()),
            ("─", FG,  lambda: self.root.withdraw()),
            ("⚙", FG,  self._open_settings),
        ]:
            lbl = tk.Label(hdr, text=sym, fg=FG_DIM, bg=BG2,
                           font=("Segoe UI", 9), cursor="hand2", padx=5)
            lbl.pack(side="right")
            lbl.bind("<Button-1>", lambda e, a=action: a())
            lbl.bind("<Enter>",    lambda e, l=lbl, c=hover_fg: l.config(fg=c))
            lbl.bind("<Leave>",    lambda e, l=lbl: l.config(fg=FG_DIM))

        # Micrófono — a la izquierda del ⚙
        self.mic_btn = tk.Button(
            hdr, text="🎙", bg=BG2, fg=FG_DIM,
            activebackground=BG3, bd=0, relief="flat",
            cursor="hand2", font=("Segoe UI", 12),
            padx=3, pady=0, command=self.toggle_recording,
        )
        self.mic_btn.pack(side="right", padx=(0, 2))

        # VU meter — barras verticales que reaccionan al volumen del micrófono
        self.vu_canvas = tk.Canvas(
            hdr, width=30, height=22, bg=BG2,
            bd=0, highlightthickness=0,
        )
        self.vu_canvas.pack(side="right", padx=(2, 4))
        self._draw_vu(0.0)

        # Label de estado — al final, ocupa solo el espacio sobrante (no cubre botones)
        self.state_lbl = tk.Label(hdr, text="Listo", fg=FG_DIM, bg=BG2,
                                  font=("Segoe UI", 8), anchor="w")
        self.state_lbl.pack(side="left", fill="x", expand=True)
        self.state_lbl.bind("<ButtonPress-1>", self._drag_start)
        self.state_lbl.bind("<B1-Motion>",     self._drag_motion)

        # ── Área de texto + scrollbar ───────────────────────────────
        tf = tk.Frame(r, bg=BG, padx=6, pady=4)
        tf.pack(fill="both", expand=True)

        sb = tk.Scrollbar(tf, bg=BG2, troughcolor=BG, width=6, bd=0,
                          relief="flat", highlightthickness=0)
        sb.pack(side="right", fill="y")

        self.txt = tk.Text(
            tf, bg=BG2, fg=FG, insertbackground=FG,
            font=("Segoe UI", 10), wrap="word",
            relief="flat", padx=6, pady=6,
            state="normal", height=5,
            yscrollcommand=sb.set,
            selectbackground=BG3, selectforeground=FG,
        )
        self.txt.pack(fill="both", expand=True, side="left")
        sb.config(command=self.txt.yview)
        self.txt.bind("<Button-1>", lambda e: self.txt.focus_set())

        self.txt.tag_configure("final",   foreground=FG)
        self.txt.tag_configure("partial", foreground=FG_DIM,
                               font=("Segoe UI", 10, "italic"))

        # ── Botones centrados ────────────────────────────────────────
        bf = tk.Frame(r, bg=BG, pady=5)
        bf.pack(fill="x")

        inner = tk.Frame(bf, bg=BG)
        inner.pack()   # sin side= → centra horizontalmente

        for label, bg_col, abg, fg_col, cmd in [
            ("Enviar",  ACCENT,  "#2563eb", FG,     self._action_send),
            ("Copiar",  BG3,     "#3d4a6a", FG,     self._action_copy),
            ("Limpiar", BG2,     BG3,       FG_DIM, self._action_clear),
        ]:
            tk.Button(
                inner, text=label, bg=bg_col, fg=fg_col,
                activebackground=abg, activeforeground=FG,
                bd=0, relief="flat", cursor="hand2",
                font=("Segoe UI", 8),
                padx=10, pady=4, command=cmd,
            ).pack(side="left", padx=2)

        # ── Palabras clave ──────────────────────────────────────────
        kw = tk.Frame(r, bg=BG, pady=2)
        kw.pack(fill="x", padx=10)
        tk.Label(kw, text=f'"{CMD_SEND}"', fg=GRAY, bg=BG,
                 font=("Segoe UI", 7)).pack(side="left")
        tk.Label(kw, text="·", fg=GRAY, bg=BG,
                 font=("Segoe UI", 7)).pack(side="left", padx=3)
        tk.Label(kw, text=f'"{CMD_COPY}"', fg=GRAY, bg=BG,
                 font=("Segoe UI", 7)).pack(side="left")

        # ── Barra de estado ─────────────────────────────────────────
        combo = self.config.get("hotkey", "combo", fallback=DEFAULT_HOTKEY)
        _h = self.config.get("server", "host", fallback=DEFAULT_HOST)
        _p = self.config.get("server", "port", fallback=DEFAULT_PORT)
        self.status_lbl = tk.Label(
            r, text=f"{combo}  ·  {_h}:{_p}",
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
        )
        self.status_lbl.pack(side="bottom", pady=(0, 3))

        r.protocol("WM_DELETE_WINDOW", self._on_close)
        r.bind("<Escape>", lambda e: r.withdraw())

    # ── Helpers UI (siempre desde hilo principal vía after) ─────────

    def _drag_start(self, event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag_motion(self, event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _draw_vu(self, level: float) -> None:
        """VU meter: 5 barras que suben y bajan juntas con el nivel de audio real."""
        c = self.vu_canvas
        c.delete("all")
        n, bw, gap = 5, 4, 1
        c_w, c_h = 30, 22
        total_w = n * bw + (n - 1) * gap
        x0 = (c_w - total_w) // 2
        max_h = c_h - 4
        base_y = c_h - 2
        # Voz normal ≈ RMS 0.01-0.08 → ×12 para llenar el medidor
        amp = min(1.0, level * 12)
        bar_h = max(2, int(max_h * amp)) if (self.recording and amp > 0.015) else 0
        color = RED if amp > 0.75 else ("#eab308" if amp > 0.45 else GREEN)
        for i in range(n):
            x = x0 + i * (bw + gap)
            # Fondo del canal (siempre visible)
            c.create_rectangle(x, base_y - max_h, x + bw, base_y, fill=BG3, outline="")
            # Barra activa (sube/baja con el volumen)
            if bar_h > 0:
                c.create_rectangle(x, base_y - bar_h, x + bw, base_y, fill=color, outline="")

    def _poll_vu(self) -> None:
        """Actualiza el VU meter cada 60 ms desde el hilo principal."""
        self._draw_vu(self._audio_level if self.recording else 0.0)
        self.root.after(60, self._poll_vu)

    def _set_status(self, msg: str, color: str = GRAY) -> None:
        self.dot.config(fg=color)
        self.state_lbl.config(text=msg, fg=color)

    def _get_text_for_action(self) -> str:
        """Texto del widget excluyendo el parcial (itálica) al final."""
        ranges = self.txt.tag_ranges("partial")
        if ranges:
            return self.txt.get("1.0", ranges[0]).strip()
        return self.txt.get("1.0", "end-1c").strip()

    def _clear_partial_in_widget(self) -> None:
        """Elimina el texto parcial (itálica) del área de texto."""
        ranges = self.txt.tag_ranges("partial")
        if ranges:
            self.txt.delete(ranges[0], ranges[-1])

    def _append_final(self, text: str) -> None:
        """Añade texto final al widget, siempre al final del contenido editable."""
        self._clear_partial_in_widget()
        if text:
            current = self.txt.get("1.0", "end-1c")
            sep = " " if current.strip() else ""
            self.txt.insert("end", sep + text, "final")
            self.txt.see("end")

    def _refresh_partial(self) -> None:
        """Muestra/actualiza el texto parcial (itálica) al final del widget."""
        self._clear_partial_in_widget()
        if self.partial:
            current = self.txt.get("1.0", "end-1c")
            sep = " " if current.strip() else ""
            self.txt.insert("end", sep + self.partial + "…", "partial")
            self.txt.see("end")

    # ── Control de grabación ────────────────────────────────────────

    def toggle_recording(self) -> None:
        # Si la ventana está oculta (withdraw), restaurarla primero
        try:
            if self.root.state() in ("iconic", "withdrawn"):
                self._show_window()
        except Exception:
            pass
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        # Limpiar texto acumulado al iniciar nueva grabación
        self.transcript = ""
        self.partial    = ""
        self.txt.delete("1.0", "end")
        url = self.ws_url
        log.info("Iniciando conexión → %s", url)
        self._set_status(f"Conectando…", GRAY)
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        # Test HTTP /health antes del WebSocket
        def _probe_then_connect() -> None:
            health = self.health_url
            log.debug("HTTP health check → %s", health)
            try:
                req = urllib.request.urlopen(health, timeout=4)
                body = req.read()
                log.info("Health OK: %s", body.decode(errors="replace"))
            except urllib.error.URLError as exc:
                msg = f"No se puede alcanzar {health}: {exc.reason}"
                log.error(msg)
                self._ui_queue.put(("error", msg))
                return
            except OSError as exc:
                msg = f"No se puede alcanzar {health}: {exc}"
                log.error(msg)
                self._ui_queue.put(("error", msg))
                return
            self._connect_ws()
        threading.Thread(target=_probe_then_connect, daemon=True, name="probe").start()

    def _stop_recording(self) -> None:
        self.recording = False
        self.partial   = ""
        if self.ws_conn:
            try:
                self.ws_conn.send(json.dumps({"type": "stop"}))
            except Exception:
                pass
        self._stop_audio()
        self.mic_btn.config(fg=FG_DIM)
        self.dot.config(fg=GRAY)
        self.state_lbl.config(text="Listo", fg=FG_DIM)
        combo = self.config.get("hotkey", "combo", fallback=DEFAULT_HOTKEY)
        self.status_lbl.config(text=f"{combo}  ·  "
                               f"{self.config.get('server','host',fallback=DEFAULT_HOST)}"
                               f":{self.config.get('server','port',fallback=DEFAULT_PORT)}",
                               fg=GRAY)
        self._clear_partial_in_widget()
        threading.Thread(target=self._update_tray_icon, daemon=True).start()

    def _stop_audio(self) -> None:
        # Solo detiene el envío de frames; el stream ALSA se mantiene abierto.
        # El stream persistente se cierra únicamente en _on_close().
        pass

    # ── WebSocket ───────────────────────────────────────────────────

    def _connect_ws(self) -> None:
        url = self.ws_url
        log.info("WebSocket connect → %s", url)

        def on_open(ws) -> None:
            log.info("WebSocket abierto")
            self.ws_conn = ws
            ws.send(json.dumps({"type": "start", "settings": {}}))

        def on_message(ws, data: str) -> None:
            try:
                self._ui_queue.put(("msg", json.loads(data)))
            except Exception as exc:
                log.warning("on_message parse error: %s", exc)

        def on_error(ws, err) -> None:
            msg = str(err)
            log.error("WebSocket error: %s", msg)
            self._ui_queue.put(("error", msg))

        def on_close(ws, code, reason) -> None:
            log.info("WebSocket cerrado — code=%s reason=%s", code, reason)
            self._ui_queue.put(("close", f"code={code} reason={reason or '-'}"))

        self.ws_app = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        threading.Thread(
            target=self.ws_app.run_forever,
            daemon=True, name="ws-thread",
        ).start()

    # ── Captura de audio (stream persistente) ──────────────────────

    def _init_persistent_audio(self) -> None:
        """Abre el dispositivo de audio UNA sola vez y lo mantiene siempre activo.
        Solo envía frames al WS cuando self.recording es True.
        Esto evita el segfault de PortAudio/ALSA por abre/cierre repetidos."""
        def reader() -> None:
            try:
                with sd.RawInputStream(
                    samplerate=SAMPLE_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=BLOCK_FRAMES,
                ) as stream:
                    self.audio_stream = stream
                    log.info("Stream de audio ALSA abierto (persistente)")
                    while not self._audio_closing:
                        try:
                            data, _ = stream.read(BLOCK_FRAMES)
                        except sd.PortAudioError as exc:
                            log.warning("PortAudio xrun recuperado: %s", exc)
                            continue
                        except Exception as exc:
                            log.error("stream.read error: %s", exc)
                            break
                        # Calcular nivel RMS para VU meter (siempre, no solo al grabar)
                        try:
                            samples = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
                            rms = float(np.sqrt(np.mean(samples ** 2))) / 32768.0
                            self._audio_level = rms
                        except Exception:
                            pass
                        if self.recording and self.ws_conn:
                            try:
                                self.ws_conn.send(bytes(data), websocket.ABNF.OPCODE_BINARY)
                            except Exception:
                                pass
                    log.info("Stream de audio ALSA cerrado")
            except Exception as exc:
                log.error("No se pudo abrir el micrófono: %s", exc)
                self._ui_queue.put(("error", f"Micrófono: {exc}"))
            finally:
                self.audio_stream = None

        threading.Thread(target=reader, daemon=True, name="audio-persist").start()

    def _start_audio(self) -> None:
        # Iniciar el stream persistente la primera vez; las siguientes veces
        # self.audio_stream ya existe y self.recording=True activa el envío.
        if self.audio_stream is None and not self._audio_closing:
            self._init_persistent_audio()

    # ── Procesamiento de mensajes WebSocket ─────────────────────────

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("type", "")

        if t == "ready":
            self.recording = True
            self.mic_btn.config(fg=GREEN)           # verde = escuchando
            self._set_status("Escuchando…", GREEN)
            self._start_audio()                     # inicia stream de audio
            threading.Thread(target=self._update_tray_icon, daemon=True).start()

        elif t == "partial":
            self.partial = msg.get("text", "")
            self.dot.config(fg=GREEN)
            self.state_lbl.config(text="Transcribiendo…", fg=GREEN)
            self._refresh_partial()

        elif t == "final":
            self.partial = ""
            # Acumular localmente; NO usar fullText del servidor porque el
            # servidor puede resetear su buffer cuando invoca a la IA.
            new_piece = msg.get("text", "").strip()
            if new_piece:
                self.transcript = (
                    (self.transcript + " " + new_piece).strip()
                    if self.transcript else new_piece
                )
            self.dot.config(fg=GREEN)
            self.state_lbl.config(text="Escuchando…", fg=GREEN)
            self._append_final(new_piece)
            self._check_commands(self.transcript)

        elif t == "server_status":
            self._set_status(msg.get("message", ""), GRAY)

        # Ignorar: config_ack, user_text, assistant, pong, stream_keepalive

    def _check_commands(self, text: str) -> None:
        """Detecta 'listo enviar' y 'listo copiar' en el texto final."""
        lower    = text.lower()
        send_idx = lower.rfind(CMD_SEND)
        copy_idx = lower.rfind(CMD_COPY)

        if send_idx != -1 and (copy_idx == -1 or send_idx > copy_idx):
            clean = text[:send_idx].rstrip()
            self._stop_recording()
            self.transcript = clean
            self.txt.delete("1.0", "end")
            if clean:
                self.txt.insert("end", clean, "final")
            self._set_status("Enviando…", GREEN)
            self._type_at_cursor(clean)   # retira la ventana al escribir
            return

        if copy_idx != -1:
            clean = text[:copy_idx].rstrip()
            self._stop_recording()
            pyperclip.copy(clean)
            self.transcript = clean
            self.txt.delete("1.0", "end")
            if clean:
                self.txt.insert("end", clean, "final")
            self._set_status("Copiado ✓", GREEN)
            self.root.after(400, self.root.withdraw)

    # ── Acciones de botones ─────────────────────────────────────────

    def _action_send(self) -> None:
        text = self._get_text_for_action()
        if not text:
            return
        if self.recording:
            self._stop_recording()
        self._set_status("Enviando…", GREEN)
        self._type_at_cursor(text)

    def _action_copy(self) -> None:
        text = self._get_text_for_action()
        if text:
            pyperclip.copy(text)
            self._set_status("Copiado ✓", GREEN)
            self.root.after(400, self.root.withdraw)

    def _action_clear(self) -> None:
        self.transcript = ""
        self.partial    = ""
        self.txt.delete("1.0", "end")
        self._set_status("Listo", GRAY)

    def _action_test(self) -> None:
        """Prueba HTTP /health y WS /ws — muestra resultado en UI y log."""
        health = self.health_url
        ws_url = self.ws_url
        self._set_status(f"Probando {health} …", GRAY)
        log.info("=== TEST CONEXION ===")
        log.info("health_url = %s", health)
        log.info("ws_url     = %s", ws_url)

        def _run() -> None:
            # 1) HTTP health
            try:
                req = urllib.request.urlopen(health, timeout=5)
                body = req.read().decode(errors="replace")
                log.info("HTTP /health OK: %s", body)
                self._ui_queue.put(("test_ok", f"HTTP OK: {body}"))
            except urllib.error.URLError as exc:
                msg = f"HTTP fallo: {exc.reason}"
                log.error(msg)
                self._ui_queue.put(("test_fail", msg))
                return
            except OSError as exc:
                msg = f"HTTP fallo: {exc}"
                log.error(msg)
                self._ui_queue.put(("test_fail", msg))
                return

            # 2) WebSocket handshake
            result = {"ok": False, "msg": ""}
            ev = threading.Event()

            def on_open(ws):
                result["ok"]  = True
                result["msg"] = "WS handshake OK"
                # Cerrar en hilo aparte para no romper el loop interno del cliente
                threading.Timer(0.3, ws.close).start()
                ev.set()

            def on_error(ws, err):
                result["msg"] = f"WS error: {err}"
                ev.set()

            def on_close(ws, code, reason):
                if not ev.is_set():
                    result["msg"] = result["msg"] or f"WS cerrado code={code}"
                    ev.set()

            app = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )
            t = threading.Thread(target=app.run_forever, daemon=True)
            t.start()
            ev.wait(timeout=6)

            if result["ok"]:
                log.info(result["msg"])
                self._ui_queue.put(("test_ok", f"HTTP OK + {result['msg']}"))
            else:
                log.error("WS fallo: %s", result["msg"])
                self._ui_queue.put(("test_fail", f"HTTP OK, WS fallo: {result['msg']}"))

        threading.Thread(target=_run, daemon=True, name="test-conn").start()

    # ── Escritura en cursor activo ──────────────────────────────────

    def _type_at_cursor(self, text: str) -> None:
        """Minimiza la ventana y escribe el texto en la ventana que tenga foco."""
        if not text:
            return

        def _do() -> None:
            # Ocultar ventana con withdraw (compatible con overrideredirect).
            # Se restaura con el hotkey (toggle_recording detecta estado withdrawn).
            self.root.after(0, self.root.withdraw)
            time.sleep(0.12)
            try:
                # Usar portapapeles + Ctrl+V para soporte fiable de tildes, Ñ y Unicode.
                # xdotool type falla con caracteres no ASCII según el layout del teclado.
                pyperclip.copy(text)
                subprocess.run(
                    ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                    check=True, timeout=5,
                )
            except FileNotFoundError:
                # xdotool no instalado → fallback con pynput + portapapeles
                try:
                    pyperclip.copy(text)
                    kb = pynput_kb.Controller()
                    with kb.pressed(pynput_kb.Key.ctrl):
                        kb.tap('v')
                except Exception:
                    pass
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    # ── Tecla de acceso rápido global ──────────────────────────────

    def _start_hotkey_listener(self) -> None:
        combo = self.config.get("hotkey", "combo", fallback=DEFAULT_HOTKEY)

        def activate() -> None:
            self.root.after(0, self.toggle_recording)

        watcher = _XGrabKeyWatcher(combo, activate)
        watcher.start()
        self._hotkey_listener = watcher

    def _restart_hotkey(self) -> None:
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None
        self._start_hotkey_listener()

    # ── Diálogo de configuración ────────────────────────────────────

    def _open_settings(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Configuración")
        dlg.configure(bg=BG)
        dlg.geometry("320x500")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        # Centrar en pantalla (evitar problemas con overrideredirect en parent)
        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        x = (sw - 320) // 2
        y = (sh - 500) // 2
        dlg.geometry(f"320x500+{x}+{y}")
        dlg.lift()
        dlg.focus_force()

        def lbl_entry(parent: tk.Widget, label: str, default: str) -> tk.StringVar:
            f = tk.Frame(parent, bg=BG)
            f.pack(fill="x", padx=14, pady=3)
            tk.Label(
                f, text=label, fg=FG_DIM, bg=BG,
                font=("Segoe UI", 9), width=16, anchor="w",
            ).pack(side="left")
            var = tk.StringVar(value=default)
            tk.Entry(
                f, textvariable=var, bg=BG2, fg=FG,
                insertbackground=FG, relief="flat",
                font=("Segoe UI", 9),
            ).pack(side="left", fill="x", expand=True)
            return var

        tk.Label(
            dlg, text="Servidor node-gateway", fg=FG, bg=BG,
            font=("Segoe UI", 9, "bold"),
        ).pack(pady=(12, 2))

        h_var = lbl_entry(dlg, "Host / IP:",
                          self.config.get("server", "host", fallback=DEFAULT_HOST))
        p_var = lbl_entry(dlg, "Puerto:",
                          self.config.get("server", "port", fallback=DEFAULT_PORT))

        tk.Label(
            dlg, text="Atajo de teclado", fg=FG, bg=BG,
            font=("Segoe UI", 9, "bold"),
        ).pack(pady=(10, 2))

        k_var = lbl_entry(dlg, "Combinación:",
                          self.config.get("hotkey", "combo", fallback=DEFAULT_HOTKEY))

        tk.Label(
            dlg,
            text="Ej:  <ctrl>+<space>   <ctrl>+<shift>+d   <alt>+v",
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
        ).pack()

        def save() -> None:
            self.config["server"]["host"] = h_var.get().strip()
            self.config["server"]["port"] = p_var.get().strip()
            if "hotkey" not in self.config:
                self.config.add_section("hotkey")
            self.config["hotkey"]["combo"] = k_var.get().strip()
            save_config(self.config)
            self._restart_hotkey()
            combo = k_var.get().strip()
            _h = self.config.get("server", "host", fallback=DEFAULT_HOST)
            _p = self.config.get("server", "port", fallback=DEFAULT_PORT)
            self.status_lbl.config(text=f"{combo}  ·  {_h}:{_p}")
            self.state_lbl.config(text="Guardado ✓", fg=GREEN)
            self.root.after(2000, lambda: self.state_lbl.config(text="Listo", fg=FG_DIM))
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(pady=10)

        tk.Button(
            btn_row, text="Probar", bg="#0f4c22", fg=FG,
            activebackground="#166534", activeforeground=FG,
            bd=0, relief="flat", cursor="hand2",
            font=("Segoe UI", 9),
            padx=10, pady=5,
            command=lambda: [dlg.destroy(), self._action_test()],
        ).pack(side="left", padx=4)

        tk.Button(
            btn_row, text="Guardar", bg=ACCENT, fg=FG,
            activebackground="#2563eb", activeforeground=FG,
            bd=0, relief="flat", cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            padx=14, pady=5, command=save,
        ).pack(side="left", padx=4)

        tk.Button(
            btn_row, text="Salir", bg="#7f1d1d", fg=FG,
            activebackground="#991b1b", activeforeground=FG,
            bd=0, relief="flat", cursor="hand2",
            font=("Segoe UI", 9),
            padx=10, pady=5,
            command=lambda: [dlg.destroy(), self._quit_app()],
        ).pack(side="left", padx=4)

        # ── Acerca de / Créditos ─────────────────────────────────────────────
        tk.Frame(dlg, bg=BG3, height=1).pack(fill="x", padx=14, pady=(12, 0))
        tk.Label(
            dlg, text="Acerca de", fg=FG, bg=BG,
            font=("Segoe UI", 9, "bold"),
        ).pack(pady=(6, 2))

        credits = (
            "Dictado por Voz en Español\n"
            "Autor: enlinea777@gmail.com\n\n"
            "Licencia: GNU General Public License v3.0\n\n"
            "Componentes de código abierto:\n"
            "  Python 3 — PSF License\n"
            "  Tkinter / Tcl-Tk — Tcl License\n"
            "  Vosk — Apache 2.0\n"
            "  websocket-client ≥1.6 — Apache 2.0\n"
            "  sounddevice ≥0.4 — MIT\n"
            "  NumPy ≥1.24 — BSD 3-Clause\n"
            "  pynput ≥1.7 — LGPLv3\n"
            "  python-xlib ≥0.33 — LGPLv2+\n"
            "  pystray ≥0.19 — LGPLv3\n"
            "  Pillow ≥9.0 — HPND\n"
            "  pyperclip ≥1.8 — BSD 3-Clause\n"
            "  xdotool — MIT"
        )
        tk.Label(
            dlg, text=credits,
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
            justify="left", anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))

    # ── Bucle de eventos UI (cola thread-safe) ──────────────────────

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                evt, data = self._ui_queue.get_nowait()
                if evt == "msg":
                    self._handle_msg(data)
                elif evt == "error":
                    self._set_status(f"Error: {data}", RED)
                    self._stop_audio()
                    self.recording = False
                    self._audio_level = 0.0
                    self.mic_btn.config(fg=FG_DIM)
                elif evt == "close":
                    detail = data or ""
                    if self.recording:
                        self.recording = False
                        self._stop_audio()
                        self.mic_btn.config(fg=GRAY)
                        self._set_status(f"Conexión cerrada  {detail}", RED)
                elif evt == "test_ok":
                    self._set_status(f"✓ {data}", GREEN)
                elif evt == "test_fail":
                    self._set_status(f"✗ {data}", RED)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_ui_queue)

    # ── Cierre limpio ───────────────────────────────────────────────

    def _on_close(self) -> None:
        """Oculta la ventana; la app sigue en segundo plano. Usa el hotkey para volver."""
        self.root.withdraw()

    # ── Bandeja del sistema (system tray) ───────────────────────────

    def _make_tray_icon_image(self, recording: bool = False) -> Image.Image:
        """Genera un icóno de micrófono 64x64 para la bandeja.
        Verde = grabando  |  Gris = en espera."""
        s = 64
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        mic_color = (34, 197, 94, 255) if recording else (100, 116, 139, 255)
        # Fondo oscuro redondeado
        d.ellipse([0, 0, s - 1, s - 1], fill=(26, 31, 46, 255))
        d.ellipse([3, 3, s - 4, s - 4], fill=(35, 42, 62, 255))
        # Cuerpo del micrófono
        mw, mh, my = 20, 28, 8
        mx = (s - mw) // 2
        d.rounded_rectangle([mx, my, mx + mw, my + mh], radius=10, fill=mic_color)
        # Arco de soporte
        cx = s // 2
        d.arc([cx - 16, my + 12, cx + 16, my + 40], start=0, end=180,
              fill=mic_color, width=3)
        # Palo vertical
        pole_top = my + mh + 1
        d.rectangle([cx - 2, pole_top, cx + 2, pole_top + 9], fill=mic_color)
        # Base horizontal
        base_y = pole_top + 9
        d.rectangle([cx - 11, base_y, cx + 11, base_y + 3], fill=mic_color)
        return img

    def _start_tray(self) -> None:
        """Inicia el icono en la bandeja del sistema en un hilo dedicado."""
        def on_show(icon, item):
            self.root.after(0, self._show_window)

        def on_quit(icon, item):
            icon.stop()
            self.root.after(0, self._quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("Mostrar / Ocultar", on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", on_quit),
        )
        img = self._make_tray_icon_image(False)
        icon = pystray.Icon(
            "dictado",
            img,
            "Dictado por voz",
            menu,
        )
        self._tray_icon = icon
        threading.Thread(target=icon.run, daemon=True, name="tray").start()

    def _update_tray_icon(self) -> None:
        """Actualiza el color del icono de bandeja según el estado de grabación."""
        if self._tray_icon:
            try:
                self._tray_icon.icon = self._make_tray_icon_image(self.recording)
            except Exception:
                pass

    def _show_window(self) -> None:
        """Restaura la ventana desde cualquier estado oculto."""
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()

    def _quit_app(self) -> None:
        """Cierre completo de la aplicación."""
        if self.recording:
            self._stop_recording()
        self._audio_closing = True
        if self.audio_stream:
            try:
                self.audio_stream.abort()
            except Exception:
                pass
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ── Punto de entrada ────────────────────────────────────────────────

if __name__ == "__main__":
    app = DictadoApp()
    app.run()
