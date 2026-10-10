# Tether for macOS

The Mac side of Tether: the same daemon and protocol as on Linux, with a menu-bar app in place of the GTK window and GNOME extension. It pairs with the same Android app.

- **Clipboard sync** both ways, text and images. Items password managers mark as secret stay on the Mac.
- **Send files** — drop them on the menu-bar icon or on a device in the Tether window, right-click in Finder → **Quick Actions → Send to Phone**, or `tether send FILE…`. Received files land in `~/Downloads/Tether`.
- **Browse the phone in Finder** — it mounts like a network drive and shows under Locations; the Mac appears on the phone as before.
- **Folder sync**, **remote commands**, **notifications to the phone**, **battery**, **transfer progress** — as on Linux.
- **Phone notifications in Notification Center**, with a Reply field for messaging apps; clearing one on the Mac clears it on the phone.
- **Wake-on-LAN** — the phone can wake the Mac from sleep.

Not yet: controlling the Mac's media from the phone (the phone hides its media controls).

```
tether/      Python daemon and CLI (from ../linux/tether, with macOS backends)
app/         Swift menu-bar app (SwiftUI + AppKit, macOS 13+)
tests/       end-to-end test, the app's socket protocol, parsers for ifconfig/networksetup/pmset
install.sh   installs the service and builds the app into ~/Applications
```

## Install

You need macOS 13 or later, Python 3.11 or newer (`brew install python`), and the Xcode command-line tools (`xcode-select --install`) to build the app.

```sh
cd macos
./install.sh
```

This puts the service in `~/Library/Application Support/Tether` (its own Python environment with `cryptography` and `aiohttp`), a `tether` command in `~/.local/bin`, and **Tether.app** in `~/Applications`, then opens it. Allow notifications and **local network access** when macOS asks — without the latter the Mac can't reach the phone. If the macOS firewall is on, the installer asks for your password to let Python accept the phone's connections.

The app runs the service and opens at login (turn that off under **Settings → Background Service**). Quitting the app stops the service.

**Without the app:** `./install.sh --headless` installs only the service, started at login by launchd (`~/Library/LaunchAgents/dev.tether.daemon.plist`). The clipboard is then text-only and notifications have no Reply button. On macOS 15 and later, a service started this way may be refused local network access; the app avoids that.

To remove it: `./uninstall.sh` (add `--purge` to forget pairings and settings too).

## Pair and use

Install the phone app as described in the [main README](../README.md#2-build-and-install-the-phone-app), open it, then open **Tether** from the menu bar → **Open Tether…** and press **Pair** next to the phone (or `tether pair`). Confirm only if both screens show the same six-digit code.

The CLI is the same as on Linux (`tether status`, `send`, `clip`, `share`, `sync`, `command`, `notify`, `set`…), with these differences:

| | |
|---|---|
| `tether mount` | mounts the phone and opens it in Finder; `--unmount` ejects it |
| `tether gui` | opens the Tether app |
| `tether wol enable` | turns on **Wake for network access** (asks for an administrator password). It wakes the Mac from sleep, not from shut down, and laptops only while on power. |
| `tether set media …` | not available |

Saved commands run through `/bin/sh` in your home folder, so Mac commands work as you'd expect:

```sh
tether command add "Lock screen" 'pmset displaysleepnow'
tether command add "Sleep" 'pmset sleepnow'
tether command add "Say hello" 'say hello'
```

## Where things are

| | |
|---|---|
| Settings, pairings, identity | `~/Library/Application Support/Tether` |
| Control socket | `~/Library/Application Support/Tether/run/ctl.sock` |
| Service log | `~/Library/Logs/Tether/daemon.log` |
| Received files | `~/Downloads/Tether` (change in the Folders tab or `tether set download_dir PATH`) |

## Troubleshooting

- **Not connecting:** check **System Settings → Privacy & Security → Local Network** has Tether switched on, and that both devices are on the same network. Ports 47290/udp and 47291/tcp must be reachable. To skip discovery, add `"static_peers": ["192.168.1.42:47291"]` to `config.json` and restart the service (Settings tab → Restart).
- **No notifications:** **System Settings → Notifications → Tether**.
- **“Send to Phone” missing in Finder:** **System Settings → Keyboard → Keyboard Shortcuts → Services → Files and Folders**, tick **Send to Phone**.
- **Finder shows the phone read-only or slow:** eject it (menu-bar menu → **Eject Phone**) and open it again.

## Development

```sh
python3 tests/test_e2e.py           # two daemons: pairing, clipboard, send, browse, WebDAV (incl. Finder locking), sync, …
python3 tests/test_app_protocol.py  # the control-socket messages the app depends on
python3 tests/test_parsers.py       # ifconfig, networksetup and pmset output
app/build.sh                        # builds app/build/Tether.app
```

The daemon tests also run on Linux. The app talks to the daemon over the control socket only: it subscribes with `{"cmd": "subscribe", "app": true}`, which makes it the clipboard owner (`clip_set`, `clip_image_set` ⇄ `clip_local`, `clip_local_image`) and the notification poster (`desk_notify`, `desk_close` ⇄ `desk_action`, `desk_closed`).
