// Tether GNOME Shell extension.
//
// Mutter does not let background programs watch the clipboard, so this
// extension does it from inside the shell and relays changes to the Tether
// daemon over its control socket. It also adds a panel menu for quick actions
// and pairing prompts.

import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Meta from 'gi://Meta';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const RETRY_SECONDS = 5;
const MAX_CLIP_BYTES = 1024 * 1024;
const SECRET_HINT = 'x-kde-passwordManagerHint';
const MAX_IMAGE_BYTES = 16 * 1024 * 1024;
const TEXT_TYPES = ['text/plain;charset=utf-8', 'UTF8_STRING', 'text/plain', 'STRING', 'TEXT'];
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

function batteryText(d) {
    if (!d.battery)
        return d.name;
    return `${d.name} · ${d.battery.level}%${d.battery.charging ? ' ⚡' : ''}`;
}

function socketPath() {
    return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'tether', 'ctl.sock']);
}

function tetherBinary() {
    return GLib.find_program_in_path('tether') ??
        GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'tether']);
}

// A line-delimited JSON connection to the daemon that reconnects on its own.
class DaemonLink {
    constructor(onEvent, onState) {
        this._onEvent = onEvent;
        this._onState = onState;
        this._cancellable = new Gio.Cancellable();
        this._conn = null;
        this._out = null;
        this._retryId = 0;
        this._connect();
    }

    get connected() {
        return this._conn !== null;
    }

    _connect() {
        this._retryId = 0;
        const client = new Gio.SocketClient();
        const addr = Gio.UnixSocketAddress.new(socketPath());
        client.connect_async(addr, this._cancellable, (c, res) => {
            try {
                this._conn = c.connect_finish(res);
            } catch (e) {
                if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    this._scheduleRetry();
                return;
            }
            this._out = new Gio.DataOutputStream({base_stream: this._conn.get_output_stream()});
            const input = new Gio.DataInputStream({base_stream: this._conn.get_input_stream()});
            this._onState(true);
            this.send({cmd: 'subscribe', clipboard: true});
            this._readLoop(input);
        });
    }

    _readLoop(input) {
        input.read_line_async(GLib.PRIORITY_DEFAULT, this._cancellable, (stream, res) => {
            let line;
            try {
                [line] = stream.read_line_finish_utf8(res);
            } catch (e) {
                line = null;
            }
            if (line === null) {
                this._drop();
                return;
            }
            try {
                this._onEvent(JSON.parse(line));
            } catch (e) {
                logError(e, 'Tether: bad message from daemon');
            }
            this._readLoop(input);
        });
    }

    _drop() {
        if (this._conn) {
            try {
                this._conn.close(null);
            } catch (e) {}
        }
        this._conn = null;
        this._out = null;
        if (!this._cancellable.is_cancelled()) {
            this._onState(false);
            this._scheduleRetry();
        }
    }

    _scheduleRetry() {
        if (this._retryId || this._cancellable.is_cancelled())
            return;
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, RETRY_SECONDS, () => {
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
    }

    send(obj) {
        if (!this._out)
            return false;
        try {
            this._out.put_string(`${JSON.stringify(obj)}\n`, null);
            return true;
        } catch (e) {
            this._drop();
            return false;
        }
    }

    destroy() {
        this._cancellable.cancel();
        if (this._retryId)
            GLib.source_remove(this._retryId);
        this._retryId = 0;
        if (this._conn) {
            try {
                this._conn.close(null);
            } catch (e) {}
        }
        this._conn = null;
        this._out = null;
    }
}

const TetherIndicator = GObject.registerClass(
class TetherIndicator extends PanelMenu.Button {
    _init(ext) {
        super._init(0.0, 'Tether');
        this._ext = ext;
        this._icon = new St.Icon({icon_name: 'phone-symbolic', style_class: 'system-status-icon'});
        this.add_child(this._icon);

        this._statusItem = new PopupMenu.PopupMenuItem('Daemon not running', {reactive: false});
        this.menu.addMenuItem(this._statusItem);

        this._pairSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._pairSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._browseItem = this.menu.addAction('Browse phone files', () => ext.browse());
        this._sendItem = this.menu.addAction('Send files…', () => ext.spawnTether(['send', '--pick']));
        this._clipItem = this.menu.addAction('Send clipboard now', () => ext.sendClipboardNow());

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._clipSwitch = new PopupMenu.PopupSwitchMenuItem('Clipboard sync', true);
        this._clipSwitch.connect('toggled', (_item, state) => {
            ext.link?.send({cmd: 'set', key: 'clipboard', value: state});
            ext.link?.send({cmd: 'status'});
        });
        this.menu.addMenuItem(this._clipSwitch);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Open Tether', () => ext.spawnTether(['gui']));
    }

    update(status, daemonUp) {
        if (!daemonUp || !status) {
            this._statusItem.label.text = 'Daemon not running';
            this._icon.opacity = 120;
            for (const item of [this._browseItem, this._sendItem, this._clipItem])
                item.setSensitive(false);
            this._pairSection.removeAll();
            return;
        }
        const connected = status.devices.filter(d => d.connected);
        this._statusItem.label.text = connected.length
            ? `Connected: ${connected.map(batteryText).join(', ')}`
            : status.devices.length ? 'Phone not connected' : 'No paired devices — run “tether pair”';
        this._icon.opacity = connected.length ? 255 : 120;
        for (const item of [this._browseItem, this._sendItem, this._clipItem])
            item.setSensitive(connected.length > 0);
        this._clipSwitch.setToggleState(Boolean(status.clipboard));

        this._pairSection.removeAll();
        for (const p of status.pending.filter(x => !x.confirmed)) {
            const code = `${p.sas.slice(0, 3)} ${p.sas.slice(3)}`;
            this._pairSection.addMenuItem(new PopupMenu.PopupMenuItem(
                `Pair with ${p.name}? Code ${code}`, {reactive: false}));
            this._pairSection.addAction('    Codes match — pair', () =>
                this._ext.link?.send({cmd: 'pair_confirm', device: p.id, accept: true}));
            this._pairSection.addAction('    Reject', () =>
                this._ext.link?.send({cmd: 'pair_confirm', device: p.id, accept: false}));
        }
        if (status.pending.some(x => !x.confirmed))
            this._icon.opacity = 255;
    }
});

export default class TetherExtension extends Extension {
    enable() {
        this._status = null;
        this._lastRemote = null;
        this._indicator = new TetherIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this.link = new DaemonLink(ev => this._onEvent(ev), up => {
            if (!up)
                this._status = null;
            this._indicator.update(this._status, up);
        });

        this._selection = global.display.get_selection();
        this._ownerChangedId = this._selection.connect('owner-changed', (_sel, type, source) => {
            if (type === Meta.SelectionType.SELECTION_CLIPBOARD)
                this._onClipboardChanged(source);
        });
    }

