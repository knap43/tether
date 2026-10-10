"""Parsers for macOS command output (ifconfig, networksetup, pmset).

Run:  python3 tests/test_parsers.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tether import wol  # noqa: E402

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=6460<TSO4,TSO6,CHANNEL_IO,PARTIAL_CSUM,ZEROINVERT_CSUM>
\tether 3c:22:fb:12:34:56
\tinet6 fe80::1c2b:aaaa:bbbb:cccc%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
\tstatus: active
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether a0:ce:c8:01:02:03
\tinet 10.0.0.7 netmask 0xfffffc00
\tstatus: active
en1: flags=8963<UP,BROADCAST,SMART,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tether 36:8a:11:00:00:01
\tmedia: autoselect <full-duplex>
\tstatus: inactive
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 36:8a:11:00:00:00
\tinet 169.254.1.1 netmask 0xffff0000 broadcast 169.254.255.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet 100.64.0.2 --> 100.64.0.2 netmask 0xffffffff
"""

PORTS = """\

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 3c:22:fb:12:34:56

Hardware Port: USB 10/100/1000 LAN
Device: en7
Ethernet Address: a0:ce:c8:01:02:03

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 36:8a:11:00:00:00

VLAN Configurations
===================
"""

PMSET = """\
System-wide power settings:
Currently in use:
 standby              1
 Sleep On Power Button 1
 womp                 1
 hibernatefile        /var/vm/sleepimage
 powernap             1
 networkoversleep     0
 disksleep            10
"""


def main():
    ifs = wol.parse_ifconfig(IFCONFIG)
    assert set(ifs) == {"en0", "en7", "bridge0"}, ifs
    assert ifs["en0"] == {"mac": "3c:22:fb:12:34:56", "ip": "192.168.1.23", "netmask": "255.255.255.0",
                          "broadcast": "192.168.1.255", "active": True}, ifs["en0"]
    assert ifs["en7"]["netmask"] == "255.255.252.0" and "broadcast" not in ifs["en7"]
    print("ok  ifconfig")

    ports = wol.parse_hardware_ports(PORTS)
    assert ports == {"en0": "Wi-Fi", "en7": "USB 10/100/1000 LAN", "bridge0": "Thunderbolt Bridge"}, ports
    print("ok  networksetup")

    assert wol.parse_womp(PMSET) is True
    assert wol.parse_womp(PMSET.replace("womp                 1", "womp                 0")) is False
    assert wol.parse_womp("System-wide power settings:\n") is None
    print("ok  pmset")

    outputs = {("networksetup", "-listallhardwareports"): PORTS, ("ifconfig",): IFCONFIG, ("pmset", "-g"): PMSET}
    wol._run = lambda *args: outputs[args]
    got = wol.interfaces()
    assert [i["ifname"] for i in got] == ["en7", "en0"], got  # wired first, bridge left out
    assert got[0]["broadcast"] == "10.0.3.255" and got[0]["wired"] and got[0]["enabled"] is True, got[0]
    assert got[1]["port"] == "Wi-Fi" and not got[1]["wired"], got[1]
    assert wol.broadcast_addresses() == ["10.0.3.255", "192.168.1.255"]
    assert wol.magic_packet("3c:22:fb:12:34:56") == b"\xff" * 6 + bytes.fromhex("3c22fb123456") * 16
    print("ok  interfaces")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
