#!/usr/bin/env bash
# MIDI Mixer for Linux installer: curl -fsSL https://raw.githubusercontent.com/adamkahl/midi-mixer-linux/main/install.sh | bash
# Installs to ~/.local (no root), adds an app-menu entry, and installs system packages only if they're missing.
# Uninstall: bash install.sh --uninstall
set -euo pipefail
URL="https://raw.githubusercontent.com/adamkahl/midi-mixer-linux/main"
DIR="$HOME/.local/share/midi-mixer-linux"
BIN="$HOME/.local/bin/midi-mixer"
DESKTOP="$HOME/.local/share/applications/midi-mixer.desktop"

if [ "${1:-}" = "--uninstall" ]; then
    pkill -f "$DIR/midimixer.py" || true
    rm -rf "$DIR" "$BIN" "$DESKTOP" "$HOME/.config/autostart/midi-mixer.desktop"
    echo "MIDI Mixer removed. Your settings are kept in ~/.config/midi-mixer-linux (delete it to remove them)."
    exit 0
fi

have_deps() {
    command -v pactl && command -v aseqdump && command -v aconnect && command -v stdbuf &&
        python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
} >/dev/null 2>&1

if ! have_deps; then
    echo "Installing required packages (needs sudo)…"
    if command -v apt-get >/dev/null; then
        sudo apt-get install -y python3-gi gir1.2-gtk-3.0 pulseaudio-utils alsa-utils
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3-gobject gtk3 pulseaudio-utils alsa-utils
    elif command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm python-gobject gtk3 libpulse alsa-utils
    elif command -v zypper >/dev/null; then
        sudo zypper install -y python3-gobject-Gdk typelib-1_0-Gtk-3_0 pulseaudio-utils alsa-utils
    else
        echo "Please install: Python 3 GObject bindings with GTK 3, pactl, and alsa-utils (aseqdump, aconnect)." >&2
        exit 1
    fi
fi

mkdir -p "$DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
src="${BASH_SOURCE[0]:-}"   # empty when piped from curl
if [ -f "$src" ] && [ -f "$(dirname "$src")/midimixer.py" ]; then   # run from a clone: install that copy
    here="$(dirname "$src")"
    cp "$here/midimixer.py" "$here/midi-mixer.svg" "$DIR/"
else
    for f in midimixer.py midi-mixer.svg; do
        curl -fsSL "$URL/$f" -o "$DIR/$f.tmp" && mv "$DIR/$f.tmp" "$DIR/$f"
    done
fi
chmod +x "$DIR/midimixer.py"
ln -sf "$DIR/midimixer.py" "$BIN"
cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=MIDI Mixer
Comment=Control app volumes with a MIDI controller
Icon=$DIR/midi-mixer.svg
Exec=python3 "$DIR/midimixer.py"
Categories=AudioVideo;Audio;Mixer;
EOF

echo "MIDI Mixer installed. Open it from your app menu, or run: midi-mixer"
setsid python3 "$DIR/midimixer.py" >/dev/null 2>&1 </dev/null &
