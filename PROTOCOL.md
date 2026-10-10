# Tether protocol, version 1

Both implementations (`linux/` in Python, `android/` in Kotlin) speak exactly this.
All integers are big-endian. All JSON is UTF-8.

## 1. Identity

Each device owns a long-term ECDSA P-256 key. Its public key is exchanged as
X.509 SubjectPublicKeyInfo DER (`spki`). The **device id** is
`hex(SHA-256(spki))` — 64 lowercase hex characters.

## 2. Discovery (UDP 47290)

Every 10 s each device broadcasts a beacon to `255.255.255.255:47290`:

```json
{"tether": 1, "id": "<device id>", "name": "Arch desktop", "port": 47291, "type": "desktop"}
```

`type` is `desktop` or `phone`. On hearing a beacon from a device it is not
connected to, a device answers once (at most every 5 s per peer) with a unicast
beacon to the sender, so one-way broadcast filtering is survivable.

For a **paired** peer that is not connected, the device whose id sorts lower
opens the TCP connection. Both sides also retry the peer's last known address
every 30 s. If two sessions to the same peer ever coexist, the one whose
client has the lower id survives.

## 3. Secure channel (TCP 47291)

### 3.1 Handshake

Each side immediately sends a 102-byte hello:

| bytes | content |
|---|---|
| 4 | `TTHR` |
| 1 | version `0x01` |
| 65 | ephemeral P-256 public key, uncompressed point |
| 32 | random |

Then, with `CH`/`SH` the client's and server's hellos:

```
th    = SHA-256("tether-v1" || CH || SH)
Z     = ECDH(own ephemeral private, peer ephemeral public)
okm   = HKDF-SHA256(ikm = Z, salt = th, info = "tether-v1 keys", L = 64)
k_c2s = okm[0:32]     k_s2c = okm[32:64]
```

### 3.2 Frames

```
u32 length || AES-256-GCM(key, nonce, plaintext)      (length includes the 16-byte tag)
nonce = 0x00000000 || u64 counter                      (per direction, starts at 0)
```

Ciphertext length must be 16 … 4 MiB + 64; anything else closes the connection.
The first plaintext byte is the kind:

* `0x01` — a JSON message follows.
* `0x02` — data chunk: `u32 stream id || bytes`. A zero-length chunk ends the stream.

Chunks carry at most 64 KiB. Stream ids are allocated by the **sender**: odd
on the TCP client, even on the server.

### 3.3 Authentication

The first frame in each direction is:

```json
{"t": "auth", "id_key": "<base64 spki>", "sig": "<base64 DER ECDSA-SHA256>", "name": "...", "type": "phone"}
```

`sig` signs `"tether-v1 auth " || role || th`, where role is the ASCII string
`client` or `server` of the **signer**. A device that sees its own id closes.

## 4. Pairing

A session with an unknown device is *untrusted* and only `pair_req`, `pair_ok`,
`pair_no`, `ping`, `pong` and `unpaired` are allowed; anything else is answered
with `unpaired` and the connection closes.

Both sides derive a short authentication string:

```
SAS = (u32(SHA-256("tether-v1 sas" || th)[0:4]) mod 1 000 000), zero-padded to 6 digits
```

1. The initiator sends `{"t": "pair_req"}`.
2. Both users compare the SAS; each side sends `{"t": "pair_ok"}` or `{"t": "pair_no"}`.
3. Having both sent and received `pair_ok`, each side stores the peer
   (id, name, spki) and the session becomes trusted in place.

Pairing that is not completed within 120 s is dropped.
`{"t": "unpaired"}` from an authenticated peer means it no longer trusts us;
the receiver forgets that peer and closes.

## 5. Messages on a trusted session

`ping` → `pong` (sent every 30 s; a session silent for 90 s is closed).

### Requests

Requests carry `"req": <int>`; the answer is
`{"t": "res", "req": n, "ok": true, ...}` or `{"t": "res", "req": n, "ok": false, "error": "<code>"}`.
Error codes: `not_found`, `exists`, `not_dir`, `is_dir`, `denied`, `changed`,
`io`, `bad_request`, `unsupported`.

A message with a `"stream"` field announces an incoming stream; the receiver
must be ready for its chunks before handling anything after that message.
A receiver that no longer wants a stream sends `{"t": "cancel", "stream": n}`;
a sender that gives up mid-stream sends `{"t": "abort", "stream": n}` instead
of the end chunk.

### Clipboard

`{"t": "clip", "text": "..."}` — set the receiver's clipboard. Text only, ≤ 1 MiB.

### Quick send

A request `{"t": "send", "req": n, "stream": s, "name": "photo.jpg", "size": 1234, "sha256": "<hex>"}`
followed by the stream. The receiver saves into its download folder under a
unique name, verifies size and hash, and answers once the stream has ended
(`"name"` is the name it was saved under).

### Browsing

Virtual paths look like `/<share>/<relative path>`; `/` lists the shares.
Entries are `{"name": "...", "dir": bool, "size": int, "mtime": <ms>}`.

