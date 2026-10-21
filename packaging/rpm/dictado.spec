Name:           dictado
Version:        1.1.0
Release:        1%{?dist}
Summary:        Widget flotante de dictado por voz
License:        GPL-3.0-or-later
URL:            https://github.com/enlinea777/dictado_por_voz
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3
Requires:       python3-pip
Requires:       python3-tkinter
Requires:       portaudio
Requires:       xdotool
Requires:       xclip
Requires:       wl-clipboard

%description
Widget flotante de dictado por voz para Linux. Captura audio del micrófono,
lo envía a un servidor Vosk por WebSocket y transcribe el texto en tiempo
real. Incluye atajo global (portal GlobalShortcuts en Wayland y XGrabKey en
X11), envío del texto tecleando con uinput y comandos de voz ("listo enviar",
"listo copiar").

%prep
%setup -q

%install
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/dictado
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/128x128/apps
install -m 755 packaging/launcher.sh %{buildroot}%{_bindir}/dictado
install -m 644 dictado.py %{buildroot}%{_datadir}/dictado/dictado.py
install -m 644 requirements.txt %{buildroot}%{_datadir}/dictado/requirements.txt
install -m 644 packaging/dictado.desktop %{buildroot}%{_datadir}/applications/dictado.desktop
install -m 644 packaging/icon/dictado.png %{buildroot}%{_datadir}/icons/hicolor/128x128/apps/dictado.png

%post
VENV=/var/lib/dictado/venv
if [ -e "$VENV" ] && ! "$VENV/bin/python3" -c "import sys" >/dev/null 2>&1; then
    rm -rf "$VENV"
fi
if [ ! -x "$VENV/bin/python3" ]; then
    rm -rf "$VENV"
    python3 -m venv "$VENV" || exit 0
fi
"$VENV/bin/pip" install --quiet --upgrade pip || true
"$VENV/bin/pip" install --quiet -r %{_datadir}/dictado/requirements.txt || \
    echo "dictado: no se pudieron instalar las dependencias Python (¿sin red?)" >&2

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t %{_datadir}/icons/hicolor >/dev/null 2>&1 || true
fi

%preun
if [ "$1" -eq 0 ]; then
    rm -rf /var/lib/dictado/venv
fi

%files
%{_bindir}/dictado
%{_datadir}/dictado/dictado.py
%{_datadir}/dictado/requirements.txt
%{_datadir}/applications/dictado.desktop
%{_datadir}/icons/hicolor/128x128/apps/dictado.png

%changelog
* Wed Oct 21 2026 enlinea777 <enlinea777@gmail.com> - 1.1.0-1
- Soporte Wayland (portal GlobalShortcuts y envío por teclado virtual)
- Ventana que crece con el texto y botones anclados abajo
- Copia con menú contextual y atajo global
