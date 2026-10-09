#!/bin/sh
# Installs Tether for the current user (Arch Linux, GNOME).
set -eu

here=$(cd "$(dirname "$0")" && pwd)
lib="$HOME/.local/lib/tether"
bin="$HOME/.local/bin"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ext_uuid="tether@tether.local"
ext_dir="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$ext_uuid"
scripts="${XDG_DATA_HOME:-$HOME/.local/share}/nautilus/scripts"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if ! python3 -c 'import cryptography, aiohttp, gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' 2>/dev/null; then
    say "Installing dependencies"
    sudo pacman -S --needed python-cryptography python-aiohttp python-gobject gtk4 libadwaita
fi
command -v playerctl >/dev/null || {
    say "Installing playerctl (media control from the phone)"
    sudo pacman -S --needed playerctl
}

say "Installing daemon to $lib"
rm -rf "$lib"
mkdir -p "$lib" "$bin"
cp -r "$here/tether" "$lib/"
cat > "$bin/tether" <<EOF
#!/bin/sh
PYTHONPATH="$lib\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m tether "\$@"
EOF
chmod +x "$bin/tether"

say "Installing systemd user service"
mkdir -p "$units"
cp "$here/systemd/tether.service" "$units/"
systemctl --user daemon-reload
systemctl --user enable --now tether.service
systemctl --user restart tether.service

say "Installing GNOME Shell extension"
rm -rf "$ext_dir"
mkdir -p "$ext_dir"
cp "$here/gnome-extension/$ext_uuid/"* "$ext_dir/"
gnome-extensions enable "$ext_uuid" 2>/dev/null ||
    say "Log out and back in, then run: gnome-extensions enable $ext_uuid"

say "Adding Tether to the app grid"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$data/applications" "$data/icons/hicolor/scalable/apps"
sed "s|@BIN@|$bin/tether|" "$here/desktop/dev.tether.Tether.desktop" > "$data/applications/dev.tether.Tether.desktop"
cp "$here/desktop/dev.tether.Tether.svg" "$data/icons/hicolor/scalable/apps/"
update-desktop-database "$data/applications" 2>/dev/null || true

say "Adding “Send to Phone” to the Files right-click menu"
mkdir -p "$scripts"
cp "$here/nautilus/Send to Phone" "$scripts/"
chmod +x "$scripts/Send to Phone"

sleep 1
"$bin/tether" mount --bookmark --no-open >/dev/null 2>&1 || true

if systemctl is-active --quiet firewalld 2>/dev/null; then
    say "firewalld is active; opening Tether's ports"
    sudo firewall-cmd --permanent --add-port=47290/udp --add-port=47291/tcp && sudo firewall-cmd --reload
elif command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q active; then
    say "ufw is active; opening Tether's ports"
    sudo ufw allow 47290/udp && sudo ufw allow 47291/tcp
fi

case ":$PATH:" in *":$bin:"*) ;; *) say "Add $bin to your PATH to use the tether command";; esac
say "Done. Install the app on your phone, open it, then open Tether from the app grid to pair"