| request | fields | answer |
|---|---|---|
| `fs_list` | `path` | `entries` |
| `fs_stat` | `path` | `entry` |
| `fs_read` | `path`, `offset`, `length` (−1 = to end) | `stream`, `size` (whole file), `length`, then the stream |
| `fs_write` | `path`, `stream`, then the stream | answered after the stream ends; written atomically |
| `fs_mkdir` | `path` | — |
| `fs_delete` | `path` (directories recursively) | — |
| `fs_move` | `from`, `to`, `overwrite` | — |

Paths are confined to the share's real path; `..` is refused.

### Remote commands (served by the desktop)

| request | fields | answer |
|---|---|---|
| `cmd_list` | — | `commands` (`[{"id", "name"}]`), `shell` (bool: free-form allowed) |
| `cmd_run` | `id` of a saved command, **or** `shell` (a command line) | `exit`, `output` (stdout+stderr, ≤ 64 KiB), `truncated`, `timed_out` |

The desktop runs the line with the user's login shell (`<shell> -l -c`) in the user's home, in its own
process group, and kills the group after its time limit. `shell` is refused
with `denied` unless the user enabled it on the desktop. The answer comes
when the process exits; background children are not waited for.

### Notifications (served by the phone)

Request `{"t": "notify", "title": "...", "text": "..."}` — the phone shows a
notification attributed to the sending device.

### Battery (phone → desktop)

`{"t": "battery", "level": 0–100, "charging": bool}` — sent when a session
becomes trusted and whenever either value changes.

### Phone notifications (phone ⇄ desktop)

* `{"t": "notif_posted", "key", "app", "app_name", "title", "text", "time", "reply": bool, "icon": <base64 PNG or null>}`
  — a notification appeared or changed on the phone. `key` is the phone's
  notification key; a repeated key replaces the earlier one.
* `{"t": "notif_removed", "key"}` — it is gone from the phone.
* `{"t": "notif_dismiss", "key"}` (desktop → phone) — the user dismissed it on the desktop.
* Request `{"t": "notif_reply", "key", "text"}` (desktop → phone) — answer it
  through its inline-reply action; `not_found` if it no longer offers one.

The desktop decides which apps it shows (a mute list keyed by `app`).

### Clipboard images (both ways)

Request `{"t": "clip_image", "req", "stream", "mime": "image/png", "size"}`
followed by the image (≤ 16 MiB). The receiver puts it on its clipboard.

### Media (phone controls the desktop's players)

| request | fields | answer |
|---|---|---|
| `media_state` | — | `players`, `volume`, `available`, `disabled` (true while the user has media control off: no players, and `media_cmd` is refused with `denied`) |
| `media_cmd` | `action`, `player` (optional), `value` | same as `media_state`, after the command |

Actions: `play_pause`, `play`, `pause`, `next`, `previous`, `stop`,
`seek` (value: seconds, ±), `position` (value: ms), `volume` (value: 0–1),
`mute` (toggle). A player is `{"name", "identity", "status", "title",
"artist", "album", "length", "position", "at"}` (times in ms); the first
player is the most relevant one (playing before paused). The desktop pushes
`{"t": "media_update", ...same fields}` to phones when something changes.

### Wake-on-LAN (desktop → phone)

`{"t": "host_info", "wol": [{"mac", "broadcast", "ip", "ifname", "wired"}]}` is
sent to phones when a session becomes trusted. The phone stores it with the
pairing and, to wake the desktop, sends the standard magic packet
(6 × `0xFF`, then the MAC 16 times) over UDP ports 9 and 7 to each broadcast
address, `255.255.255.255` and the last known IP.

### Transfer progress

Progress is local to each side; no messages are added. Cancelling an
outgoing transfer sends `abort`; cancelling an incoming one sends `cancel`
(see Requests above), and the sender's request fails with `cancelled`.

### Folder sync

A sync folder has the same `folder` id on both devices. Each device keeps an
index of its files: `relpath → [sha256 | null, size, mtime_ms]`, where `null`
is a deletion tombstone. Files only; names beginning `.tether` are ignored.

`{"t": "sync_index", "folder": "camera", "files": {...}}` is sent after a
session becomes trusted and whenever the local index changes.

Each device keeps, per peer and folder, a **base**: the hash both sides last
agreed on. For every path present in the remote index, with local hash `L`,
remote hash `R` and base `B`:

| condition | action |
|---|---|
| `L == R` | base := L |
| `R == B` | nothing (the peer will pull from us) |
| `L == B` | `R` null → delete local; else pull |
| both changed, `R` null | keep ours (modification beats deletion) |
| both changed, `L` null | pull |
| both modified | the side with the older `(mtime, device id)` renames its copy to `name.conflict-YYYYmmdd-HHMMSS.ext` and pulls |

Pulling uses the request `{"t": "sync_get", "folder": "...", "path": "...", "hash": "<hex>"}`
→ `stream`, `size`, `mtime`. The answer is `changed` if the file no longer has
that hash. The puller writes to a temporary file beside the target, verifies
the hash, checks that its local copy did not change meanwhile, renames into
place, sets the mtime, and records the new base.
