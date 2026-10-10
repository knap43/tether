import AppKit
import ServiceManagement
import UniformTypeIdentifiers

/// The app's actions, shared by the menu-bar menu, the window and the Finder service.
final class Controller {
    let model = AppModel()
    let daemon = DaemonClient()
    let supervisor = Supervisor()
    lazy var clipboard = ClipboardBridge(daemon: daemon)
    lazy var notifier = Notifier(daemon: daemon)
    var onStatusChange: (() -> Void)?
    var showWindow: (() -> Void)?
    var windowVisible: () -> Bool = { false }

    private var pairing: [String: PairingPanel] = [:]

    func start() {
        daemon.onEvent = { [weak self] ev in self?.handle(ev) }
        daemon.onRunning = { [weak self] up in
            guard let self else { return }
            model.running = up
            if up {
                model.problem = nil
                refreshWol()
            } else {
                supervisor.ensureRunning(after: 1)
            }
            onStatusChange?()
        }
        supervisor.onProblem = { [weak self] text in self?.model.problem = text }
        daemon.start()
        supervisor.ensureRunning(after: 1.5)
        clipboard.start()
        notifier.onOpenMessage = { [weak self] path in
            if let path {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            } else {
                self?.showWindow?()
            }
        }
        notifier.start()

        // Open at login by default, once; the Settings tab turns it off.
        if !UserDefaults.standard.bool(forKey: "loginItemSet") {
            UserDefaults.standard.set(true, forKey: "loginItemSet")
            try? SMAppService.mainApp.register()
        }
    }

    // MARK: events

    private func handle(_ ev: DaemonClient.Event) {
        let o = ev.object
        switch ev.name {
        case "status":
            guard let st = try? snakeDecoder.decode(Status.self, from: ev.raw) else { return }
            apply(st)
        case "clip_set":
            if let text = o["text"] as? String { clipboard.set(text: text) }
        case "clip_image_set":
            if let b64 = o["data"] as? String, let data = Data(base64Encoded: b64) {
                clipboard.set(image: data, mime: o["mime"] as? String ?? "image/png")
            }
        case "desk_notify":
            guard let nid = o["nid"] as? Int else { return }
            let actions = (o["actions"] as? [[String: Any]] ?? []).compactMap { $0["key"] as? String }
            notifier.post(nid: nid, app: o["app"] as? String ?? "", title: o["title"] as? String ?? "",
                          body: o["body"] as? String ?? "", icon: o["icon"] as? String ?? "", actions: actions)
        case "desk_close":
            if let nid = o["nid"] as? Int { notifier.close(nid: nid) }
        case "notify":
            let title = o["title"] as? String ?? "Tether"
            // Received files get a clickable notification of their own (file_received).
            if !title.hasPrefix("File from ") { notifier.message(title, o["body"] as? String ?? "") }
        case "file_received":
            let path = o["path"] as? String ?? ""
            let name = (path as NSString).lastPathComponent
            let from = model.status?.devices.first { $0.id == o["from"] as? String }?.name ?? "your phone"
            notifier.message("File from \(from)", name, open: path)
            model.show("Received \(name)")
        case "pair_request":
            if o["initiator"] as? Bool != true, let id = o["id"] as? String {
                showPairing(id: id, name: o["name"] as? String ?? "?", sas: o["sas"] as? String ?? "", initiator: false)
            }
            refresh()
        case "paired":
            closePairing(o["id"] as? String)
            model.show("Paired with \(o["name"] as? String ?? "the device")")
            refresh()
        case "pair_failed":
            closePairing(o["id"] as? String)
            report(o["reason"] as? String == "timeout" ? "Pairing timed out" : "Pairing was cancelled")
            refresh()
        case "transfer":
            guard let t = try? snakeDecoder.decode(Transfer.self, from: ev.raw) else { return }
            model.update(t)
            if t.state == "failed" { report("Couldn't transfer \(t.name)") }
            onStatusChange?()
        default:
            break
        }
    }

