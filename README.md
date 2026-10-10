# Tether

Your Android phone and your Linux computer or Mac, joined on the home network:

- **Clipboard sync** both ways (text).
- **Quick send** — push files from either side; they land in `~/Downloads/Tether` or the phone's `Download/Tether`.
- **Browse live** — the phone appears in GNOME Files; the computer appears in Android's Files app, every "open file" dialog, and Tether's own **Browse** screen.
- **Folder sync** — chosen folders kept identical on both devices, with deletions and conflicts handled.
- **Remote commands** — run your computer's saved commands from the phone (or, if you allow it, any shell command) and see the output.
- **Notifications** — `tether notify` pops a notification on the phone, handy at the end of long scripts.
- **Phone notifications on the desktop** — reply to messages from GNOME; dismissing on one side dismisses on the other.
- **Media control** — play, pause, skip, seek and volume for whatever plays on the computer, from the phone.
- **Battery** — the phone's charge in the top bar and the window, with a low-battery warning.
- **Images on the clipboard** — copy a screenshot on one device, paste it on the other.
- **Transfer progress** — progress bars with Cancel on both sides.
- **Wake-on-LAN** — switch the computer on from the phone.

Everything travels over an authenticated, encrypted channel of its own (see `PROTOCOL.md`). No cloud, no accounts, no Google services — it runs fine on LineageOS and GrapheneOS.

```
linux/      Python daemon, CLI, GNOME Shell extension, installer
macos/      the same daemon with macOS backends, a Swift menu-bar app, installer — see macos/README.md
android/    Android app (Kotlin, no dependencies beyond kotlinx-serialization)
tests/      Linux end-to-end test and a Kotlin ⇄ Python interop test
```

## 1. Install on the computer (Arch, GNOME)

On a Mac, follow [macos/README.md](macos/README.md) instead, then continue at step 2.

```sh
cd linux
./install.sh
```

This installs the daemon to `~/.local/lib/tether` with a `tether` command in `~/.local/bin`, enables the `tether` systemd user service, installs the GNOME Shell extension (a phone icon in the top bar), adds **Scripts → Send to Phone** to the Files right-click menu, and bookmarks **Phone (Tether)** in the Files sidebar. It asks for `sudo` only to install `python-cryptography` and `python-aiohttp`, and to open ports 47290/udp and 47291/tcp if a firewall is active.

On Wayland a newly installed extension takes effect after you log out and back in; then run `gnome-extensions enable tether@tether.local` if the installer couldn't.

The installer also adds **Tether** to the app grid: a GTK window for everything below — pairing, sending files (or dropping them onto a device), shared and synced folders, phone commands, and settings. The top-bar menu opens it too, and `tether gui` from a terminal.

## 2. Build and install the phone app

You need JDK 17 and the Android SDK (platform 35).

**With Android Studio** (`android-studio` from the AUR): open the `android/` folder and press Run.

**From the terminal:**

