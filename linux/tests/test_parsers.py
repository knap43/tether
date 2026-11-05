"""Unit tests for output parsers of external tools (wpctl, gdbus)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether.desknotify import gv_string, parse_signal  # noqa: E402
from tether.media import parse_volume  # noqa: E402

assert parse_volume("Volume: 0.45") == {"level": 0.45, "muted": False}
assert parse_volume("Volume: 1.00 [MUTED]") == {"level": 1.0, "muted": True}
assert parse_volume("nothing") is None

assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 12, 'reply')") \
    == ("action", 12, "reply")
assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.NotificationClosed (uint32 7, uint32 2)") \
    == ("closed", 7, 2)
assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.ActivationToken (uint32 7, 'x')") is None
assert gv_string('say "hi"\n\\ ok') == '"say \\"hi\\"\\n\\\\ ok"'
from tether.sleepwatch import parse_prepare  # noqa: E402

assert parse_prepare("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)") is True
assert parse_prepare("/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)") is False
assert parse_prepare("/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionNew ('3', objectpath '/x')") is None
print("ALL PARSER TESTS PASSED")
