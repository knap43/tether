import AppKit
import UserNotifications

/// Notification Center: phone notifications (with an inline Reply field when the phone
/// allows it) and Tether's own messages. Replies and dismissals go back to the daemon.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    private enum Category {
        static let reply = "tether.phone.reply"
        static let phone = "tether.phone"
        static let message = "tether.message"
    }

    /// The daemon's reasons, as in org.freedesktop.Notifications.
    private enum Reason {
        static let dismissed = 2
        static let closed = 3
    }

    private let center = UNUserNotificationCenter.current()
    private let daemon: DaemonClient
    private let iconDir = FileManager.default.temporaryDirectory.appendingPathComponent("TetherIcons")
    var onOpenMessage: ((String?) -> Void)?

    init(daemon: DaemonClient) {
        self.daemon = daemon
        super.init()
    }

    func start() {
        center.delegate = self
        let reply = UNTextInputNotificationAction(identifier: "reply", title: "Reply", options: [],
                                                  textInputButtonTitle: "Send", textInputPlaceholder: "Message")
        center.setNotificationCategories([
            UNNotificationCategory(identifier: Category.reply, actions: [reply], intentIdentifiers: [],
                                   options: [.customDismissAction]),
            UNNotificationCategory(identifier: Category.phone, actions: [], intentIdentifiers: [],
                                   options: [.customDismissAction]),
            UNNotificationCategory(identifier: Category.message, actions: [], intentIdentifiers: [], options: []),
        ])
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
        try? FileManager.default.createDirectory(at: iconDir, withIntermediateDirectories: true)
    }

    private func identifier(_ nid: Int) -> String { "phone-\(nid)" }

    /// A phone notification. Posting the same id again replaces it.
    func post(nid: Int, app: String, title: String, body: String, icon: String, actions: [String]) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.subtitle = app
        content.body = body
        content.threadIdentifier = app
        content.categoryIdentifier = actions.contains("reply") ? Category.reply : Category.phone
        content.userInfo = ["nid": nid]
        if !icon.isEmpty, let attachment = attachment(for: icon) {
            content.attachments = [attachment]
        }
        center.add(UNNotificationRequest(identifier: identifier(nid), content: content, trigger: nil))
    }

    /// Notification Center moves attachment files into its own store, so hand it a copy.
    private func attachment(for path: String) -> UNNotificationAttachment? {
        let copy = iconDir.appendingPathComponent(UUID().uuidString + ".png")
        guard (try? FileManager.default.copyItem(atPath: path, toPath: copy.path)) != nil else { return nil }
        return try? UNNotificationAttachment(identifier: "icon", url: copy, options: nil)
    }

    func close(nid: Int) {
        center.removeDeliveredNotifications(withIdentifiers: [identifier(nid)])
        center.removePendingNotificationRequests(withIdentifiers: [identifier(nid)])
    }

    /// Tether's own messages (pairing, received files…); `open` is a path to reveal when clicked.
    func message(_ title: String, _ body: String, open path: String? = nil) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.categoryIdentifier = Category.message
        if let path { content.userInfo = ["open": path] }
        center.add(UNNotificationRequest(identifier: "msg-\(UUID().uuidString)", content: content, trigger: nil))
    }

    // MARK: UNUserNotificationCenterDelegate

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let info = response.notification.request.content.userInfo
        guard let nid = info["nid"] as? Int else {
            if response.actionIdentifier == UNNotificationDefaultActionIdentifier {
                let path = info["open"] as? String
                DispatchQueue.main.async { self.onOpenMessage?(path) }
            }
            done()
            return
        }
        switch response.actionIdentifier {
        case "reply":
            if let text = (response as? UNTextInputNotificationResponse)?.userText, !text.isEmpty {
                daemon.post(["cmd": "desk_action", "nid": nid, "action": "reply", "text": text])
            }
            daemon.post(["cmd": "desk_closed", "nid": nid, "reason": Reason.closed])
        case UNNotificationDismissActionIdentifier, UNNotificationDefaultActionIdentifier:
            // Clearing it here clears it on the phone, as on Linux.
            daemon.post(["cmd": "desk_closed", "nid": nid, "reason": Reason.dismissed])
        default:
            break
        }
        done()
    }
}
