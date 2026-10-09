from gi.repository import Gio, GLib
XML = """<node>
<interface name="org.mpris.MediaPlayer2"><property name="Identity" type="s" access="read"/></interface>
<interface name="org.mpris.MediaPlayer2.Player">
<method name="PlayPause"/><method name="Play"/><method name="Pause"/><method name="Next"/><method name="Previous"/><method name="Stop"/>
<method name="Seek"><arg type="x" direction="in"/></method>
<method name="SetPosition"><arg type="o" direction="in"/><arg type="x" direction="in"/></method>
<property name="PlaybackStatus" type="s" access="read"/><property name="Metadata" type="a{sv}" access="read"/>
<property name="Position" type="x" access="read"/><property name="CanControl" type="b" access="read"/>
<property name="CanPlay" type="b" access="read"/><property name="CanPause" type="b" access="read"/>
<property name="CanGoNext" type="b" access="read"/><property name="CanGoPrevious" type="b" access="read"/><property name="CanSeek" type="b" access="read"/>
</interface></node>"""
node = Gio.DBusNodeInfo.new_for_xml(XML)
st = {"status": "Playing", "track": 1, "pos": 30_000_000}
def meta():
    return GLib.Variant("a{sv}", {"mpris:trackid": GLib.Variant("o", f"/t/{st['track']}"),
        "xesam:title": GLib.Variant("s", f"Track {st['track']}"), "xesam:artist": GLib.Variant("as", ["The Band"]),
        "xesam:album": GLib.Variant("s", "LP"), "mpris:length": GLib.Variant("x", 240_000_000)})
conn_ref = {}
def changed(props):
    conn_ref["c"].emit_signal(None, "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties", "PropertiesChanged",
        GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", props, [])))
def call(conn, sender, path, iface, method, params, inv):
    print("CALL", method, params.unpack() if params else None, flush=True)
    if method == "PlayPause":
        st["status"] = "Paused" if st["status"] == "Playing" else "Playing"
        changed({"PlaybackStatus": GLib.Variant("s", st["status"])})
    elif method == "Next":
        st["track"] += 1; changed({"Metadata": meta()})
    elif method == "Seek":
        st["pos"] += params.unpack()[0]
    elif method == "SetPosition":
        st["pos"] = params.unpack()[1]
    inv.return_value(None)
def getp(conn, sender, path, iface, prop):
    return {"Identity": GLib.Variant("s", "Fake Player"), "PlaybackStatus": GLib.Variant("s", st["status"]),
            "Metadata": meta(), "Position": GLib.Variant("x", st["pos"])}.get(prop, GLib.Variant("b", True))
def acquired(conn, name):
    conn_ref["c"] = conn
    for i in node.interfaces:
        conn.register_object("/org/mpris/MediaPlayer2", i, call, getp, None)
    print("READY", flush=True)
Gio.bus_own_name(Gio.BusType.SESSION, "org.mpris.MediaPlayer2.fakeplayer", 0, acquired, None, None)
GLib.MainLoop().run()
