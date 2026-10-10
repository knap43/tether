#!/bin/sh
# Removes Tether. Pairings and settings are kept unless you pass --purge.
set -eu

support="$HOME/Library/Application Support/Tether"
label=dev.tether.daemon

osascript -e 'tell application id "dev.tether.Tether" to quit' >/dev/null 2>&1 || true
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$label.plist"
umount "$support/run/Phone" 2>/dev/null || true
rm -rf "$HOME/Applications/Tether.app" "${support:?}/venv" "${support:?}/lib" "${support:?}/bin"
[ "$(readlink "$HOME/.local/bin/tether" 2>/dev/null)" = "$support/bin/tether" ] && rm -f "$HOME/.local/bin/tether"
/System/Library/CoreServices/pbs -update 2>/dev/null || true

if [ "${1:-}" = "--purge" ]; then
    rm -rf "$support" "$HOME/Library/Logs/Tether"
    echo "Tether removed, with its pairings and settings."
else
    echo "Tether removed. Pairings and settings remain in ~/Library/Application Support/Tether (--purge removes them)."
fi
