#!/usr/bin/env bash
# install.sh — Instala dependencias para vosk-dictado
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "==> Dependencias del sistema (apt)…"
sudo apt-get update -q
sudo apt-get install -y \
    python3-tk \
    python3-pip \
    python3-venv \
    python3-full \
    portaudio19-dev \
    xdotool \
    xclip

echo "==> Creando entorno virtual en $VENV_DIR…"
python3 -m venv --system-site-packages "$VENV_DIR"

echo "==> Dependencias Python…"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

# La librería 'keyboard' en Linux necesita acceso a /dev/input
# → añadimos el usuario actual al grupo 'input' si no está ya
if ! groups "$USER" | grep -qw input; then
    echo "==> Añadiendo $USER al grupo 'input' (necesario para hotkeys globales)…"
    sudo usermod -aG input "$USER"
    echo "   ⚠  Cierra sesión y vuelve a entrar para que el cambio de grupo tenga efecto."
    echo "   Mientras tanto, ejecuta:  newgrp input && ./run.sh"
else
    echo "==> El usuario $USER ya pertenece al grupo 'input' ✓"
fi

echo "==> Creando lanzador run.sh…"
cat > "$SCRIPT_DIR/run.sh" << 'EOF'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/dictado.py" "$@"
EOF
chmod +x "$SCRIPT_DIR/run.sh"

echo ""
echo "✓ Instalación completa."
echo ""
echo "  Uso:"
echo "    cd vosk-dictado && ./run.sh"
echo ""
echo "  Atajo por defecto: Ctrl+Space"
echo "  Servidor por defecto: ws://xeon:8085/ws"
echo "  Config guardada en: ~/.config/vosk-dictado/config.ini"
echo ""
echo "  Comandos de voz:"
echo "    'listo enviar'  → escribe el texto donde está el cursor"
echo "    'listo copiar'  → copia el texto al portapapeles"