    private func apply(_ st: Status) {
        model.status = st
        clipboard.enabled = st.clipboard
        for t in st.transfers { model.update(t) }
        for p in st.pending where !p.confirmed && !p.initiator && pairing[p.id] == nil {
            showPairing(id: p.id, name: p.name, sas: p.sas, initiator: false)
        }
        for id in pairing.keys where !st.pending.contains(where: { $0.id == id }) {
            closePairing(id)
        }
        onStatusChange?()
    }

    func refresh() {
        daemon.call("status") { [weak self] res in
            guard res["ok"] as? Bool == true, let data = try? JSONSerialization.data(withJSONObject: res),
                  let st = try? snakeDecoder.decode(Status.self, from: data) else { return }
            self?.apply(st)
        }
    }

    func refreshWol() {
        daemon.call("wol_status") { [weak self] res in
            guard let list = res["interfaces"],
                  let data = try? JSONSerialization.data(withJSONObject: list),
                  let ifs = try? snakeDecoder.decode([WolInterface].self, from: data) else { return }
            self?.model.wol = ifs
        }
    }

    /// Feedback: a toast in the window when it's open, a notification otherwise.
    func report(_ text: String) {
        if windowVisible() {
            model.show(text)
        } else {
            notifier.message("Tether", text)
        }
    }

    /// Runs a command, reporting failures (and `success`, if given).
    func run(_ cmd: String, _ args: [String: Any] = [:], success: String? = nil,
             then: (([String: Any]) -> Void)? = nil) {
        daemon.call(cmd, args) { [weak self] res in
            guard let self else { return }
            if res["ok"] as? Bool == true {
                if let success { report(success) }
                then?(res)
            } else {
                report(friendly(res["error"]))
            }
        }
    }

    func set(_ key: String, _ value: Any, success: String? = nil) {
        run("set", ["key": key, "value": value], success: success)
    }

    // MARK: pairing

    func pair(device: String? = nil, address: String? = nil) {
        var args: [String: Any] = [:]
        if let device { args["device"] = device }
        if let address { args["addr"] = address }
        run("pair", args) { [weak self] res in
            guard let id = res["device"] as? String else { return }
            self?.showPairing(id: id, name: res["name"] as? String ?? "?", sas: res["sas"] as? String ?? "",
                              initiator: true)
        }
    }

    func answer(_ id: String, accept: Bool) {
        if accept {
            pairing[id]?.waiting = true
        } else {
            closePairing(id)
        }
        run("pair_confirm", ["device": id, "accept": accept])
    }

    private func showPairing(id: String, name: String, sas: String, initiator: Bool) {
        if let panel = pairing[id] {
            panel.present()
            return
        }
        let panel = PairingPanel(name: name, sas: sas) { [weak self] accept in self?.answer(id, accept: accept) }
        pairing[id] = panel
        panel.present()
    }

    private func closePairing(_ id: String?) {
        guard let id, let panel = pairing.removeValue(forKey: id) else { return }
        panel.close()
    }

    // MARK: devices

    func pickAndSend(to device: String) {
        let panel = NSOpenPanel()
        panel.title = "Send to Phone"
        panel.prompt = "Send"
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK else { return }
        send(panel.urls.map(\.path), to: device)
    }

    func send(_ paths: [String], to device: String?) {
        var isDir: ObjCBool = false
        let files = paths.filter { FileManager.default.fileExists(atPath: $0, isDirectory: &isDir) && !isDir.boolValue }
        guard !files.isEmpty else {
            report("Only files can be sent, not folders")
            return
        }
        guard let target = device ?? model.status?.defaultTarget?.id else {
            report(model.status?.connected.isEmpty ?? true ? "No phone is connected"
                   : "Several devices are connected; send from the Tether menu")
            return
        }
        let noun = files.count == 1 ? (files[0] as NSString).lastPathComponent : "\(files.count) files"
        run("send", ["device": target, "paths": files], success: "Sent \(noun)")
    }

    func sendClipboard() {
        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else {
            report("The clipboard has no text")
            return
        }
        run("clip_send", ["text": text], success: "Clipboard sent")
    }