```sh
sudo pacman -S jdk17-openjdk android-tools
# Android command-line tools: https://developer.android.com/studio#command-tools
sdkmanager "platforms;android-35" "build-tools;35.0.0"
export ANDROID_HOME=~/Android/Sdk          # wherever the SDK lives
cd android
./gradlew assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Open **Tether** on the phone and grant what it asks for: notifications, **All files access** (needed for browsing and sync), and **background activity**.

## 3. Pair

With both on the same network, run `tether pair` on the computer (or tap **Pair** next to the computer in the app). Both screens show a six-digit code; confirm on each side only if they match. Pairing requests started from the phone appear in the top-bar menu, or answer them with `tether accept`.

If discovery is blocked (guest networks, "AP isolation"), pair by address: `tether pair --addr 192.168.1.42` or **Pair by IP address** in the app.

## 4. Everyday use

| | On the computer | On the phone |
|---|---|---|
| Clipboard | Automatic | Computer → phone automatic; phone → computer via the app, the **Send clipboard** Quick Settings tile, the notification button, or **Send to computer** in the text-selection menu (or automatic — see below) |
| Send files | Right-click → Scripts → Send to Phone; top-bar menu → Send files…; `tether send FILE…` | Share sheet → **Send to computer**; **Send files** in the app |
| Browse | Files sidebar → **Phone (Tether)**, or `tether mount` — opens straight into the phone's storage | **Browse** next to the computer in the app (open, download, upload, rename, delete), or Files app → your computer's name |
| Sync a folder | `tether sync add camera ~/Pictures/Phone` | **Sync a folder** → pick the folder → name it `camera` |

A sync folder must have the **same name** on both devices. Conflicting edits keep both versions: the older one is renamed `name.conflict-YYYYmmdd-HHMMSS.ext` and synced too. Shared folders for browsing are set with `tether share add NAME PATH` (default: your home folder) and **Share a folder** in the app (default: internal storage).

In the app's **Browse** screen, tapping a file streams it straight into whichever app opens it; **⋮ → Download** saves a copy to `Download/Tether`, and the toolbar uploads files or makes a folder in the folder you're in. Downloads and uploads show progress with Cancel, like any other transfer.

### Commands from the phone

Save commands on the computer; they appear as buttons under **Commands** next to your computer in the app:

```sh
tether command add "Lock screen" 'loginctl lock-session'
tether command add "Suspend" 'systemctl suspend'
tether command list
tether command remove "Suspend"
```

To also type arbitrary commands on the phone, run `tether set remote_shell on` (off by default). Commands run as you, via your login shell (`$shell -l -c`, so fish/zsh/bash config applies) in your home folder, and are stopped after 120 s (`tether set command_timeout 300` to change). The phone shows exit status and up to 64 KiB of output. Each free-form command also raises a desktop notification, so nothing runs unnoticed. With the shell on, anyone holding your unlocked phone holds your account — keep a screen lock.

### Phone notifications, media and battery

- **Notifications:** in the phone app, tap **Allow notification access**. Notifications then appear on the desktop; those from messaging apps get a **Reply** button. Turn individual apps off under **Settings → Apps** in the Tether window.
- **Media:** tap **Media** next to your computer in the app, or use the playback notification that appears while something plays. This uses `playerctl`, which the installer adds, and works with any player that supports MPRIS (Spotify, Firefox, mpv, Rhythmbox…). Volume uses PipeWire's `wpctl`. To keep the phone out of it, turn off **Settings → Media** in the Tether window (or `tether set media off`).
- **Battery:** shown next to the phone in the top-bar menu and the Tether window; a notification warns at 15%.

### Wake-on-LAN

Whenever the phone connects, the computer tells it the hardware address of each network card. When the computer is asleep or off, its card in the app shows **Wake up**.

On the computer, check and enable it once (Settings → Wake on LAN in the Tether window does the same):

```sh
tether wol           # status of each interface
tether wol enable    # turns on magic-packet wake for the wired connection (NetworkManager)
```

Also enable **Wake on LAN** (sometimes “Power on by PCI-E”) in the BIOS/UEFI. Wake works reliably over Ethernet; most Wi-Fi cards can't be woken. Packets go to the home network's broadcast address, so the phone must be on the same network.

### Notifications to the phone

```sh
tether notify "Backup finished" "312 files, no errors"
make && tether notify "Build passed" || tether notify "Build failed"
```

Other commands: `tether status`, `tether clip "text"`, `tether unpair NAME`, `tether set clipboard off`.

### Automatic clipboard from the phone

Android lets apps read the clipboard only while they're on screen. To lift that for Tether, enable USB debugging, connect the phone, and run once:

```sh
adb shell pm grant dev.tether android.permission.READ_LOGS
adb shell appops set dev.tether SYSTEM_ALERT_WINDOW allow
```

Then tap **Restart Tether** in the app. Whenever you copy something, Tether briefly flashes an invisible window to read it and send it on.

## Security

- Each device has a P-256 identity key (on the phone it lives in the Android Keystore). Sessions use an ephemeral ECDH handshake, signed by those keys, and AES-256-GCM.
- Pairing is protected against interception by the six-digit code, which is derived from the handshake itself.
- Unpaired devices can do nothing but ask to pair. A paired phone may run only the commands you saved, unless you turn on `remote_shell`. Browsing is confined to the folders you share; `..` and symlinks leading outside are refused.
- The Files bridge on the computer listens only on 127.0.0.1, behind a random token in the URL. `…/Phone/` is the connected phone (its folder itself when it shares just one); the bridge's top level lists every connected device and its shares.
- Password managers that mark copies as secret are not synced from GNOME.

## Troubleshooting

- Logs: `journalctl --user -u tether -f` on the computer; `adb logcat -s Tether` on the phone.
- Not connecting: check that both are on the same network and that ports 47290/udp and 47291/tcp are open. To skip discovery entirely, add `"static_peers": ["192.168.1.42:47291"]` to `~/.config/tether/config.json` and restart the service.
- The phone disconnects when idle: allow background activity in the app, and on some ROMs exempt Tether from battery optimisation in Settings.

## Limits

- The clipboard carries text and images (up to 16 MB), not files.
- Sync is between two devices at a time per folder, and very large folders (tens of thousands of files) send sizeable indexes.
- Android's shared storage is case-insensitive; two files differing only in case on Linux will collide there.

## Development

```sh
python3 linux/tests/test_e2e.py     # two Linux daemons: pairing, clipboard, send, browse, WebDAV, sync, commands, reconnect
tests/interop/run.sh                # the Android app's Kotlin core against the Linux daemon (needs kotlinc)
linux/tests/dbus/run.sh            # real D-Bus: desktop notifications via gdbus, media via playerctl (fake services)
python3 linux/tests/test_parsers.py # parsers for playerctl, wpctl and gdbus output
dbus-run-session -- xvfb-run -a env GDK_BACKEND=x11 python3 linux/tests/gui_snapshot.py shots/
                                    # drives the GTK window against real daemons and saves a PNG of each page
```
