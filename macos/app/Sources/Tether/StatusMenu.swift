import AppKit

/// A menu item that runs a closure.
final class ActionItem: NSMenuItem {
    private let handler: () -> Void

    init(_ title: String, key: String = "", symbol: String? = nil, _ handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: key)
        target = self
        if let symbol { image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil) }
    }

    @available(*, unavailable)
    required init(coder: NSCoder) { fatalError() }

    @objc private func fire() { handler() }
}

/// The menu-bar icon: a menu of devices and actions, and a drop target for files.
final class StatusMenu: NSObject, NSMenuDelegate, NSWindowDelegate, NSDraggingDestination {
    private let controller: Controller
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let menu = NSMenu()
    private var menuOpen = false
    var openWindow: (() -> Void)?

    init(controller: Controller) {
        self.controller = controller
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        item.menu = menu
        item.button?.toolTip = "Tether"
        update()
        DispatchQueue.main.async { self.acceptDrops() }
    }

    /// Files dropped on the icon go to the connected phone. The status bar window
    /// passes drag messages to its delegate.
    private func acceptDrops() {
        guard let window = item.button?.window else { return }
        window.registerForDraggedTypes([.fileURL])
        window.delegate = self
    }

    func update() {
        let model = controller.model
        let connected = model.running && !(model.status?.connected.isEmpty ?? true)
        let name = connected ? "iphone" : "iphone.slash"
        let image = NSImage(systemSymbolName: name, accessibilityDescription: "Tether")
            ?? NSImage(systemSymbolName: "iphone", accessibilityDescription: "Tether")
        image?.isTemplate = true
        item.button?.image = image
        item.button?.appearsDisabled = !connected
        if !menuOpen { rebuild() }
    }

    func menuNeedsUpdate(_ menu: NSMenu) { rebuild() }
    func menuWillOpen(_ menu: NSMenu) { menuOpen = true }
    func menuDidClose(_ menu: NSMenu) { menuOpen = false }

    private func header(_ title: String) -> NSMenuItem {
        if #available(macOS 14.0, *) { return NSMenuItem.sectionHeader(title: title) }
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    /// A title with a smaller grey line under it.
    private func twoLine(_ title: String, _ subtitle: String) -> NSAttributedString {
        let s = NSMutableAttributedString(string: title, attributes: [.font: NSFont.menuFont(ofSize: 0)])
        s.append(NSAttributedString(string: "\n" + subtitle, attributes: [
            .font: NSFont.menuFont(ofSize: NSFont.smallSystemFontSize),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]))
        return s
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let i = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        i.isEnabled = false
        return i
    }

    private func rebuild() {
        menu.removeAllItems()
        let c = controller
        let model = c.model

        guard model.running, let st = model.status else {
            menu.addItem(disabled(model.problem ?? "Starting the Tether service…"))
            menu.addItem(ActionItem("Restart Service", symbol: "arrow.clockwise") { c.restartService() })
            menu.addItem(.separator())
            addFooter()
            return
        }

        if !st.pending.isEmpty {
            menu.addItem(header("Pairing"))
            for p in st.pending {
                let i = NSMenuItem(title: "\(p.name) — code \(shortCode(p.sas))", action: nil, keyEquivalent: "")
                if !p.confirmed {
                    let sub = NSMenu()
                    sub.addItem(ActionItem("Codes Match") { c.answer(p.id, accept: true) })
                    sub.addItem(ActionItem("Reject") { c.answer(p.id, accept: false) })
                    i.submenu = sub
                } else {
                    i.isEnabled = false
                }
                menu.addItem(i)
            }
            menu.addItem(.separator())
        }

        menu.addItem(header("Devices"))
        if st.devices.isEmpty {
            menu.addItem(disabled("No paired devices"))
        }
        for d in st.devices {
            let i = NSMenuItem(title: d.name, action: nil, keyEquivalent: "")
            i.image = NSImage(systemSymbolName: d.symbol, accessibilityDescription: nil)
            i.attributedTitle = twoLine(d.name, d.subtitle)
            let sub = NSMenu()
            if d.connected {
                sub.addItem(ActionItem("Send Files…", symbol: "paperplane") { c.pickAndSend(to: d.id) })
                sub.addItem(ActionItem("Send Clipboard", symbol: "doc.on.clipboard") { c.sendClipboard() })
                sub.addItem(ActionItem("Send Notification…", symbol: "bell") { c.askNotification(to: d) })
                if d.isPhone {
                    sub.addItem(ActionItem("Browse in Finder", symbol: "folder") { c.browse() })
                }
                sub.addItem(.separator())
            }
            sub.addItem(ActionItem("Unpair…") { c.confirmUnpair(d) })
            i.submenu = sub
            menu.addItem(i)
        }

        if !st.discovered.isEmpty {
            menu.addItem(.separator())
            menu.addItem(header("Nearby"))
            for d in st.discovered {
                menu.addItem(ActionItem("Pair with \(d.name)", symbol: "plus.circle") { c.pair(device: d.id) })
            }
        }

        if !model.transfers.isEmpty {
            menu.addItem(.separator())
            menu.addItem(header("Transfers"))
            for t in model.transfers {
                let pct = t.fraction.map { " \(Int($0 * 100))%" } ?? ""
                let i = NSMenuItem(title: "\(t.outgoing ? "↑" : "↓") \(t.name)\(pct)", action: nil, keyEquivalent: "")
                let sub = NSMenu()
                sub.addItem(disabled(t.subtitle))
                sub.addItem(ActionItem("Cancel") { c.run("transfer_cancel", ["id": t.id]) })
                i.submenu = sub
                menu.addItem(i)
            }
        }

        menu.addItem(.separator())
        let browse = ActionItem("Browse Phone in Finder", symbol: "folder") { c.browse() }
        browse.isEnabled = st.connected.contains(where: \.isPhone)
        menu.addItem(browse)
        if st.mounted == true {
            menu.addItem(ActionItem("Eject Phone", symbol: "eject") { c.eject() })
        }
        menu.addItem(ActionItem("Received Files", symbol: "tray.and.arrow.down") { c.openDownloads() })
        let clip = ActionItem("Sync Clipboard") { c.set("clipboard", !st.clipboard) }
        clip.state = st.clipboard ? .on : .off
        menu.addItem(clip)
        menu.addItem(.separator())
        addFooter()
    }

    private func addFooter() {
        menu.addItem(ActionItem("Open Tether…", key: ",") { [weak self] in self?.openWindow?() })
        menu.addItem(ActionItem("Quit Tether", key: "q") { NSApp.terminate(nil) })
    }

    // MARK: drops on the icon

    private func fileURLs(_ info: NSDraggingInfo) -> [URL] {
        info.draggingPasteboard.readObjects(forClasses: [NSURL.self],
                                            options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
    }

    func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        let ok = controller.model.status?.defaultTarget != nil && !fileURLs(sender).isEmpty
        if ok { item.button?.highlight(true) }
        return ok ? .copy : []
    }

    func draggingExited(_ sender: NSDraggingInfo?) { item.button?.highlight(false) }

    func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        item.button?.highlight(false)
        let urls = fileURLs(sender)
        guard !urls.isEmpty else { return false }
        controller.send(urls.map(\.path), to: nil)
        return true
    }
}
