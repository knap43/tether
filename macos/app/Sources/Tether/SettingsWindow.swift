import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// The Tether window: devices, folders, commands and settings.
final class MainWindow: NSObject, NSWindowDelegate {
    private let controller: Controller
    private var window: NSWindow?

    init(controller: Controller) {
        self.controller = controller
    }

    var isVisible: Bool { window?.isVisible == true }

    func show() {
        if window == nil {
            let root = MainView(c: controller).environmentObject(controller.model)
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 600),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable],
                             backing: .buffered, defer: false)
            w.title = "Tether"
            w.contentViewController = NSHostingController(rootView: root)
            w.setContentSize(NSSize(width: 640, height: 600))
            w.minSize = NSSize(width: 520, height: 420)
            w.isReleasedWhenClosed = false
            _ = w.setFrameAutosaveName("TetherMain")
            w.delegate = self
            if !w.setFrameUsingName("TetherMain") { w.center() }
            window = w
        }
        // A menu-bar app shows in the Dock and app switcher only while its window is open.
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        controller.refresh()
        controller.refreshWol()
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}

// MARK: - Layout

private struct MainView: View {
    let c: Controller
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            if !model.running {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.yellow)
                    Text(model.problem ?? "The Tether service isn't running")
                    Spacer()
                    Button("Restart") { c.restartService() }
                }
                .padding(10)
                .background(.yellow.opacity(0.12))
            }
            TabView {
                DevicesTab(c: c).tabItem { Label("Devices", systemImage: "iphone") }
                FoldersTab(c: c).tabItem { Label("Folders", systemImage: "folder") }
                CommandsTab(c: c).tabItem { Label("Commands", systemImage: "terminal") }
                SettingsTab(c: c).tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .disabled(!model.running)
            .padding(.top, 8)
        }
        .overlay(alignment: .bottom) {
            if let toast = model.toast {
                Text(toast)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(.regularMaterial, in: Capsule())
                    .shadow(radius: 3)
                    .padding(.bottom, 16)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.2), value: model.toast)
        .frame(minWidth: 520, minHeight: 420)
    }
}

private struct Placeholder: View {
    let text: String
    var body: some View { Text(text).foregroundStyle(.secondary) }
}

private struct Row<Trailing: View>: View {
    let symbol: String?
    let title: String
    let subtitle: String?
    var dimmed = false
    @ViewBuilder let trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 10) {
            if let symbol {
                Image(systemName: symbol)
                    .font(.title3)
                    .frame(width: 24)
                    .foregroundStyle(dimmed ? .tertiary : .secondary)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
            Spacer()
            trailing()
        }
    }
}

// MARK: - Devices

private struct DevicesTab: View {
    let c: Controller
    @EnvironmentObject var model: AppModel

    var body: some View {
        Form {
            if !model.transfers.isEmpty {
                Section("Transfers") {
                    ForEach(model.transfers) { t in
                        Row(symbol: t.outgoing ? "arrow.up.doc" : "arrow.down.doc", title: t.name, subtitle: t.subtitle) {
                            if let f = t.fraction {
                                ProgressView(value: f).frame(width: 120)
                            } else {
                                ProgressView().controlSize(.small)
                            }
                            Button {
                                c.run("transfer_cancel", ["id": t.id])
                            } label: { Image(systemName: "xmark.circle.fill") }
                                .buttonStyle(.borderless)
                                .help("Cancel")
                        }
                    }
                }
            }
            if let pending = model.status?.pending, !pending.isEmpty {
                Section("Pairing") {
                    ForEach(pending) { p in
                        Row(symbol: "iphone.radiowaves.left.and.right", title: "Pairing with \(p.name)",
                            subtitle: "Code \(shortCode(p.sas))") {
                            if p.confirmed {
                                Text("Waiting for the other device…").foregroundStyle(.secondary)
                            } else {
                                Button("Reject") { c.answer(p.id, accept: false) }
                                Button("Codes Match") { c.answer(p.id, accept: true) }.buttonStyle(.borderedProminent)
                            }
                        }
                    }
                }
            }
            Section {
                let devices = model.status?.devices ?? []
                if devices.isEmpty { Placeholder(text: "No paired devices yet — pair one below") }
                ForEach(devices) { d in DeviceRow(c: c, device: d) }
            } header: {
                Text("Paired Devices")
            } footer: {
                Text("Drop files on a connected device to send them, or on the Tether icon in the menu bar.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section {
                let found = model.status?.discovered ?? []
                if found.isEmpty {
                    Row(symbol: nil, title: "Looking for devices…",
                        subtitle: "Open Tether on your phone; it appears here within a few seconds") {
                        ProgressView().controlSize(.small)
                    }
                }
                ForEach(found) { d in
                    Row(symbol: d.type == "phone" ? "iphone" : "desktopcomputer", title: d.name, subtitle: d.addr) {
                        Button("Pair") { c.pair(device: d.id) }.buttonStyle(.borderedProminent)
                    }
                }
            } header: {
                HStack {
                    Text("Nearby")
                    Spacer()
                    Button("Pair by Address…") { c.askAddress() }.buttonStyle(.link).font(.callout)
                }
            }
        }
        .formStyle(.grouped)
    }
}

private struct DeviceRow: View {
    let c: Controller
    let device: Device
    @State private var targeted = false

