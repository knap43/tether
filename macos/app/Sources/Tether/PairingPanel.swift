import AppKit
import SwiftUI

/// A small floating window showing the pairing code, open until pairing finishes or fails.
final class PairingPanel: ObservableObject {
    let name: String
    let sas: String
    @Published var waiting = false
    private let answer: (Bool) -> Void
    private var window: NSPanel?

    init(name: String, sas: String, answer: @escaping (Bool) -> Void) {
        self.name = name
        self.sas = sas
        self.answer = answer
    }

    func present() {
        if window == nil {
            let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 360, height: 220),
                                styleMask: [.titled, .nonactivatingPanel, .fullSizeContentView],
                                backing: .buffered, defer: false)
            panel.title = "Pair with \(name)"
            panel.titlebarAppearsTransparent = true
            panel.isFloatingPanel = true
            panel.level = .floating
            panel.isReleasedWhenClosed = false
            panel.contentViewController = NSHostingController(rootView: PairingView(panel: self))
            panel.center()
            window = panel
        }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    func respond(_ accept: Bool) { answer(accept) }

    func close() {
        window?.close()
        window = nil
    }
}

private struct PairingView: View {
    @ObservedObject var panel: PairingPanel

    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "iphone.radiowaves.left.and.right")
                .font(.system(size: 30))
                .foregroundStyle(.secondary)
            Text("Pair with \(panel.name)")
                .font(.headline)
            Text(shortCode(panel.sas))
                .font(.system(size: 34, weight: .semibold, design: .monospaced))
                .textSelection(.enabled)
            if panel.waiting {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for \(panel.name) to confirm…").foregroundStyle(.secondary)
                }
            } else {
                Text("Confirm only if \(panel.name) shows the same code.")
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                HStack {
                    Button("Reject", role: .cancel) { panel.respond(false) }
                        .keyboardShortcut(.cancelAction)
                    Button("Codes Match") { panel.respond(true) }
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(24)
        .frame(width: 360)
    }
}
