#!/bin/sh
# Installs Tether for the current user on macOS 13 or later.
#
#   ./install.sh             the Tether app (menu bar) plus the background service it runs
#   ./install.sh --headless  only the service, started by launchd; no app, so the clipboard
#                            is text-only and notifications have no Reply button
set -eu

here=$(cd "$(dirname "$0")" && pwd)
support="$HOME/Library/Application Support/Tether"
apps="$HOME/Applications"
agent="$HOME/Library/LaunchAgents/dev.tether.daemon.plist"
label=dev.tether.daemon
uid=$(id -u)
headless=0
[ "${1:-}" = "--headless" ] && headless=1

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = Darwin ] || die "this installer is for macOS; on Linux use ../linux/install.sh"

find_python() {
    for p in python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        command -v "$p" >/dev/null 2>&1 || continue
        if "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            command -v "$p"
            return 0
        fi
    done
    return 1
}

if ! py=$(find_python); then
    command -v brew >/dev/null || die "Tether needs Python 3.11 or newer. Install Homebrew (https://brew.sh), then: brew install python"
    say "Installing Python with Homebrew"
    brew install python
    py=$(find_python) || die "Python 3.11 or newer still not found"
fi

if [ $headless = 0 ] && ! xcrun --find swift >/dev/null 2>&1; then
    die "building the app needs the Xcode command-line tools: run  xcode-select --install  and try again
       (or install without the app: ./install.sh --headless)"
fi

say "Stopping any running Tether"
osascript -e 'tell application id "dev.tether.Tether" to quit' >/dev/null 2>&1 || true
launchctl bootout "gui/$uid/$label" 2>/dev/null || true
sleep 1

say "Installing the service to ~/Library/Application Support/Tether"
mkdir -p "$support/bin" "$support/lib" "$HOME/Library/Logs/Tether"
if ! "$support/venv/bin/python3" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    rm -rf "$support/venv"
    "$py" -m venv "$support/venv"
fi
"$support/venv/bin/python3" -m pip install --quiet --disable-pip-version-check "cryptography>=41" "aiohttp>=3.9"
rm -rf "$support/lib/tether"
cp -R "$here/tether" "$support/lib/"
find "$support/lib/tether" -name __pycache__ -prune -exec rm -rf {} +
cat > "$support/bin/tether" <<EOF
#!/bin/sh
PYTHONPATH="$support/lib" exec "$support/venv/bin/python3" -m tether "\$@"
EOF
chmod +x "$support/bin/tether"
mkdir -p "$HOME/.local/bin"
ln -sf "$support/bin/tether" "$HOME/.local/bin/tether"

if [ $headless = 1 ]; then
    say "Starting the service at login (launchd)"
    mkdir -p "$(dirname "$agent")"
    cat > "$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>$support/bin/tether</string>
		<string>daemon</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>5</integer>
	<key>ProcessType</key>
	<string>Background</string>
	<key>StandardOutPath</key>
	<string>$HOME/Library/Logs/Tether/daemon.log</string>
	<key>StandardErrorPath</key>
	<string>$HOME/Library/Logs/Tether/daemon.log</string>
</dict>
</plist>
EOF
    launchctl bootstrap "gui/$uid" "$agent"
else
    # The app runs the service itself, so it shares the app's Local Network permission.
    [ -f "$agent" ] && rm -f "$agent"
    say "Building the Tether app"
    build=$(mktemp -d)
    "$here/app/build.sh" "$build" >/dev/null
    mkdir -p "$apps"
    rm -rf "$apps/Tether.app"
    ditto "$build/Tether.app" "$apps/Tether.app"
    rm -rf "$build"
    # Registers “Send to Phone” with Finder's Quick Actions / Services.
    /System/Library/CoreServices/pbs -update 2>/dev/null || true
fi

fw=/usr/libexec/ApplicationFirewall/socketfilterfw
if [ -x $fw ] && $fw --getglobalstate 2>/dev/null | grep -q enabled; then
    say "The macOS firewall is on; allowing Python to accept connections from the phone (asks for your password)"
    "$support/venv/bin/python3" - <<'EOF' | sort -u | while read -r bin; do
import os, sys
seen = {os.path.realpath(sys.executable), os.path.realpath(getattr(sys, "_base_executable", sys.executable))}
app = os.path.join(sys.base_prefix, "Resources", "Python.app", "Contents", "MacOS", "Python")
if os.path.exists(app):
    seen.add(os.path.realpath(app))
print("\n".join(seen))
EOF
        sudo $fw --add "$bin" >/dev/null && sudo $fw --unblockapp "$bin" >/dev/null
    done
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) say "Add ~/.local/bin to your PATH to use the tether command";; esac

if [ $headless = 1 ]; then
    say "Done. Install the app on your phone, open it, then run: tether pair"
else
    open "$apps/Tether.app"
    say "Done. Tether is in the menu bar. Allow notifications and local network access when macOS asks,"
    say "then install the app on your phone and pair from the Tether window."
fi
