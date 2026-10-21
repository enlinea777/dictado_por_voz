#!/usr/bin/env bash
# dictado — lanzador del widget (paquetes rpm / Arch)
VENV=/var/lib/dictado/venv
APP=/usr/share/dictado/dictado.py
LOG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/vosk-dictado"
LOG="$LOG_DIR/launcher.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

if [[ ! -x "$VENV/bin/python3" ]] \
   || ! "$VENV/bin/python3" -c "import sys" >/dev/null 2>&1; then
    MSG="ERROR: entorno Python roto en $VENV. Reinstala el paquete dictado."
    echo "$MSG" >&2
    echo "$(date -Is) $MSG" >> "$LOG" 2>/dev/null || true
    exit 1
fi

# Wayland: el portal GlobalShortcuts necesita un 'app id', que systemd deduce
# del scope 'app-<id>-<aleatorio>.scope'. Si no venimos de uno, relanzamos.
IS_WAYLAND=0
[[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]] && IS_WAYLAND=1
unit="$(head -1 /proc/self/cgroup 2>/dev/null)"
unit="${unit##*:}"
unit="${unit##*/}"
if (( IS_WAYLAND )) && [[ -z "${DICTADO_IN_SCOPE:-}" ]] \
   && [[ "$unit" != app-* ]] && command -v systemd-run >/dev/null 2>&1; then
    export DICTADO_IN_SCOPE=1
    exec systemd-run --user --scope --quiet \
        --unit="app-dictado-$(printf '%06x' $((RANDOM * RANDOM))).scope" \
        /usr/bin/dictado "$@"
fi

exec "$VENV/bin/python3" -W ignore "$APP" "$@" >>"$LOG" 2>&1
