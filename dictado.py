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
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import urllib.error
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pyperclip
import pystray
import sounddevice as sd
import tkinter as tk
import websocket

# X11 sólo existe en Linux; en Windows la app usa la API nativa.
try:
    from Xlib import X as _X, XK as _XK
    from Xlib import display as _Xdisplay
    from Xlib import error as _Xerror
    HAVE_X11 = True
except Exception:                                    # pragma: no cover
    _X = _XK = _Xdisplay = _Xerror = None
    HAVE_X11 = False

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# pynput se importa bajo demanda: al importarlo abre una conexión X y en
# sesiones Wayland sin Xauthority válido aborta el arranque de la app.
_pynput_kb = None


def _pynput():
    """Devuelve el módulo pynput.keyboard, importándolo la primera vez."""
    global _pynput_kb
    if _pynput_kb is None:
        from pynput import keyboard as kb
        _pynput_kb = kb
    return _pynput_kb

# ── Constantes ─────────────────────────────────────────────────────
DEFAULT_HOST   = "xeon"
DEFAULT_PORT   = "8085"
# En macOS Ctrl+Space está ocupado por Spotlight / cambio de idioma.
DEFAULT_HOTKEY = "<ctrl>+<alt>+<space>" if IS_MACOS else "<ctrl>+<space>"
# Copa la transcripción sin necesidad de foco (la ventana sin bordes no
# recibe teclado en Wayland). Se registra como segundo atajo global.
DEFAULT_COPY_HOTKEY = "<ctrl>+<alt>+c"
SAMPLE_RATE    = 16000
BLOCK_FRAMES   = 4096

if IS_WINDOWS:
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "vosk-dictado"
elif IS_MACOS:
    CONFIG_DIR = (Path.home() / "Library" / "Application Support"
                  / "vosk-dictado")
else:
    CONFIG_DIR = Path.home() / ".config" / "vosk-dictado"
CONFIG_FILE = CONFIG_DIR / "config.ini"
LOG_FILE    = CONFIG_DIR / "dictado.log"

CMD_SEND = "listo enviar"
CMD_COPY = "listo copiar"

# ── Detección de sesión ────────────────────────────────────────────
# En Wayland el atajo global se registra con el portal GlobalShortcuts
# y el texto se inyecta con uinput (evdev); en X11 se usa XGrabKey +
# xdotool, igual que siempre.


def _detect_wayland() -> bool:
    if IS_WINDOWS or IS_MACOS:
        return False
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    return os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"


IS_WAYLAND = _detect_wayland()

# Combinación de teclas para pegar (portapapeles + tecla de pegado)
DEFAULT_PASTE_KEY         = "ctrl+v"
DEFAULT_PASTE_KEY_TERMINAL = "ctrl+shift+v"
# En Wayland no se puede saber si el foco está en una terminal, y Ctrl+V no
# pega en ellas: Shift+Insert funciona en terminales y en casi todas las apps.
DEFAULT_PASTE_KEY_WAYLAND = "shift+insert"
# Cómo enviar el texto en Wayland: "escribir" (teclado virtual) o "pegar".
DEFAULT_SEND_MODE = "pegar" if IS_MACOS else "escribir"

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

_MOD_MAP = {}
if HAVE_X11:
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


# ── Atajo global en Wayland: portal GlobalShortcuts ──────────────────
#
# El portal XDG (org.freedesktop.portal.GlobalShortcuts) es la única vía
# soportada para registrar atajos globales en una sesión Wayland: la pide
# el compositor, así que funciona con aplicaciones nativas de Wayland y
# también con las que corren bajo XWayland.
#
# Referencia:
#   https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html

_XDG_MOD_MAP = {
    "ctrl":    "Control",
    "control": "Control",
    "alt":     "Alt",
    "shift":   "Shift",
    "super":   "Super",
    "cmd":     "Super",
    "meta":    "Super",
}


def _combo_to_xdg_trigger(combo: str) -> str:
    """Convierte '<ctrl>+<space>' en el trigger XDG '<Control>space'."""
    mods, keys = [], []
    for raw in combo.split("+"):
        part = raw.strip().strip("<>").strip()
        if not part:
            continue
        low = part.lower()
        if low in _XDG_MOD_MAP:
            mods.append(_XDG_MOD_MAP[low])
        else:
            keys.append(part if len(part) > 1 else low)
    return "".join(f"<{m}>" for m in mods) + "".join(keys)