    disable() {
        if (this._ownerChangedId)
            this._selection.disconnect(this._ownerChangedId);
        this._ownerChangedId = 0;
        this._selection = null;
        this.link?.destroy();
        this.link = null;
        this._indicator?.destroy();
        this._indicator = null;
        this._status = null;
    }

    _onClipboardChanged(source) {
        let types = [];
        try {
            types = source?.get_mimetypes?.() ?? [];
            // Respect password managers that mark their copies as secret.
            if (types.includes(SECRET_HINT))
                return;
        } catch (e) {}
        const image = IMAGE_TYPES.find(t => types.includes(t));
        if (image && !TEXT_TYPES.some(t => types.includes(t))) {
            St.Clipboard.get_default().get_content(St.ClipboardType.CLIPBOARD, image, (_cb, bytes) => {
                const data = bytes?.get_data?.();
                if (!data || data.length === 0 || data.length > MAX_IMAGE_BYTES)
                    return;
                const b64 = GLib.base64_encode(data);
                if (b64 === this._lastRemoteImage)
                    return;
                this.link?.send({cmd: 'clip_local_image', mime: image, data: b64});
            });
            return;
        }
        St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => {
            if (!text || text === this._lastRemote || text.length > MAX_CLIP_BYTES)
                return;
            this.link?.send({cmd: 'clip_local', text});
        });
    }

    sendClipboardNow() {
        St.Clipboard.get_default().get_text(St.ClipboardType.CLIPBOARD, (_cb, text) => {
            if (text)
                this.link?.send({cmd: 'clip_send', text});
        });
    }

    browse() {
        if (!this._status)
            return;
        try {
            Gio.AppInfo.launch_default_for_uri(this._status.dav_url, null);
        } catch (e) {
            Main.notify('Tether', `Could not open ${this._status.dav_url}: ${e.message}`);
        }
    }

    spawnTether(args) {
        try {
            Gio.Subprocess.new([tetherBinary(), ...args], Gio.SubprocessFlags.NONE);
        } catch (e) {
            Main.notify('Tether', `Could not run tether: ${e.message}`);
        }
    }

    _onEvent(ev) {
        switch (ev.ev) {
        case 'status':
            this._status = ev;
            this._indicator.update(ev, true);
            break;
        case 'clip_set':
            this._lastRemote = ev.text;
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, ev.text);
            break;
        case 'clip_image_set':
            this._lastRemoteImage = ev.data;
            St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD, ev.mime,
                new GLib.Bytes(GLib.base64_decode(ev.data)));
            break;
        case 'notify':
            Main.notify(ev.title, ev.body);
            break;
        case 'pair_request':
        case 'pair_failed':
        case 'paired':
            this.link?.send({cmd: 'status'});
            break;
        default:
            // Responses to our own commands (status, etc.).
            if (ev.ok && ev.devices)
                this._onEvent({...ev, ev: 'status'});
        }
    }
}
