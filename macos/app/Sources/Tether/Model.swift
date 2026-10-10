import Foundation

struct Battery: Decodable, Equatable {
    let level: Int
    let charging: Bool
}

struct Device: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let type: String?
    let connected: Bool
    let battery: Battery?

    var isPhone: Bool { type == "phone" }
    var symbol: String { isPhone ? "iphone" : "desktopcomputer" }

    var subtitle: String {
        guard connected else { return "Not connected" }
        guard let b = battery else { return "Connected" }
        return "Connected · Battery \(b.level)%" + (b.charging ? " · charging" : "")
    }
}

struct NearbyDevice: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let type: String?
    let addr: String?
}

struct PendingPairing: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let sas: String
    let initiator: Bool
    let confirmed: Bool
}

struct SyncFolder: Decodable, Identifiable, Equatable {
    let id: String
    let path: String
}

struct SavedCommand: Decodable, Identifiable, Equatable {
    let id: String
    let name: String
    let command: String
}

struct Transfer: Decodable, Identifiable, Equatable {
    let id: Int
    let name: String
    let total: Int?
    let done: Int
    let direction: String
    let peer: String
    let peerName: String
    let state: String

    var outgoing: Bool { direction == "out" }
    var fraction: Double? {
        guard let total, total > 0 else { return nil }
        return min(1, Double(done) / Double(total))
    }
    var subtitle: String {
        let who = (outgoing ? "To " : "From ") + peerName
        if let total, total > 0 { return "\(who) · \(human(done)) of \(human(total))" }
        return "\(who) · \(human(done))"
    }
}

struct Share: Identifiable, Equatable {
    let name: String
    let path: String
    var id: String { name }
}

struct NotifApp: Decodable, Identifiable, Equatable {
    let app: String
    let name: String
    var id: String { app }
}

struct WolInterface: Decodable, Identifiable, Equatable {
    let ifname: String
    let port: String?
    let mac: String
    let ip: String
    let wired: Bool
    let enabled: Bool?
    var id: String { ifname }
}

struct Status: Decodable, Equatable {
    let id: String
    let name: String
    let devices: [Device]
    let discovered: [NearbyDevice]
    let pending: [PendingPairing]
    let clipboard: Bool
    let clipboardBackend: String
    let davUrl: String
    let shares: [String: String]
    let sync: [SyncFolder]
    let downloadDir: String
    let commands: [SavedCommand]
    let remoteShell: Bool
    let commandTimeout: Int
    let transfers: [Transfer]
    let notifications: Bool
    let notifMuted: [String]
    let notifApps: [NotifApp]
    let mounted: Bool?
    let mountPath: String?

    var connected: [Device] { devices.filter(\.connected) }

    /// The device a drop on the menu-bar icon or “Send to Phone” goes to: the one connected phone.
    var defaultTarget: Device? {
        let phones = connected.filter(\.isPhone)
        if phones.count == 1 { return phones[0] }
        return connected.count == 1 ? connected[0] : nil
    }

    var sortedShares: [Share] {
        shares.map { Share(name: $0.key, path: $0.value) }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }
}

let snakeDecoder: JSONDecoder = {
    let d = JSONDecoder()
    d.keyDecodingStrategy = .convertFromSnakeCase
    return d
}()

func human(_ n: Int) -> String {
    ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file)
}

func shortCode(_ sas: String) -> String {
    guard sas.count == 6 else { return sas }
    return "\(sas.prefix(3)) \(sas.suffix(3))"
}

/// “not_found: no device is connected” → “No device is connected”.
func friendly(_ error: Any?) -> String {
    let text = (error as? String ?? "Something went wrong").components(separatedBy: ": ").last ?? ""
    return text.prefix(1).uppercased() + text.dropFirst()
}

/// Everything the window and the menu show, kept current from the daemon's events.
final class AppModel: ObservableObject {
    @Published var status: Status?
    @Published var running = false
    @Published var transfers: [Transfer] = []
    @Published var wol: [WolInterface] = []
    @Published var toast: String?
    @Published var problem: String?

    private var toastWork: DispatchWorkItem?

    func show(_ message: String) {
        toast = message
        toastWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.toast = nil }
        toastWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: work)
    }

    func update(_ t: Transfer) {
        if let i = transfers.firstIndex(where: { $0.id == t.id }) {
            transfers[i] = t
        } else if t.state == "active" {
            transfers.append(t)
        }
        if t.state != "active" {
            DispatchQueue.main.asyncAfter(deadline: .now() + (t.state == "done" ? 1.2 : 0)) { [weak self] in
                self?.transfers.removeAll { $0.id == t.id }
            }
        }
    }
}