class _PortalShortcutWatcher:
    """Registra el atajo global mediante el portal GlobalShortcuts.

    Funciona en cualquier sesión (el portal existe también bajo X11) pero
    sólo se usa bajo Wayland, donde XGrabKey no es suficiente.
    """

    _BUS_NAME   = "org.freedesktop.portal.Desktop"
    _OBJ_PATH   = "/org/freedesktop/portal/desktop"
    _IFACE      = "org.freedesktop.portal.GlobalShortcuts"
    def __init__(self, shortcuts, callback, parent_window: str = "") -> None:
        # shortcuts: [(id, descripción, trigger XDG), ...]
        self._shortcuts     = list(shortcuts)
        self._ids           = {sid for sid, _, _ in self._shortcuts}
        self._callback      = callback
        self._parent_window = parent_window
        self._active        = True
        self._state         = "pending"   # pending | ok | error
        self._error         = ""
        self._loop          = None
        self._thread        = None
        self._session       = ""
        self._bus           = None

    # -- API pública -------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="portal-hotkey")
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        loop = self._loop
        if loop:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        self._loop = None

    # -- Interno -----------------------------------------------------
    def _run(self) -> None:
        try:
            import asyncio
            asyncio.run(self._amain())
        except Exception as exc:
            self._state = "error"
            self._error = str(exc)
            log.warning("[portal] No se pudo registrar el atajo: %s", exc)
            if "app id" in str(exc).lower():
                log.warning("[portal] El proceso no tiene 'app id': el portal "
                            "sólo concede app id a apps lanzadas desde el "
                            "menú de GNOME o dentro de un scope systemd "
                            "'app-dictado-*'. Se usará el atajo X11 de "
                            "respaldo (sólo aplicaciones X11).")

    @staticmethod
    def _unwrap(value):
        """dbus-fast puede devolver Variant o el valor ya desempaquetado."""
        return value.value if hasattr(value, "value") else value

    @staticmethod
    def _request_path(bus, token: str) -> str:
        """Ruta del objeto Request que el portal creará para ese token."""
        sender = bus.unique_name.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    async def _call_and_wait(self, bus, method, token: str):
        """Llama a un método y espera su señal Response.

        La ruta del Request es predecible a partir del handle_token, así que
        se registra la escucha *antes* de la llamada: el portal puede emitir
        Response y retirar el objeto antes de que lleguemos a mirarlo.
        """
        import asyncio
        from dbus_fast import MessageType

        path = self._request_path(bus, token)
        fut = asyncio.get_running_loop().create_future()

        def handler(msg) -> bool:
            if (msg.message_type == MessageType.SIGNAL
                    and msg.path == path
                    and msg.member == "Response"):
                if not fut.done():
                    response = msg.body[0]
                    raw = msg.body[1] or {}
                    results = {
                        k: (v.value if hasattr(v, "value") else v)
                        for k, v in raw.items()
                    }
                    fut.set_result((response, results))
                return True
            return False

        rule = ("type='signal',sender='org.freedesktop.portal.Desktop',"
                "interface='org.freedesktop.portal.Request',member='Response',"
                f"path='{path}'")
        bus.add_message_handler(handler)
        # El AddMatch va antes que la llamada en la misma conexión (orden FIFO).
        bus._add_match_rule(rule)
        try:
            request_path = await method
            if request_path != path:
                log.warning("[portal] Ruta de Request inesperada: %s (esperada %s)",
                            request_path, path)
            return await asyncio.wait_for(fut, timeout=25)
        finally:
            bus.remove_message_handler(handler)
            bus._remove_match_rule(rule)

    async def _amain(self) -> None:
        import asyncio
        from dbus_fast import BusType, Variant
        from dbus_fast.aio.message_bus import MessageBus

        loop = asyncio.get_running_loop()
        self._loop = loop

        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._bus = bus
        introspection = await bus.introspect(self._BUS_NAME, self._OBJ_PATH)
        obj = bus.get_proxy_object(self._BUS_NAME, self._OBJ_PATH, introspection)
        iface = obj.get_interface(self._IFACE)
        iface.on_activated(self._on_activated, unpack_variants=True)

        # El portal exige 'session_handle_token' (si no, xdg-desktop-portal
        # aborta con "session->token != NULL") y un 'handle_token' válido.
        token = uuid.uuid4().hex
        response, results = await self._call_and_wait(
            bus,
            iface.call_create_session({
                "handle_token":         Variant("s", token),
                "session_handle_token": Variant("s", token),
            }),
            token,
        )
        if response != 0:
            raise RuntimeError("el portal rechazó la sesión (%s): %s"
                               % (response, results))
        session = self._unwrap(results.get("session_handle", ""))
        if not session:
            raise RuntimeError("el portal no devolvió session_handle")
        self._session = session

        shortcuts = [
            (sid, {
                "description":       Variant("s", desc),
                "preferred_trigger": Variant("s", trigger),
                "token_type":        Variant("s", "default"),
            })
            for sid, desc, trigger in self._shortcuts
        ]
        token = uuid.uuid4().hex
        response, results = await self._call_and_wait(
            bus,
            iface.call_bind_shortcuts(
                session, shortcuts, self._parent_window,
                {"handle_token": Variant("s", token)}),
            token,
        )
        if response != 0:
            raise RuntimeError("el portal rechazó los atajos (%s): %s"
                               % (response, results))
        self._state = "ok"
        # El portal puede reasignar el atajo si está ocupado: dejar constancia.
        try:
            bound = results.get("shortcuts")
            bound = bound.value if hasattr(bound, "value") else bound
            for sid, props in bound or []:
                desc = props.get("trigger_description")
                desc = desc.value if hasattr(desc, "value") else desc
                log.info("[portal] Atajo %s → %s", sid, desc)
        except Exception:
            pass
        log.info("[portal] Atajos registrados vía GlobalShortcuts")

        while self._active:
            await asyncio.sleep(0.5)

    def _on_activated(self, session_handle, shortcut_id, timestamp, options) -> None:
        if not self._active:
            return
        if shortcut_id not in self._ids:
            return
        if self._session and session_handle and session_handle != self._session:
            return
        try:
            self._callback(shortcut_id)
        except Exception as exc:
            log.error("[portal] Error en el callback: %s", exc)


# ── Wayland: inyección de teclas con uinput (evdev) ─────────────────
# /dev/uinput lo escribe el usuario de la sesión (ACL de systemd-logind),
# así que no hace falta ydotool ni permisos de root.

_KEY_ALIASES = {
    "ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL",
    "shift": "KEY_LEFTSHIFT", "alt": "KEY_LEFTALT",
    "super": "KEY_LEFTMETA", "meta": "KEY_LEFTMETA", "win": "KEY_LEFTMETA",
    "space": "KEY_SPACE", "enter": "KEY_ENTER", "return": "KEY_ENTER",
    "tab": "KEY_TAB", "escape": "KEY_ESC", "esc": "KEY_ESC",
    "backspace": "KEY_BACKSPACE", "delete": "KEY_DELETE",
}

_MODIFIER_KEYS = ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_LEFTALT",
                  "KEY_LEFTMETA", "KEY_RIGHTCTRL", "KEY_RIGHTSHIFT",
                  "KEY_RIGHTALT", "KEY_RIGHTMETA")

# Keysyms de teclas muertas (X11) → se usan para escribir letras acentuadas.
_DEAD_KEY_KEYSYMS = {
    0x0300: 0xfe50,  # grave
    0x0301: 0xfe51,  # agudo
    0x0302: 0xfe52,  # circunflejo
    0x0303: 0xfe53,  # tilde
    0x0304: 0xfe54,  # macron
    0x0306: 0xfe55,  # breve
    0x0307: 0xfe56,  # punto arriba
    0x0308: 0xfe57,  # diéresis
    0x030A: 0xfe58,  # anillo
    0x030B: 0xfe59,  # doble agudo
    0x030C: 0xfe5a,  # carón
    0x0327: 0xfe5b,  # cedilla
    0x0328: 0xfe5c,  # ogonek
}

