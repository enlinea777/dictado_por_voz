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
