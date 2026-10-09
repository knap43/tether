"""Unit tests for output parsers of external tools (playerctl, wpctl, gdbus)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether.desknotify import gv_string, parse_signal  # noqa: E402
from tether.media import SEP, parse_players, parse_volume  # noqa: E402

out = "\n".join([
    SEP.join(["firefox.instance_1_42", "firefox", "Paused", "Video", "", "", "", "5000000"]),
    SEP.join(["spotify", "spotify", "Playing", "Song", "Band", "Album", "215000000", "12345678"]),
    "garbage line",
])
ps = parse_players(out, now_ms=1)
assert [p["name"] for p in ps] == ["spotify", "firefox.instance_1_42"], ps
assert ps[0]["length"] == 215000 and ps[0]["position"] == 12345 and ps[0]["identity"] == "Spotify"
assert ps[1]["length"] is None and ps[1]["position"] == 5000

assert parse_volume("Volume: 0.45") == {"level": 0.45, "muted": False}
assert parse_volume("Volume: 1.00 [MUTED]") == {"level": 1.0, "muted": True}
assert parse_volume("nothing") is None

assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.ActionInvoked (uint32 12, 'reply')") \
    == ("action", 12, "reply")
assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.NotificationClosed (uint32 7, uint32 2)") \
    == ("closed", 7, 2)
assert parse_signal("/org/freedesktop/Notifications: org.freedesktop.Notifications.ActivationToken (uint32 7, 'x')") is None
assert gv_string('say "hi"\n\\ ok') == '"say \\"hi\\"\\n\\\\ ok"'
print("ALL PARSER TESTS PASSED")