    var body: some View {
        Row(symbol: device.symbol, title: device.name, subtitle: device.subtitle, dimmed: !device.connected) {
            if device.connected {
                Button { c.pickAndSend(to: device.id) } label: { Image(systemName: "paperplane") }
                    .buttonStyle(.borderless).help("Send Files…")
                if device.isPhone {
                    Button { c.browse() } label: { Image(systemName: "folder") }
                        .buttonStyle(.borderless).help("Browse in Finder")
                }
            }
            Menu {
                if device.connected {
                    Button("Send Clipboard") { c.sendClipboard() }
                    Button("Send Notification…") { c.askNotification(to: device) }
                    Divider()
                }
                Button("Unpair…", role: .destructive) { c.confirmUnpair(device) }
            } label: {
                Image(systemName: "ellipsis.circle")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
        }
        .padding(4)
        .background(targeted ? Color.accentColor.opacity(0.15) : .clear, in: RoundedRectangle(cornerRadius: 6))
        .onDrop(of: [.fileURL], isTargeted: device.connected ? $targeted : .constant(false)) { providers in
            guard device.connected else { return false }
            loadURLs(providers) { paths in c.send(paths, to: device.id) }
            return true
        }
    }
}

/// File paths from dropped items, delivered on the main thread.
func loadURLs(_ providers: [NSItemProvider], then: @escaping ([String]) -> Void) {
    let group = DispatchGroup()
    var paths: [String] = []
    let lock = NSLock()
    for p in providers where p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
        group.enter()
        _ = p.loadObject(ofClass: URL.self) { url, _ in
            if let url, url.isFileURL {
                lock.lock()
                paths.append(url.path)
                lock.unlock()
            }
            group.leave()
        }
    }
    group.notify(queue: .main) { if !paths.isEmpty { then(paths) } }
}

// MARK: - Folders

private struct FoldersTab: View {
    let c: Controller
    @EnvironmentObject var model: AppModel

