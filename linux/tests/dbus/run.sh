#!/bin/sh
# Real D-Bus checks: desktop notifications via gdbus and media via playerctl,
# against fake services on a private session bus. Needs python-gobject, playerctl.
set -eu
cd "$(dirname "$0")"
dbus-run-session -- sh -c '
  python3 fake_notification_server.py & n=$!
  python3 fake_mpris_player.py & m=$!
  sleep 1
  python3 check_notifications.py
  python3 check_media.py
  kill $n $m'
