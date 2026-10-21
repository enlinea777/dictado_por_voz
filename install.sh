#!/usr/bin/env bash
# install.sh — Instala dependencias para vosk-dictado
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

IS_WAYLAND=0
[[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]] && IS_WAYLAND=1

echo "==> Dependencias del sistema (apt)…"
sudo apt-get update -q
PKGS=(
    python3-tk
    python3-pip
    python3-venv
    python3-full
    portaudio19-dev
    libportaudio2
    xdotool
    xclip
)
if (( IS_WAYLAND )); then
    # wl-copy: portapapeles nativo de Wayland (necesario para pegar el texto)
    PKGS+=(wl-clipboard)
fi
sudo apt-get install -y "${PKGS[@]}"

echo "==> Entorno virtual en $VENV_DIR…"
# Un upgrade de Python deja el venv inservible (los paquetes quedan en
# lib/python3.X y el intérprete nuevo no los ve): se detecta y se recrea.
if [[ -e "$VENV_DIR" ]] && ! "$VENV_DIR/bin/python3" -c "import sys" >/dev/null 2>&1; then
    echo "    venv inservible (¿cambió la versión de Python?): se recrea"
    rm -rf "$VENV_DIR"
fi
if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    # Sin --system-site-packages: evita mezclar paquetes de la distro
    python3 -m venv "$VENV_DIR"
fi

echo "==> Dependencias Python…"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "==> Lanzador run.sh…"
cat > "$SCRIPT_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
# run.sh — lanza el widget de dictado con el venv local
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv"
PY="$VENV/bin/python3"

if [[ ! -x "$PY" ]] || ! "$PY" -c "import sys" >/dev/null 2>&1; then
    echo "ERROR: el entorno virtual de $SCRIPT_DIR no sirve" >&2
    echo "       (¿se actualizó la versión de Python?). Ejecuta ./install.sh" >&2
    exit 1
fi

IS_WAYLAND=0
[[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]] && IS_WAYLAND=1

# En Wayland el portal GlobalShortcuts exige un 'app id' que systemd deduce
# del scope 'app-<id>-<aleatorio>.scope'. Si no venimos de uno, relanzamos.
current_unit="$(head -1 /proc/self/cgroup 2>/dev/null)"
current_unit="${current_unit##*:}"
current_unit="${current_unit##*/}"

if (( IS_WAYLAND )) && [[ -z "${DICTADO_IN_SCOPE:-}" ]] \
   && [[ "$current_unit" != app-* ]] && command -v systemd-run >/dev/null 2>&1; then
    export DICTADO_IN_SCOPE=1
    exec systemd-run --user --scope --quiet \
        --unit="app-dictado-$(printf '%06x' $((RANDOM * RANDOM))).scope" \
        "$SCRIPT_DIR/run.sh" "$@"
fi

exec "$PY" "$SCRIPT_DIR/dictado.py" "$@"
EOF
chmod +x "$SCRIPT_DIR/run.sh"

# Entrada de menú para desarrollo: el portal deduce el app id del nombre
# del .desktop, así que también hace falta cuando no se instaló el .deb.
if (( IS_WAYLAND )) && [[ ! -f /usr/share/applications/dictado.desktop ]]; then
    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    cat > "$DESKTOP_DIR/dictado.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Dictado por Voz (dev)
Comment=Transcripción de voz a texto en tiempo real
Exec=$SCRIPT_DIR/run.sh
Icon=audio-input-microphone
Terminal=false
Categories=Utility;Accessibility;
StartupNotify=false
EOF
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    echo "==> Entrada de menú creada en $DESKTOP_DIR/dictado.desktop"
fi

echo ""
echo "✓ Instalación completa."
echo ""
echo "  Uso:"
echo "    cd vosk-dictado && ./run.sh"
echo ""
echo "  Atajo por defecto: Ctrl+Space"
echo "  Servidor por defecto: ws://xeon:8085/ws"
echo "  Config guardada en: ~/.config/vosk-dictado/config.ini"
if (( IS_WAYLAND )); then
    echo ""
    echo "  Sesión Wayland detectada:"
    echo "    · atajo global vía portal GlobalShortcuts (GNOME pide confirmar"
    echo "      la primera vez; se puede cambiar en Ajustes → Teclado)"
    echo "    · texto pegado vía portapapeles + uinput (evdev)"
    echo "    · ./run.sh relanza la app en un scope systemd para tener app id"
fi
echo ""
echo "  Comandos de voz:"
echo "    'listo enviar'  → escribe el texto donde está el cursor"
echo "    'listo copiar'  → copia el texto al portapapeles"
