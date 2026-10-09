#!/bin/sh
# Runs the Android app's Kotlin core against the Linux daemon.
# Needs: kotlinc (pacman -S kotlin), and the kotlinx-serialization jars, which
# land in ~/.gradle after the Android app has been built once.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
core="$here/../../android/app/src/main/java/dev/tether/core"
out=$(mktemp -d)
trap 'rm -r "$out"' EXIT

jar_for() {
    find "$HOME/.gradle/caches/modules-2" -name "$1-*.jar" ! -name "*sources*" 2>/dev/null | sort | tail -n 1
}
ser_core=$(jar_for kotlinx-serialization-core-jvm)
ser_json=$(jar_for kotlinx-serialization-json-jvm)
[ -n "$ser_core" ] && [ -n "$ser_json" ] || { echo "build the Android app once so Gradle fetches kotlinx-serialization"; exit 1; }
stdlib=$(dirname "$(command -v kotlinc)")/../lib/kotlin-stdlib.jar

kotlinc -cp "$ser_core:$ser_json" "$core"/*.kt "$here/Harness.kt" -d "$out/harness.jar"
python3 "$here/test_interop.py" "$out/harness.jar:$stdlib:$ser_core:$ser_json"
