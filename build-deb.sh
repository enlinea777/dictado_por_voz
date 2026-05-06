#!/usr/bin/env bash
# =============================================================================
# build-deb.sh — Construye dictado_<version>_all.deb
#
# Uso:  cd vosk-dictado && ./build-deb.sh
#
# Requiere: dpkg-deb, python3, y que el venv local tenga Pillow instalado.
# =============================================================================

set -euo pipefail
cd "$(dirname "$0")"

NAME="dictado"
VERSION="1.0.0"
ARCH="all"
PKG_DIR="deb-build/${NAME}_${VERSION}_${ARCH}"

echo "=== Construyendo ${NAME}_${VERSION}_${ARCH}.deb ==="

# ── Limpiar build anterior ────────────────────────────────────────────────────
rm -rf deb-build
mkdir -p \
    "${PKG_DIR}/DEBIAN" \
    "${PKG_DIR}/usr/bin" \
    "${PKG_DIR}/usr/lib/dictado" \
    "${PKG_DIR}/usr/share/applications" \
    "${PKG_DIR}/usr/share/icons/hicolor/128x128/apps" \
    "${PKG_DIR}/var/lib/dictado"

# ── Script principal ──────────────────────────────────────────────────────────
cp dictado.py "${PKG_DIR}/usr/lib/dictado/"
chmod 644 "${PKG_DIR}/usr/lib/dictado/dictado.py"

# ── Lanzador (sin consola) ────────────────────────────────────────────────────
cat > "${PKG_DIR}/usr/bin/dictado" << 'LAUNCHER'
#!/usr/bin/env bash
VENV=/var/lib/dictado/venv
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "ERROR: venv no encontrado en $VENV. Reinstala el paquete dictado." >&2
    exit 1
fi
exec "$VENV/bin/python3" -W ignore /usr/lib/dictado/dictado.py "$@" \
    >/dev/null 2>&1
LAUNCHER
chmod 755 "${PKG_DIR}/usr/bin/dictado"

# ── .desktop (integración con menú de escritorio) ─────────────────────────────
cat > "${PKG_DIR}/usr/share/applications/dictado.desktop" << 'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=Dictado por Voz
GenericName=Widget de dictado
Comment=Transcripción de voz a texto en tiempo real (Vosk)
Exec=dictado
Icon=dictado
Terminal=false
Categories=Utility;Accessibility;
Keywords=voz;dictado;transcripción;vosk;
StartupNotify=false
DESKTOP
chmod 644 "${PKG_DIR}/usr/share/applications/dictado.desktop"

# ── Icono PNG (generado con Pillow) ──────────────────────────────────────────
ICON_OUT="${PKG_DIR}/usr/share/icons/hicolor/128x128/apps/dictado.png"
PYTHON="${PWD}/venv/bin/python3"
if [[ ! -x "$PYTHON" ]]; then
    PYTHON="python3"
fi

echo "Generando icono..."
"$PYTHON" - "$ICON_OUT" << 'PYEOF'
import sys
from PIL import Image, ImageDraw

s = 128
img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# Fondo oscuro redondo
d.ellipse([0, 0, s-1, s-1], fill=(26, 31, 46, 255))
d.ellipse([6, 6, s-7, s-7], fill=(35, 42, 62, 255))

# Cuerpo del micrófono (redondeado, verde)
mw, mh, my = 30, 44, 16
mx = (s - mw) // 2
d.rounded_rectangle([mx, my, mx+mw, my+mh], radius=15, fill=(34, 197, 94, 255))

# Soporte en arco
cx = s // 2
arc_box = [cx-24, my+16, cx+24, my+68]
d.arc(arc_box, start=0, end=180, fill=(34, 197, 94, 255), width=5)

# Palo vertical
pole_top = my + mh + 1
d.rectangle([cx-3, pole_top, cx+3, pole_top+14], fill=(34, 197, 94, 255))

# Base horizontal
base_y = pole_top + 14
d.rectangle([cx-16, base_y, cx+16, base_y+5], fill=(34, 197, 94, 255))

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dictado.png"
img.save(out)
print(f"  Icono guardado: {out}")
PYEOF

# ── DEBIAN/control ────────────────────────────────────────────────────────────
cat > "${PKG_DIR}/DEBIAN/control" << CONTROL
Package: ${NAME}
Version: ${VERSION}
Architecture: ${ARCH}
Maintainer: Usuario <usuario@localhost>
Section: utils
Priority: optional
Depends: python3 (>= 3.9), python3-venv, python3-tk, libportaudio2, xclip | xsel
Description: Widget flotante de dictado por voz
 Transcribe voz a texto en tiempo real conectándose a un servidor Vosk
 mediante WebSocket. Incluye icono en bandeja del sistema, atajo de
 teclado global (Ctrl+Space) y gestión de texto acumulado.
CONTROL

# ── DEBIAN/postinst ───────────────────────────────────────────────────────────
cat > "${PKG_DIR}/DEBIAN/postinst" << 'POSTINST'
#!/bin/bash
set -e

VENV="/var/lib/dictado/venv"

echo "Instalando entorno Python para Dictado por Voz..."
echo "(Requiere conexión a internet para descargar paquetes)"

python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    "sounddevice>=0.4.6" \
    "websocket-client>=1.6.0" \
    "pynput>=1.7.6" \
    "pyperclip>=1.8.2" \
    "numpy>=1.24.0" \
    "pystray>=0.19" \
    "Pillow>=9.0" \
    "python-xlib>=0.33"

# Actualizar caché de iconos si está disponible
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1 || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications >/dev/null 2>&1 || true
fi

echo "Dictado instalado correctamente."
echo "Lanza con: dictado  (o desde el menú de aplicaciones)"
exit 0
POSTINST
chmod 755 "${PKG_DIR}/DEBIAN/postinst"

# ── DEBIAN/prerm ──────────────────────────────────────────────────────────────
cat > "${PKG_DIR}/DEBIAN/prerm" << 'PRERM'
#!/bin/bash
set -e
# Eliminar el entorno virtual antes de desinstalar
rm -rf /var/lib/dictado/venv
exit 0
PRERM
chmod 755 "${PKG_DIR}/DEBIAN/prerm"

# ── Construir el .deb ─────────────────────────────────────────────────────────
DEB_OUT="deb-build/${NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --build "${PKG_DIR}" "${DEB_OUT}"

echo ""
echo "✓ Paquete listo: ${DEB_OUT}"
echo "  Tamaño: $(du -sh "${DEB_OUT}" | cut -f1)"
echo ""
echo "Para instalar:"
echo "  sudo dpkg -i ${DEB_OUT}"
echo "  sudo apt-get install -f   # si faltan dependencias del sistema"
