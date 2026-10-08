# Tether

Your Android phone and your Linux computer, joined on the home network:

- **Clipboard sync** both ways (text).
- **Quick send** — push files from either side; they land in `~/Downloads/Tether` or the phone's `Download/Tether`.
- **Browse live** — the phone appears in GNOME Files; the computer appears in Android's Files app and every "open file" dialog.
- **Folder sync** — chosen folders kept identical on both devices, with deletions and conflicts handled.

Everything travels over an authenticated, encrypted channel of its own (see `PROTOCOL.md`). No cloud, no accounts, no Google services — it runs fine on LineageOS and GrapheneOS.

```
linux/      Python daemon, CLI, GNOME Shell extension, installer
android/    Android app (Kotlin, no dependencies beyond kotlinx-serialization)
tests/      Linux end-to-end test and a Kotlin ⇄ Python interop test
```

## 1. Install on the computer (Arch, GNOME)

```sh
cd linux
./install.sh
```

This installs the daemon to `~/.local/lib/tether` with a `tether` command in `~/.local/bin`, enables the `tether` systemd user service, installs the GNOME Shell extension (a phone icon in the top bar), adds **Scripts → Send to Phone** to the Files right-click menu, and bookmarks **Phone (Tether)** in the Files sidebar. It asks for `sudo` only to install `python-cryptography` and `python-aiohttp`, and to open ports 47290/udp and 47291/tcp if a firewall is active.

On Wayland a newly installed extension takes effect after you log out and back in; then run `gnome-extensions enable tether@tether.local` if the installer couldn't.

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
| Browse | Files sidebar → **Phone (Tether)**, or `tether mount` | Files app → your computer's name |
| Sync a folder | `tether sync add camera ~/Pictures/Phone` | **Sync a folder** → pick the folder → name it `camera` |

A sync folder must have the **same name** on both devices. Conflicting edits keep both versions: the older one is renamed `name.conflict-YYYYmmdd-HHMMSS.ext` and synced too. Shared folders for browsing are set with `tether share add NAME PATH` (default: your home folder) and **Share a folder** in the app (default: internal storage).

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
- Unpaired devices can do nothing but ask to pair. Browsing is confined to the folders you share; `..` and symlinks leading outside are refused.
- The Files bridge on the computer listens only on 127.0.0.1, behind a random token in the URL.
- Password managers that mark copies as secret are not synced from GNOME.

## Troubleshooting

- Logs: `journalctl --user -u tether -f` on the computer; `adb logcat -s Tether` on the phone.
- Not connecting: check that both are on the same network and that ports 47290/udp and 47291/tcp are open. To skip discovery entirely, add `"static_peers": ["192.168.1.42:47291"]` to `~/.config/tether/config.json` and restart the service.
- The phone disconnects when idle: allow background activity in the app, and on some ROMs exempt Tether from battery optimisation in Settings.

## Limits

- The clipboard carries text only.
- Sync is between two devices at a time per folder, and very large folders (tens of thousands of files) send sizeable indexes.
- Android's shared storage is case-insensitive; two files differing only in case on Linux will collide there.

## Development

```sh
python3 linux/tests/test_e2e.py     # two Linux daemons: pairing, clipboard, send, browse, WebDAV, sync, reconnect
tests/interop/run.sh                # the Android app's Kotlin core against the Linux daemon (needs kotlinc)
```