_KEYSYM_NAMES = {
    "Shift_L": 0xffe1, "Shift_R": 0xffe2, "Control_L": 0xffe3,
    "Control_R": 0xffe4, "Alt_L": 0xffe9, "Alt_R": 0xffea,
    "Super_L": 0xffeb, "Super_R": 0xffec, "Caps_Lock": 0xffe5,
    "ISO_Level3_Shift": 0xfe03,
    "dead_grave": 0xfe50, "dead_acute": 0xfe51, "dead_circumflex": 0xfe52,
    "dead_tilde": 0xfe53, "dead_macron": 0xfe54, "dead_breve": 0xfe55,
    "dead_abovedot": 0xfe56, "dead_diaeresis": 0xfe57, "dead_abovering": 0xfe58,
    "dead_doubleacute": 0xfe59, "dead_caron": 0xfe5a, "dead_cedilla": 0xfe5b,
    "dead_ogonek": 0xfe5c,
}

_uinput = None
_keymap_cache = None


def _uinput_device() -> "object":
    """Crea (una sola vez) un teclado virtual con todas las teclas."""
    global _uinput
    if _uinput is not None:
        return _uinput
    from evdev import UInput, ecodes
    caps = sorted({v for k, v in vars(ecodes).items()
                   if k.startswith("KEY_") and isinstance(v, int) and 0 < v < 256})
    _uinput = UInput({ecodes.EV_KEY: caps}, name="dictado-virtual")
    log.info("[uinput] Teclado virtual creado")
    return _uinput


def _combo_to_evdev(combo: str):
    """'<ctrl>+<shift>+v' → [KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_V]."""
    from evdev import ecodes
    keys = []
    for raw in combo.split("+"):
        part = raw.strip().strip("<>").strip().lower()
        if not part:
            continue
        name = _KEY_ALIASES.get(part)
        if name is None:
            name = "KEY_" + part.upper()
        key = getattr(ecodes, name, None)
        if key is None:
            log.warning("[uinput] Tecla desconocida en %r: %r", combo, part)
            return []
        keys.append(key)
    return keys


def _uinput_press(combo: str) -> bool:
    """Pulsa una combinación de teclas por uinput (sirve en Wayland)."""
    keys = _combo_to_evdev(combo)
    if not keys:
        log.warning("[uinput] Combinación no soportada: %r", combo)
        return False
    try:
        from evdev import ecodes
        ui = _uinput_device()
        for key in keys:
            ui.write(ecodes.EV_KEY, key, 1)
            ui.syn()
        time.sleep(0.03)
        for key in reversed(keys):
            ui.write(ecodes.EV_KEY, key, 0)
            ui.syn()
        return True
    except Exception as exc:
        log.warning("[uinput] Error inyectando %r: %s", combo, exc)
        return False


def _keysym_by_name(name: str) -> int:
    if name in _KEYSYM_NAMES:
        return _KEYSYM_NAMES[name]
    if name.startswith("U") and len(name) > 1:
        try:
            return int(name[1:], 16)
        except ValueError:
            pass
    try:
        from Xlib import XK
        return XK.string_to_keysym(name)
    except Exception:
        return 0