    func askNotification(to device: Device) {
        let alert = NSAlert()
        alert.messageText = "Notify \(device.name)"
        alert.informativeText = "Shows a notification on the phone."
        let title = NSTextField(frame: NSRect(x: 0, y: 30, width: 280, height: 24))
        title.placeholderString = "Title"
        let text = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        text.placeholderString = "Text (optional)"
        let box = NSView(frame: NSRect(x: 0, y: 0, width: 280, height: 54))
        box.addSubview(title)
        box.addSubview(text)
        alert.accessoryView = box
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = title
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let t = title.stringValue.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        run("notify", ["device": device.id, "title": t, "text": text.stringValue], success: "Notification sent")
    }

    func confirmUnpair(_ device: Device) {
        let alert = NSAlert()
        alert.messageText = "Unpair \(device.name)?"
        alert.informativeText = "It will need to be paired again before it can connect."
        alert.addButton(withTitle: "Unpair").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        run("unpair", ["device": device.id])
    }

    func askAddress() {
        let alert = NSAlert()
        alert.messageText = "Pair by Address"
        alert.informativeText = "The phone's IP address, shown in the Tether app on the phone."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        field.placeholderString = "192.168.1.42"
        alert.accessoryView = field
        alert.addButton(withTitle: "Pair")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let addr = field.stringValue.trimmingCharacters(in: .whitespaces)
        if !addr.isEmpty { pair(address: addr) }
    }

    // MARK: files

    func browse() {
        run("mount") { res in
            if let path = res["path"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
        }
    }

    func eject() { run("unmount", success: "Ejected") }

    func openDownloads() {
        guard let dir = model.status?.downloadDir else { return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    func pickFolder(_ title: String, then: @escaping (String) -> Void) {
        let panel = NSOpenPanel()
        panel.title = title
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url { then(url.path) }
    }

    func changeDownloadDir() {
        pickFolder("Save Received Files In") { [weak self] path in self?.set("download_dir", path) }
    }

    func addShare() {
        pickFolder("Share a Folder") { [weak self] path in
            guard let self else { return }
            let base = (path as NSString).lastPathComponent.replacingOccurrences(of: "/", with: "-")
            var name = base.isEmpty ? "Folder" : base
            var i = 2
            let taken = Set(model.status?.shares.keys.map { $0 } ?? [])
            while taken.contains(name) {
                name = "\(base) \(i)"
                i += 1
            }
            run("share_add", ["name": name, "path": path], success: "Sharing \(name)")
        }
    }

    func addSync() {
        pickFolder("Sync a Folder") { [weak self] path in
            guard let self else { return }
            let alert = NSAlert()
            alert.messageText = "Name This Folder"
            alert.informativeText = "\(path)\n\nUse the same name when adding the folder on the phone."
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
            field.stringValue = Self.folderID((path as NSString).lastPathComponent)
            alert.accessoryView = field
            alert.addButton(withTitle: "Sync")
            alert.addButton(withTitle: "Cancel")
            alert.window.initialFirstResponder = field
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            let id = Self.folderID(field.stringValue)
            if !id.isEmpty { run("sync_add", ["folder": id, "path": path], success: "Syncing “\(id)”") }
        }
    }

    static func folderID(_ s: String) -> String {
        let lowered = s.lowercased()
        var out = ""
        var dash = false
        for ch in lowered {
            if ch.isASCII && (ch.isLetter || ch.isNumber || ch == "_" || ch == "-") {
                out.append(ch)
                dash = false
            } else if !dash {
                out.append("-")
                dash = true
            }
        }
        return out.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    // MARK: settings

    func setRemoteShell(_ on: Bool) {
        guard on else {
            set("remote_shell", false)
            return
        }
        let alert = NSAlert()
        alert.messageText = "Allow Any Command?"
        alert.informativeText = "Your phone will be able to run any command on this Mac as you. "
            + "Keep a screen lock on the phone."
        alert.addButton(withTitle: "Allow").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else {
            onStatusChange?()
            return
        }
        set("remote_shell", true)
    }

    func enableWol() {
        run("wol_enable", ["ifname": ""], success: "Wake for network access is on") { [weak self] _ in
            self?.refreshWol()
        }
    }

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                report("Couldn't change Open at Login: \(error.localizedDescription)")
            }
            model.objectWillChange.send()
        }
    }

    func restartService() {
        supervisor.restart()
        report("Restarting the Tether service…")
    }
}
