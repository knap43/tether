#!/bin/sh
# Builds Tether.app from the Swift package. Needs the Xcode command-line tools
# (xcode-select --install). Usage: ./build.sh [OUTPUT_DIR]   (default: ./build)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
out=${1:-"$here/build"}
app="$out/Tether.app"

cd "$here"
swift build -c release --product Tether
bin=$(swift build -c release --product Tether --show-bin-path)

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$bin/Tether" "$app/Contents/MacOS/Tether"
cp Resources/Info.plist "$app/Contents/Info.plist"
cp Resources/AppIcon.icns "$app/Contents/Resources/AppIcon.icns"
printf 'APPL????' > "$app/Contents/PkgInfo"

# Ad-hoc signature: enough for Notification Center, login items and the
# Local Network prompt on this Mac. It isn't notarized, so don't hand it out.
codesign --force --sign - --timestamp=none "$app"
echo "$app"