def _xkb_keymap() -> dict:
    """keysym → (keycode evdev, nivel 0-3) según el mapa de teclado activo."""
    global _keymap_cache
    if _keymap_cache is not None:
        return _keymap_cache
    kmap = {}
    try:
        out = subprocess.run(["xmodmap", "-pke"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            if not line.startswith("keycode "):
                continue
            left, right = line.split("=", 1)
            keycode = int(left.split()[1]) - 8      # X11 → evdev
            for level, name in enumerate(right.split()[:4]):
                ks = _keysym_by_name(name)
                if ks and ks not in kmap:
                    kmap[ks] = (keycode, level)
    except Exception as exc:
        log.warning("[uinput] No se pudo leer el mapa de teclado: %s", exc)
    _keymap_cache = kmap
    return kmap


def _text_to_events(text: str):
    """Texto → [(keycode, nivel)] o None si hay algún carácter imposible."""
    kmap = _xkb_keymap()
    if not kmap:
        return None
    events = []

    def add_char(ch: str) -> bool:
        ks = ord(ch) if ord(ch) < 0x100 else 0x01000000 + ord(ch)
        if ks in kmap:
            events.append(kmap[ks])
            return True
        # Letra acentuada: tecla muerta + letra base (á = ´ + a)
        nfd = unicodedata.normalize("NFD", ch)
        if len(nfd) == 2:
            dead = _DEAD_KEY_KEYSYMS.get(ord(nfd[1]))
            if dead in kmap:
                events.append(kmap[dead])
                return add_char(nfd[0])
        return False

    for ch in text:
        if ch == "\n":
            events.append((28, 0))          # KEY_ENTER
        elif ch == "\t":
            events.append((15, 0))          # KEY_TAB
        elif not add_char(ch):
            log.warning("[uinput] Carácter no escribible: %r", ch)
            return None
    return events


def _type_text(text: str) -> bool:
    """Escribe el texto tecla a tecla, como un teclado real (Wayland)."""
    events = _text_to_events(text)
    if not events:
        return False
    kmap = _xkb_keymap()
    shift = kmap.get(0xffe1, (42, 0))[0]
    altgr = kmap.get(0xfe03, (100, 0))[0]
    dead_codes = {kc for ks, (kc, _) in kmap.items() if 0xfe50 <= ks <= 0xfe5d}
    try:
        from evdev import ecodes
        ui = _uinput_device()
        for keycode, level in events:
            mods = ([altgr] if level & 2 else []) + ([shift] if level & 1 else [])
            for mod in mods:
                ui.write(ecodes.EV_KEY, mod, 1)
            ui.syn()
            ui.write(ecodes.EV_KEY, keycode, 1)
            ui.syn()
            ui.write(ecodes.EV_KEY, keycode, 0)
            ui.syn()
            for mod in reversed(mods):
                ui.write(ecodes.EV_KEY, mod, 0)
            ui.syn()
            # Las teclas muertas necesitan que el IM las procese antes de la
            # siguiente tecla; el resto puede ir rápido.
            time.sleep(0.14 if keycode in dead_codes else 0.02)
        return True
    except Exception as exc:
        log.warning("[uinput] Error escribiendo el texto: %s", exc)
        return False


# ── Windows: inyección de texto con SendInput ────────────────────────

def _win_send_unicode(text: str) -> bool:
    """Escribe el texto como si se tecleara, con SendInput Unicode.

    KEYEVENTF_UNICODE permite enviar cualquier carácter (tildes, ñ, ¿...)
    sin depender de la distribución de teclado.
    """
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        ULONG_PTR = (ctypes.c_ulonglong
                     if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong)

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class INPUT(ctypes.Structure):
            class _U(ctypes.Union):
                # El tamaño del union debe ser el de MOUSEINPUT para que
                # SendInput acepte cbSize.
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                            ("hi", HARDWAREINPUT)]

            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        INPUT_KEYBOARD = 1

        def emit(vk: int, scan: int, flags: int) -> None:
            inp = INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

        for ch in text:
            if ch == "\n":
                emit(0x0D, 0, 0)                 # VK_RETURN
                emit(0x0D, 0, KEYEVENTF_KEYUP)
            elif ch == "\t":
                emit(0x09, 0, 0)                 # VK_TAB
                emit(0x09, 0, KEYEVENTF_KEYUP)
            else:
                data = ch.encode("utf-16-le")
                for i in range(0, len(data), 2):
                    unit = int.from_bytes(data[i:i + 2], "little")
                    emit(0, unit, KEYEVENTF_UNICODE)
                    emit(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            time.sleep(0.002)
        return True
    except Exception as exc:
        log.warning("[windows] SendInput falló: %s", exc)
        return False


def _win_press_combo(combo: str) -> bool:
    """Pulsa una combinación (ctrl+v, ctrl+shift+v) con SendInput."""
    if not IS_WINDOWS:
        return False
    vk = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
          "v": 0x56, "c": 0x43, "a": 0x41, "insert": 0x2D}
    parts = [p.strip().strip("<>").lower() for p in combo.split("+") if p.strip()]
    keys = [vk[p] for p in parts if p in vk]
    if not keys:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        for k in keys:
            user32.keybd_event(k, 0, 0, 0)
        for k in reversed(keys):
            user32.keybd_event(k, 0, 2, 0)      # KEYEVENTF_KEYUP
        return True
    except Exception as exc:
        log.warning("[windows] keybd_event falló: %s", exc)
        return False


def _copy_to_clipboard(text: str) -> str:
    """Copia texto al portapapeles. Devuelve el método usado."""
    if IS_WAYLAND and shutil.which("wl-copy"):
        try:
            proc = subprocess.run(["wl-copy"], input=text.encode("utf-8"),
                                  timeout=10)
            if proc.returncode == 0:
                return "wl-copy"
        except Exception as exc:
            log.warning("[clipboard] wl-copy falló: %s", exc)
    try:
        pyperclip.copy(text)
        return "pyperclip"
    except Exception as exc:
        log.warning("[clipboard] pyperclip falló: %s", exc)
        return ""


# ── Configuración ──────────────────────────────────────────────────

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["server"] = {"host": DEFAULT_HOST, "port": DEFAULT_PORT}
    cfg["hotkey"] = {"combo": DEFAULT_HOTKEY, "copy_combo": DEFAULT_COPY_HOTKEY}
    cfg["send"] = {"mode": DEFAULT_SEND_MODE}
    cfg["paste"] = {"key": DEFAULT_PASTE_KEY,
                    "key_terminal": DEFAULT_PASTE_KEY_TERMINAL,
                    "key_wayland": DEFAULT_PASTE_KEY_WAYLAND}
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
        self._connecting  = False   # True mientras se hace health-check / handshake
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
        self._x11_watcher = None
        self._hotkey_backends = []
        self._last_hotkey_ts = 0.0
        self._drag_x = 0
        self._drag_y = 0
        self._tray_icon = None
        self._target_wid = ""   # ventana destino (guardada al activar el hotkey)

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
        r.geometry("310x220+80+80")           # ancho x alto + posición
        r.resizable(False, True)                  # solo altura ajustable
        r.minsize(310, 180)
        r.maxsize(310, 600)
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
            ("×", RED, lambda: self._hide_window()),
            ("─", FG,  lambda: self._hide_window()),
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
        # Se empaqueta al final: las barras inferiores reservan su espacio
        # primero y así los botones nunca quedan fuera de la ventana.

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
        # focus_force necesario con overrideredirect para que el widget sea editable
        self.txt.bind("<Button-1>", lambda e: self.root.after(1, self.txt.focus_force))

        self.txt.tag_configure("final",   foreground=FG)
        self.txt.tag_configure("partial", foreground=FG_DIM,
                               font=("Segoe UI", 10, "italic"))

        # Copiar/seleccionar como en cualquier editor (Tk no trae Ctrl+A y
        # su Ctrl+C puede bloquearse si otro cliente posee el portapapeles).
        copy_seqs = ["<Control-c>", "<Control-C>", "<Control-Insert>"]
        select_seqs = ["<Control-a>", "<Control-A>"]
        if IS_MACOS:
            copy_seqs += ["<Command-c>", "<Command-C>"]
            select_seqs += ["<Command-a>", "<Command-A>"]
        for seq in copy_seqs:
            self.txt.bind(seq, self._copy_selection)
        for seq in select_seqs:
            self.txt.bind(seq, self._select_all_text)
        self.txt.bind("<Button-3>", self._text_menu)

        # ── Botones centrados ────────────────────────────────────────
        bf = tk.Frame(r, bg=BG, pady=5)

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
        # Empaquetado de abajo arriba: estado, palabras clave, botones y,
        # por último, el área de texto (que se lleva el espacio sobrante).
        self.status_lbl.pack(side="bottom", pady=(0, 3))
        kw.pack(side="bottom", fill="x", padx=10)
        bf.pack(side="bottom", fill="x")
        tf.pack(fill="both", expand=True)

        r.protocol("WM_DELETE_WINDOW", self._on_close)
        # ESC en root Y en el widget de texto (por si el foco está dentro)
        def _esc(e=None):
            self._hide_window()
        r.bind("<Escape>", _esc)
        r.bind_all("<Escape>", _esc)

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

    def _selected_text(self) -> str:
        """Texto seleccionado en el área de texto ('' si no hay selección)."""
        try:
            return self.txt.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _copy_from_shortcut(self) -> None:
        """Copia (atajo global) aunque la ventana no tenga el foco."""
        if not self._selected_text() and not self._get_text_for_action():
            return
        self._copy_selection()
        self.root.after(1200, lambda: self._set_status("Listo", FG_DIM))

    def _copy_selection(self, event=None):
        """Copia la selección (o todo el texto) al portapapeles."""
        text = self._selected_text() or self._get_text_for_action()
        if not text:
            return "break"
        method = _copy_to_clipboard(text)
        if method:
            self._set_status("Copiado ✓", GREEN)
            self.root.after(1500, lambda: self._set_status("Listo", FG_DIM))
        else:
            self._set_status("No se pudo copiar", RED)
        return "break"

    def _select_all_text(self, event=None):
        """Ctrl+A: selecciona todo el texto transcrito."""
        self.txt.tag_add("sel", "1.0", "end-1c")
        self.txt.mark_set("insert", "1.0")
        self.txt.see("1.0")
        return "break"

    def _text_menu(self, event):
        """Menú contextual del área de texto."""
        menu = tk.Menu(self.root, tearoff=0, bg=BG2, fg=FG,
                       activebackground=BG3, activeforeground=FG,
                       bd=0, font=("Segoe UI", 9))
        menu.add_command(label="Copiar",            command=self._copy_selection)
        menu.add_command(label="Seleccionar todo",  command=self._select_all_text)
        menu.add_separator()
        menu.add_command(label="Limpiar",           command=self._action_clear)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

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

    def _auto_resize(self) -> None:
        """Ajusta la ventana al contenido del texto (5–18 líneas).

        Se cambia la altura de la ventana (no sólo la del widget) para que
        el área de texto tenga sitio y los botones sigan anclados abajo.
        """
        try:
            lines = int(self.txt.count("1.0", "end", "displaylines")[0] or 1)
            lines = max(5, min(18, lines + 1))
            if int(self.txt.cget("height")) != lines:
                self.txt.config(height=lines)
            self.root.update_idletasks()
            w = self.root.winfo_width() or 310
            h = min(self.root.winfo_reqheight(),
                    self.root.winfo_screenheight() - 120)
            if abs(self.root.winfo_height() - h) > 1:
                self.root.geometry(f"{w}x{h}")
        except Exception:
            pass

    def _append_final(self, text: str) -> None:
        """Añade texto final al widget, siempre al final del contenido editable."""
        self._clear_partial_in_widget()
        if text:
            current = self.txt.get("1.0", "end-1c")
            sep = " " if current.strip() else ""
            self.txt.insert("end", sep + text, "final")
            self.txt.see("end")
        self._auto_resize()

    def _refresh_partial(self) -> None:
        """Muestra/actualiza el texto parcial (itálica) al final del widget."""
        self._clear_partial_in_widget()
        if self.partial:
            current = self.txt.get("1.0", "end-1c")
            sep = " " if current.strip() else ""
            self.txt.insert("end", sep + self.partial + "…", "partial")
            self.txt.see("end")
        self._auto_resize()

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
        self._connecting = True
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
            if not self._connecting:
                log.info("Conexión cancelada antes de abrir el WebSocket")
                return
            self._connect_ws()
        threading.Thread(target=_probe_then_connect, daemon=True, name="probe").start()

    def _stop_recording(self) -> None:
        self.recording    = False
        self._connecting  = False
        self.partial      = ""
        if self.ws_conn:
            try:
                self.ws_conn.send(json.dumps({"type": "stop"}))
            except Exception:
                pass
        self._close_ws()
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

    def _close_ws(self) -> None:
        """Cierra el WebSocket y descarta la conexión. Idempotente."""
        self._connecting = False
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
            self.ws_app = None
        self.ws_conn = None

    def _hide_window(self) -> None:
        """Oculta la ventana cerrando antes cualquier grabación/conexión activa."""
        if self.recording or self._connecting:
            self._stop_recording()
        else:
            self._close_ws()
        self.root.withdraw()

    # ── WebSocket ───────────────────────────────────────────────────

    def _connect_ws(self) -> None:
        url = self.ws_url
        log.info("WebSocket connect → %s", url)

        def on_open(ws) -> None:
            log.info("WebSocket abierto")
            if not self._connecting:
                log.info("Conexión cancelada antes del handshake — cerrando")
                try:
                    ws.close()
                except Exception:
                    pass
                return
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
            if not self._connecting:
                log.info("'ready' ignorado: la conexión fue cancelada")
                return
            self.recording    = True
            self._connecting  = False
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
            self._auto_resize()
            self._set_status("Enviando…", GREEN)
            self._type_at_cursor(clean)   # retira la ventana al escribir
            return

        if copy_idx != -1:
            clean = text[:copy_idx].rstrip()
            self._stop_recording()
            _copy_to_clipboard(clean)
            self.transcript = clean
            self.txt.delete("1.0", "end")
            if clean:
                self.txt.insert("end", clean, "final")
            self._auto_resize()
            self._set_status("Copiado ✓", GREEN)
            self.root.after(400, self._hide_window)

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
        # Si el usuario seleccionó un fragmento con el ratón, se copia eso.
        text = self._selected_text() or self._get_text_for_action()
        if not text:
            return
        if self.recording:
            self._stop_recording()
        if _copy_to_clipboard(text):
            self._set_status("Copiado ✓", GREEN)
            self.root.after(600, self._hide_window)
        else:
            self._set_status("No se pudo copiar", RED)

    def _action_clear(self) -> None:
        self.transcript = ""
        self.partial    = ""
        self.txt.delete("1.0", "end")
        self._auto_resize()
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

    # Clases WM de terminales conocidas (usan Ctrl+Shift+V para pegar)
    _TERMINAL_CLASSES = {
        "gnome-terminal-server", "gnome-terminal", "xterm", "urxvt",
        "konsole", "terminator", "tilix", "alacritty", "kitty",
        "xfce4-terminal", "mate-terminal", "lxterminal", "terminology",
        "rxvt", "sakura", "yakuake", "guake", "qterminal", "st-256color",
        "st", "foot", "wezterm",
    }

    def _is_terminal_window(self, wid: str) -> bool:
        """Detecta si la ventana destino es una terminal usando WM_CLASS completo."""
        try:
            # xprop devuelve: WM_CLASS(STRING) = "instancia", "Clase"
            rc = subprocess.run(
                ["xprop", "-id", wid, "WM_CLASS"],
                capture_output=True, text=True, timeout=2,
            )
            raw = rc.stdout.lower()
            log.debug("_is_terminal: wid=%s WM_CLASS=%r", wid, rc.stdout.strip())
            return any(t in raw for t in self._TERMINAL_CLASSES)
        except Exception:
            # Fallback: getwindowclassname
            try:
                rc = subprocess.run(
                    ["xdotool", "getwindowclassname", wid],
                    capture_output=True, text=True, timeout=2,
                )
                return any(t in rc.stdout.strip().lower() for t in self._TERMINAL_CLASSES)
            except Exception:
                return False

    def _type_at_cursor(self, text: str) -> None:
        """Minimiza la ventana y envía el texto a la ventana destino guardada.

        Estrategia:
          - X11 → Terminales: xdotool type (XTest, emulación de teclado real).
            XTest es aceptado por todas las terminales y maneja Unicode
            mapeando cada carácter a su keysym U+XXXX.
            X11 → Apps normales: portapapeles + Ctrl+V (instantáneo).
          - Wayland → portapapeles + tecla de pegado inyectada por uinput
            (evdev). Es la única vía que alcanza a las aplicaciones nativas
            de Wayland; nunca se llama a 'xdotool windowactivate' porque
            robaría el foco de la aplicación nativa.
        """
        if not text:
            return

        target_wid = self._target_wid

        def _do() -> None:
            self._ui_queue.put(("hide", None))
            time.sleep(0.25)  # esperar a que la ventana desaparezca

            is_terminal = self._is_terminal_window(target_wid) if target_wid else False
            log.debug("_type_at_cursor: wid=%s is_terminal=%s", target_wid, is_terminal)

            if IS_WAYLAND:
                self._send_wayland(text)
                return

            if IS_WINDOWS:
                self._send_windows(text)
                return

            if IS_MACOS:
                self._send_macos(text)
                return

            # Activar la ventana destino si la conocemos
            if target_wid:
                try:
                    subprocess.run(
                        ["xdotool", "windowactivate", "--sync", target_wid],
                        check=False, timeout=3,
                    )
                    time.sleep(0.12)
                except Exception:
                    pass

            try:
                if is_terminal:
                    # XTest keyboard simulation: funciona en terminales, soporta
                    # tildes y Ñ porque xdotool mapea Unicode → keysym U+XXXX.
                    subprocess.run(
                        ["xdotool", "type", "--clearmodifiers",
                         "--delay", "20", "--", text],
                        check=True, timeout=60,
                    )
                else:
                    # Apps normales: portapapeles + Ctrl+V (más rápido)
                    pyperclip.copy(text)
                    subprocess.run(
                        ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
                        check=True, timeout=3,
                    )
            except FileNotFoundError:
                # xdotool no instalado → fallback pynput
                try:
                    pyperclip.copy(text)
                    kb = _pynput()
                    ctrl = kb.Key.ctrl
                    shift = kb.Key.shift
                    with kb.pressed(ctrl):
                        if is_terminal:
                            with kb.pressed(shift):
                                kb.tap('v')
                        else:
                            kb.tap('v')
                except Exception:
                    pass
            except Exception as exc:
                log.debug("_type_at_cursor error: %s", exc)

        threading.Thread(target=_do, daemon=True).start()

    def _send_windows(self, text: str) -> None:
        """Envía el texto en Windows: lo teclea o lo pega (según ⚙)."""
        mode = self.config.get("send", "mode", fallback="escribir")
        if mode in ("escribir", "auto"):
            if _win_send_unicode(text):
                log.info("[windows] Texto escrito con SendInput (%d caracteres)",
                         len(text))
                return
            log.warning("[windows] SendInput falló; se intenta pegar")

        key = self.config.get("paste", "key", fallback=DEFAULT_PASTE_KEY)
        method = _copy_to_clipboard(text)
        if not method:
            log.error("No se pudo copiar al portapapeles; texto: %r", text)
            return
        time.sleep(0.15)
        log.debug("_send_windows: pegando con %s (método=%s)", key, method)
        if not _win_press_combo(key):
            log.error("No se pudo pegar el texto")

    def _send_macos(self, text: str) -> None:
        """Envía el texto en macOS: Cmd+V (o escritura con pynput)."""
        mode = self.config.get("send", "mode", fallback=DEFAULT_SEND_MODE)
        if mode in ("escribir", "auto"):
            try:
                _pynput().Controller().type(text)
                log.info("[macos] Texto escrito con pynput (%d caracteres)",
                         len(text))
                return
            except Exception as exc:
                log.warning("[macos] No se pudo escribir (%s); se pega", exc)

        method = _copy_to_clipboard(text)
        if not method:
            log.error("No se pudo copiar al portapapeles; texto: %r", text)
            return
        time.sleep(0.15)
        try:
            kb = _pynput()
            ctrl = kb.Controller()
            with ctrl.pressed(kb.Key.cmd):
                ctrl.press("v")
                ctrl.release("v")
            log.debug("[macos] Pegado con Cmd+V (método=%s)", method)
        except Exception as exc:
            log.error("[macos] No se pudo pegar (¿permiso de Accesibilidad?): %s",
                      exc)

    def _send_wayland(self, text: str) -> None:
        """Envía el texto a la ventana enfocada en Wayland.

        Modo "escribir" (por defecto): teclea el texto con el teclado virtual
        de uinput, igual que si lo escribiera una persona. Funciona en
        terminales y en cualquier app, sin depender del portapapeles.
        Modo "pegar": copia al portapapeles y pulsa Shift+Insert.
        """
        mode = self.config.get("send", "mode", fallback="escribir")
        if mode in ("escribir", "auto"):
            if _type_text(text):
                log.info("[wayland] Texto escrito con el teclado virtual "
                         "(%d caracteres)", len(text))
                return
            log.warning("[wayland] No se pudo escribir todo el texto; "
                        "se intenta pegar desde el portapapeles")

        key = self.config.get("paste", "key_wayland",
                              fallback=DEFAULT_PASTE_KEY_WAYLAND)
        method = _copy_to_clipboard(text)
        if not method:
            log.error("No se pudo copiar al portapapeles; texto: %r", text)
            return
        log.debug("_send_wayland: pegando con %s (método=%s)", key, method)

        if _uinput_press(key):
            return

        # Sin uinput sólo podemos alcanzar a clientes X11
        log.warning("[wayland] uinput no disponible; se intenta xdotool "
                    "(sólo funciona en aplicaciones X11)")
        try:
            subprocess.run(["xdotool", "key", "--clearmodifiers", key],
                           check=True, timeout=3)
        except Exception as exc:
            log.error("No se pudo enviar el texto: %s", exc)

    # ── Tecla de acceso rápido global ──────────────────────────────

    def _start_hotkey_listener(self) -> None:
        combo = self.config.get("hotkey", "combo", fallback=DEFAULT_HOTKEY)
        self._hotkey_backends = []

        def activate() -> None:
            # En Wayland el portal y XGrabKey pueden disparar a la vez
            # (XGrabKey sólo ve clientes X11): un pequeño debounce evita
            # que el atajo se cuente como dos.
            now = time.monotonic()
            if now - self._last_hotkey_ts < 0.4:
                return
            self._last_hotkey_ts = now

            # Guardar la ventana activa ANTES de que nuestra ventana tome el foco.
            # En este momento el usuario está en la terminal u otra app.
            # En Wayland 'xdotool getactivewindow' no es fiable (devuelve la
            # ventana fantasma de XWayland), así que sólo se usa en X11.
            if HAVE_X11 and not IS_WAYLAND:
                try:
                    res = subprocess.run(
                        ["xdotool", "getactivewindow"],
                        capture_output=True, text=True, timeout=2,
                    )
                    wid = res.stdout.strip()
                    if wid.isdigit():
                        self._target_wid = wid
                except Exception:
                    pass
            # Nunca tocar Tk desde este hilo: la cola la consume el bucle UI.
            self._ui_queue.put(("toggle", None))

        # Windows/macOS: pynput registra el atajo global con la API nativa.
        if IS_WINDOWS or IS_MACOS:
            try:
                listener = _pynput().GlobalHotKeys({combo: activate})
                listener.daemon = True
                listener.start()
                self._hotkey_listener = listener
                self._hotkey_backends.append("win32-hotkey")
                log.info("[hotkey] %s: atajo global %r registrado",
                         "Windows" if IS_WINDOWS else "macOS", combo)
            except Exception as exc:
                log.warning("[hotkey] No se pudo registrar el atajo: %s", exc)
            return

        # En Wayland el atajo global se registra con el portal; XGrabKey se
        # deja activo como respaldo para las aplicaciones X11 bajo XWayland.
        if IS_WAYLAND:
            copy_combo = self.config.get("hotkey", "copy_combo",
                                         fallback=DEFAULT_COPY_HOTKEY)

            def on_portal(shortcut_id: str) -> None:
                if shortcut_id == "dictado-copy":
                    self._ui_queue.put(("copy", None))
                else:
                    activate()

            portal = _PortalShortcutWatcher(
                [
                    ("dictado-toggle", "Dictado por voz (grabar / parar)",
                     _combo_to_xdg_trigger(combo)),
                    ("dictado-copy", "Dictado: copiar la transcripción",
                     _combo_to_xdg_trigger(copy_combo)),
                ],
                on_portal, parent_window=self._x11_window_id())
            portal.start()
            self._hotkey_listener = portal
            self._hotkey_backends.append("wayland-portal")
            log.info("[hotkey] Sesión Wayland: atajo vía portal "
                     "GlobalShortcuts (+ respaldo X11)")
        else:
            watcher = _XGrabKeyWatcher(combo, activate)
            watcher.start()
            self._hotkey_listener = watcher
            self._hotkey_backends.append("x11-xgrabkey")
            log.info("[hotkey] Sesión X11: atajo vía XGrabKey")

        if IS_WAYLAND:
            # Respaldo para clientes X11 mientras el portal no esté disponible
            watcher = _XGrabKeyWatcher(combo, activate)
            watcher.start()
            self._hotkey_backends.append("x11-xgrabkey")
            self._x11_watcher = watcher

    def _x11_window_id(self) -> str:
        """ID de la ventana X11 del widget (para el portal), o ''.

        Tk crea una ventana interior y otra exterior (la que gestiona el
        WM); el portal necesita la exterior, y con WM_CLASS/_NET_WM_PID
        fijados para poder asociarla a la app.
        """
        try:
            # La ventana X sólo existe en el servidor tras procesar la cola.
            self.root.update_idletasks()
            self.root.update()
            wid = self.root.winfo_id()
            root = _Xdisplay.Display()
            try:
                win = root.create_resource_object("window", wid)
                parent = None
                for _ in range(5):
                    try:
                        parent = win.query_tree().parent
                        break
                    except Exception:
                        time.sleep(0.05)
                        win = root.create_resource_object("window", wid)
                toplevel = parent.id if parent else wid
                obj = root.create_resource_object("window", toplevel)
                obj.change_property(
                    root.intern_atom("WM_CLASS"), root.intern_atom("STRING"),
                    8, b"dictado\x00dictado\x00")
                obj.change_property(
                    root.intern_atom("_NET_WM_PID"),
                    root.intern_atom("CARDINAL"), 32, [os.getpid()])
                root.flush()
                return hex(toplevel)
            finally:
                root.close()
        except Exception as exc:
            log.debug("[portal] No se pudo preparar la ventana X11: %s", exc)
            try:
                return hex(self.root.winfo_id())
            except Exception:
                return ""

    def _restart_hotkey(self) -> None:
        for watcher in (self._hotkey_listener, self._x11_watcher):
            if watcher:
                try:
                    watcher.stop()
                except Exception:
                    pass
        self._hotkey_listener = None
        self._x11_watcher = None
        self._start_hotkey_listener()

    # ── Diálogo de configuración ────────────────────────────────────

    def _open_settings(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Configuración")
        dlg.configure(bg=BG)
        dlg.geometry("320x590")
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)
        # Centrar en pantalla (evitar problemas con overrideredirect en parent)
        dlg.update_idletasks()
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        x = (sw - 320) // 2
        y = max(0, (sh - 590) // 2)
        dlg.geometry(f"320x590+{x}+{y}")
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
        if IS_WAYLAND:
            kc_var = lbl_entry(
                dlg, "Copiar texto:",
                self.config.get("hotkey", "copy_combo",
                                fallback=DEFAULT_COPY_HOTKEY))
        else:
            kc_var = tk.StringVar(
                value=self.config.get("hotkey", "copy_combo",
                                      fallback=DEFAULT_COPY_HOTKEY))

        tk.Label(
            dlg,
            text="Ej:  <ctrl>+<space>   <ctrl>+<shift>+d   <alt>+v",
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
        ).pack()

        tk.Label(
            dlg, text="Envío del texto", fg=FG, bg=BG,
            font=("Segoe UI", 9, "bold"),
        ).pack(pady=(10, 2))

        if IS_WAYLAND:
            modo_var = tk.StringVar(
                value=self.config.get("send", "mode",
                                      fallback=DEFAULT_SEND_MODE))
            mf = tk.Frame(dlg, bg=BG)
            mf.pack(fill="x", padx=14, pady=3)
            tk.Label(mf, text="Modo:", fg=FG_DIM, bg=BG,
                     font=("Segoe UI", 9), width=16, anchor="w").pack(side="left")
            for texto, valor in (("Escribir (teclado)", "escribir"),
                                 ("Pegar (portapapeles)", "pegar")):
                tk.Radiobutton(
                    mf, text=texto, value=valor, variable=modo_var,
                    bg=BG, fg=FG, selectcolor=BG3, activebackground=BG,
                    activeforeground=FG, highlightthickness=0, bd=0,
                    font=("Segoe UI", 8), anchor="w",
                ).pack(side="left", padx=2)
        else:
            modo_var = tk.StringVar(
                value=self.config.get("send", "mode",
                                      fallback=DEFAULT_SEND_MODE))

        if IS_WAYLAND:
            pk_var = lbl_entry(
                dlg, "Tecla de pegado:",
                self.config.get("paste", "key_wayland",
                                fallback=DEFAULT_PASTE_KEY_WAYLAND))
            pkt_var = tk.StringVar(
                value=self.config.get("paste", "key_terminal",
                                      fallback=DEFAULT_PASTE_KEY_TERMINAL))
        else:
            pk_var = lbl_entry(
                dlg, "Tecla (apps):",
                self.config.get("paste", "key", fallback=DEFAULT_PASTE_KEY))
            # En X11 las terminales se escriben con 'xdotool type', así que
            # no hace falta una tecla de pegado aparte.
            pkt_var = tk.StringVar(
                value=self.config.get("paste", "key_terminal",
                                      fallback=DEFAULT_PASTE_KEY_TERMINAL))

        _sesion = ("Windows" if IS_WINDOWS else
                   "macOS" if IS_MACOS else
                   "Wayland" if IS_WAYLAND else "X11")
        if IS_WINDOWS:
            _inyeccion = "SendInput (Unicode)"
        elif IS_MACOS:
            _inyeccion = "pynput (Cmd+V)"
        elif IS_WAYLAND:
            _inyeccion = "uinput (evdev)"
        else:
            _inyeccion = "xdotool (XTest)"
        tk.Label(
            dlg,
            text=f"Sesión: {_sesion}  ·  inyección: {_inyeccion}",
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
        ).pack()
        if IS_WAYLAND:
            tk.Label(
                dlg,
                text="Shift+Insert pega en terminales y en casi todas las\n"
                     "apps (ctrl+v sólo en apps, ctrl+shift+v en terminales).\n"
                     "La ventana sin bordes no recibe el teclado en Wayland:\n"
                     "para copiar usa el botón, el clic derecho o el atajo\n"
                     "«Copiar texto».",
                fg=GRAY, bg=BG, font=("Segoe UI", 7), justify="left",
            ).pack()

        def save() -> None:
            self.config["server"]["host"] = h_var.get().strip()
            self.config["server"]["port"] = p_var.get().strip()
            if "hotkey" not in self.config:
                self.config.add_section("hotkey")
            self.config["hotkey"]["combo"] = k_var.get().strip()
            self.config["hotkey"]["copy_combo"] = (
                kc_var.get().strip() or DEFAULT_COPY_HOTKEY)
            if "paste" not in self.config:
                self.config.add_section("paste")
            if IS_WAYLAND:
                self.config["paste"]["key_wayland"] = (
                    pk_var.get().strip() or DEFAULT_PASTE_KEY_WAYLAND)
            else:
                self.config["paste"]["key"] = (
                    pk_var.get().strip() or DEFAULT_PASTE_KEY)
            self.config["paste"]["key_terminal"] = (
                pkt_var.get().strip() or DEFAULT_PASTE_KEY_TERMINAL)
            if "send" not in self.config:
                self.config.add_section("send")
            self.config["send"]["mode"] = modo_var.get().strip() or DEFAULT_SEND_MODE
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
            "  dbus-fast ≥5.0 — MIT\n"
            "  python-evdev ≥2.0 — BSD 3-Clause\n"
            "  xdotool — MIT\n"
            "  wl-clipboard — MIT (Wayland)"
        )
        tk.Label(
            dlg, text=credits,
            fg=GRAY, bg=BG, font=("Segoe UI", 7),
            justify="left", anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 12))

        # Ajustar el tamaño al contenido real (el texto de créditos crece y
        # una altura fija recortaba la parte inferior).
        dlg.update_idletasks()
        w = max(320, dlg.winfo_reqwidth())
        h = min(dlg.winfo_reqheight(), sh - 80)
        dlg.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2)}")

    # ── Bucle de eventos UI (cola thread-safe) ──────────────────────

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                evt, data = self._ui_queue.get_nowait()
                if evt == "msg":
                    self._handle_msg(data)
                elif evt == "error":
                    self._set_status(f"Error: {data}", RED)
                    self._close_ws()
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
                    self._close_ws()
                elif evt == "test_ok":
                    self._set_status(f"✓ {data}", GREEN)
                elif evt == "test_fail":
                    self._set_status(f"✗ {data}", RED)
                elif evt == "toggle":
                    self.toggle_recording()
                elif evt == "copy":
                    self._copy_from_shortcut()
                elif evt == "show":
                    self._show_window()
                elif evt == "hide":
                    self._hide_window()
                elif evt == "quit":
                    self._quit_app()
        except queue.Empty:
            pass
        self.root.after(50, self._poll_ui_queue)

    # ── Cierre limpio ───────────────────────────────────────────────

    def _on_close(self) -> None:
        """Oculta la ventana cerrando antes la conexión; la app sigue en segundo plano."""
        self._hide_window()

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
        try:
            self._start_tray_impl()
        except Exception as exc:
            log.warning("Bandeja del sistema no disponible: %s", exc)

    def _start_tray_impl(self) -> None:
        def on_show(icon, item):
            self._ui_queue.put(("show", None))

        def on_quit(icon, item):
            icon.stop()
            self._ui_queue.put(("quit", None))

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
        # Guardar ventana destino si aún no fue capturada por el hotkey
        # (p.ej. al mostrar desde la bandeja del sistema).
        if not self._target_wid and HAVE_X11 and not IS_WAYLAND:
            try:
                res = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    capture_output=True, text=True, timeout=2,
                )
                wid = res.stdout.strip()
                if wid.isdigit():
                    self._target_wid = wid
            except Exception:
                pass
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        # Forzar foco dos veces: a los 80ms (primer mapeado) y a los 200ms (seguro)
        self.root.after(80,  self.root.focus_force)
        self.root.after(200, self.root.focus_force)

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
        if self._x11_watcher:
            try:
                self._x11_watcher.stop()
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
