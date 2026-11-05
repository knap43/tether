#!/bin/sh
# Real D-Bus checks: desktop notifications via gdbus,
# against fake services on a private session bus. Needs python-gobject.
set -eu
cd "$(dirname "$0")"
dbus-run-session -- sh -c '
  python3 fake_notification_server.py & n=$!
  python3 fake_mpris_player.py & m=$!
  sleep 1
  python3 check_notifications.py
  kill $n $m'