    var body: some View {
        Form {
            Section("Received Files") {
                Row(symbol: "tray.and.arrow.down", title: "Saved to", subtitle: model.status?.downloadDir) {
                    Button("Open") { c.openDownloads() }
                    Button("Change…") { c.changeDownloadDir() }
                }
            }
            Section {
                Row(symbol: "externaldrive.connected.to.line.below", title: "Phone in Finder",
                    subtitle: model.status?.mounted == true ? "Mounted — it's under Locations in the Finder sidebar"
                        : "Mounts the connected phone like a network drive") {
                    if model.status?.mounted == true { Button("Eject") { c.eject() } }
                    Button("Open in Finder") { c.browse() }
                        .disabled(!(model.status?.connected.contains(where: \.isPhone) ?? false))
                }
            } header: {
                Text("Phone Storage")
            }
            Section {
                let shares = model.status?.sortedShares ?? []
                if shares.isEmpty { Placeholder(text: "Nothing shared") }
                ForEach(shares) { s in
                    Row(symbol: "folder", title: s.name, subtitle: s.path) {
                        Button { c.run("share_remove", ["name": s.name]) } label: { Image(systemName: "minus.circle") }
                            .buttonStyle(.borderless).help("Stop Sharing")
                    }
                }
            } header: {
                HStack {
                    Text("Shared With Your Phone")
                    Spacer()
                    Button { c.addShare() } label: { Image(systemName: "plus") }.buttonStyle(.borderless)
                        .help("Share a Folder…")
                }
            } footer: {
                Text("Your phone can browse and edit these folders.").font(.caption).foregroundStyle(.secondary)
            }
            Section {
                let folders = model.status?.sync ?? []
                if folders.isEmpty { Placeholder(text: "No synced folders") }
                ForEach(folders) { f in
                    Row(symbol: "arrow.triangle.2.circlepath", title: f.id, subtitle: f.path) {
                        Button { c.run("sync_remove", ["folder": f.id]) } label: { Image(systemName: "minus.circle") }
                            .buttonStyle(.borderless).help("Stop Syncing")
                    }
                }
            } header: {
                HStack {
                    Text("Synced Folders")
                    Spacer()
                    Button { c.run("rescan", success: "Folders rescanned") } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.borderless).help("Sync Now")
                    Button { c.addSync() } label: { Image(systemName: "plus") }.buttonStyle(.borderless)
                        .help("Sync a Folder…")
                }
            } footer: {
                Text("Kept identical on both devices. Give the folder the same name on the phone.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Commands

private struct CommandsTab: View {
    let c: Controller
    @EnvironmentObject var model: AppModel
    @State private var name = ""
    @State private var line = ""

    var body: some View {
        Form {
            Section {
                let cmds = model.status?.commands ?? []
                if cmds.isEmpty { Placeholder(text: "No saved commands yet") }
                ForEach(cmds) { cmd in
                    Row(symbol: "terminal", title: cmd.name, subtitle: cmd.command) {
                        Button { c.run("command_remove", ["name": cmd.name]) } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless).help("Remove")
                    }
                }
            } header: {
                Text("Saved Commands")
            } footer: {
                Text("Buttons your phone can press. They run as you, through /bin/sh, in your home folder.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Add a Command") {
                TextField("Name", text: $name, prompt: Text("Lock screen"))
                TextField("Command line", text: $line, prompt: Text("pmset displaysleepnow"))
                    .font(.system(.body, design: .monospaced))
                HStack {
                    Spacer()
                    Button("Add") {
                        let n = name.trimmingCharacters(in: .whitespaces)
                        let l = line.trimmingCharacters(in: .whitespaces)
                        c.run("command_add", ["name": n, "command": l], success: "Added “\(n)”") { _ in
                            name = ""
                            line = ""
                        }
                    }
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty
                              || line.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            Section("Free-Form Commands") {
                Toggle(isOn: Binding(get: { model.status?.remoteShell ?? false }, set: { c.setRemoteShell($0) })) {
                    Text("Allow any command from the phone")
                    Text("Anyone holding your unlocked phone could run anything as you. "
                         + "Each command still shows a notification here.")
                }
                Stepper(value: Binding(get: { model.status?.commandTimeout ?? 120 },
                                       set: { c.set("command_timeout", $0) }),
                        in: 5...3600, step: 5) {
                    Text("Time limit: \(model.status?.commandTimeout ?? 120) s")
                    Text("Seconds before a running command is stopped")
                }
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Settings

private struct SettingsTab: View {
    let c: Controller
    @EnvironmentObject var model: AppModel
    @State private var name = ""

    private var version: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
    }

    var body: some View {
        Form {
            Section("This Mac") {
                HStack {
                    TextField("Name shown on your phone", text: $name)
                        .onSubmit(rename)
                    Button("Rename", action: rename)
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || name == model.status?.name)
                }
                LabeledContent("Device ID") {
                    Text(groupedID).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                }
            }
            Section("Clipboard") {
                Toggle(isOn: Binding(get: { model.status?.clipboard ?? true }, set: { c.set("clipboard", $0) })) {
                    Text("Sync clipboard")
                    Text("Text and images. Items password managers mark as secret stay on this Mac.")
                }
            }
            Section {
                Toggle(isOn: Binding(get: { model.status?.notifications ?? true },
                                     set: { c.set("notifications", $0) })) {
                    Text("Show phone notifications here")
                    Text("Reply from the notification; clearing one here clears it on the phone")
                }
            } header: {
                Text("Phone Notifications")
            } footer: {
                Text("Needs notification access, granted in the phone app.").font(.caption).foregroundStyle(.secondary)
            }
            Section("Apps") {
                let apps = model.status?.notifApps ?? []
                if apps.isEmpty { Placeholder(text: "Apps appear here once the phone forwards a notification") }
                ForEach(apps) { a in
                    Toggle(isOn: Binding(get: { !(model.status?.notifMuted.contains(a.app) ?? false) },
                                         set: { c.run("notif_mute", ["app": a.app, "muted": !$0]) })) {
                        Text(a.name)
                        Text(a.app)
                    }
                }
            }
            Section {
                if model.wol.isEmpty { Placeholder(text: "No network port with an address") }
                ForEach(model.wol) { i in
                    Row(symbol: i.wired ? "cable.connector" : "wifi", title: "\(i.port ?? i.ifname) (\(i.ifname))",
                        subtitle: i.mac) { EmptyView() }
                }
                LabeledContent("Wake for network access") {
                    switch model.wol.first?.enabled {
                    case .some(true):
                        Label("On", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                    default:
                        Button("Turn On…") { c.enableWol() }
                    }
                }
            } header: {
                Text("Wake on LAN")
            } footer: {
                Text("Lets the phone wake this Mac from sleep (not from shut down; laptops only on power).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("Background Service") {
                Toggle("Open Tether at login", isOn: Binding(get: { c.launchAtLogin }, set: { c.launchAtLogin = $0 }))
                LabeledContent("Tether service") {
                    HStack {
                        Text(model.running ? "Running" : "Stopped").foregroundStyle(.secondary)
                        Button("Restart") { c.restartService() }
                    }
                }
                LabeledContent("Version", value: version)
            }
        }
        .formStyle(.grouped)
        .onAppear { name = model.status?.name ?? "" }
        .onChange(of: model.status?.name) { new in
            if let new { name = new }
        }
    }

    private var groupedID: String {
        let id = String((model.status?.id ?? "").prefix(32))
        return stride(from: 0, to: id.count, by: 4).map { i -> String in
            let start = id.index(id.startIndex, offsetBy: i)
            return String(id[start..<id.index(start, offsetBy: min(4, id.count - i))])
        }.joined(separator: " ")
    }

    private func rename() {
        let n = name.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty, n != model.status?.name { c.set("name", n, success: "Renamed") }
    }
}
