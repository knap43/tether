import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let controller = Controller()
    private var statusMenu: StatusMenu?
    private var window: MainWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = MainWindow(controller: controller)
        let menu = StatusMenu(controller: controller)
        menu.openWindow = { window.show() }
        controller.showWindow = { window.show() }
        controller.windowVisible = { window.isVisible }
        controller.onStatusChange = { menu.update() }
        self.window = window
        statusMenu = menu
        controller.start()

        // “Send to Phone” in Finder's Quick Actions / Services menu.
        NSApp.servicesProvider = self
        NSUpdateDynamicServices()

        // First run: nothing paired yet, so open the window where pairing happens.
        if !FileManager.default.fileExists(atPath: Locations.support.appendingPathComponent("data/peers.json").path) {
            window.show()
        }
    }

    /// Opening the app again (Finder, Spotlight, `tether gui`) shows the window.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        window?.show()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        controller.supervisor.stop()
    }

    /// The Finder service (NSServices in Info.plist).
    @objc func sendToPhone(_ pboard: NSPasteboard, userData: String?, error: AutoreleasingUnsafeMutablePointer<NSString?>) {
        let urls = pboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        controller.send(urls.map(\.path), to: nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
