#!/bin/sh
# Lanzador dentro del sandbox de Flatpak
SITE="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' | sed 's|^/usr|/app|')"
export PYTHONPATH="$SITE${PYTHONPATH:+:$PYTHONPATH}"
exec python3 /app/share/dictado/dictado.py "$@"
