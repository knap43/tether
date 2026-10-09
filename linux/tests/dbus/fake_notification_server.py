from gi.repository import Gio, GLib  # noqa: F811

XML = """<node><interface name="org.freedesktop.Notifications">
<method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
<signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
<signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal></interface></node>"""
node = Gio.DBusNodeInfo.new_for_xml(XML)
def call(conn, sender, path, iface, method, params, inv):
    if method == "Notify":
        print("NOTIFY", params.unpack(), flush=True)
        inv.return_value(GLib.Variant("(u)", (77,)))
        def later():
            conn.emit_signal(None, path, iface, "ActionInvoked", GLib.Variant("(us)", (77, "reply")))
            conn.emit_signal(None, path, iface, "NotificationClosed", GLib.Variant("(uu)", (77, 2)))
            return False
        GLib.timeout_add(300, later)
    else:
        print("CLOSE", params.unpack(), flush=True)
        inv.return_value(None)
def acquired(conn, name):
    conn.register_object("/org/freedesktop/Notifications", node.interfaces[0], call, None, None)
    print("READY", flush=True)
Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.Notifications", 0, acquired, None, None)
GLib.MainLoop().run()
