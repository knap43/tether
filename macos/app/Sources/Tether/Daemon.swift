import Foundation

/// Where the installer put the daemon, and where it keeps its socket.
enum Locations {
    static let support = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Tether")
    static let socket = support.appendingPathComponent("run/ctl.sock").path
    static let daemon = support.appendingPathComponent("bin/tether").path
    static let logs = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/Tether")
    static let launchAgentLabel = "dev.tether.daemon"
}

/// A blocking, newline-delimited JSON connection to the daemon's control socket.
final class LineSocket {
    private let fd: Int32
    private var buffer = Data()
    private var scanned = 0

    private init(fd: Int32) { self.fd = fd }

    deinit { Darwin.close(fd) }

    /// Connects to the socket at `path`; nil if nothing is listening there.
    static func connect(_ path: String) -> LineSocket? {
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8) + [0]
        guard bytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else { return nil }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            raw.copyBytes(from: bytes)
        }
        addr.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        let sock = LineSocket(fd: fd)  // closes fd when released
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        return ok == 0 ? sock : nil
    }

    func shutdown() { Darwin.shutdown(fd, SHUT_RDWR) }

    @discardableResult
    func write(_ object: [String: Any]) -> Bool {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return false }
        data.append(0x0A)
        return data.withUnsafeBytes { raw -> Bool in
            var sent = 0
            while sent < raw.count {
                let n = send(fd, raw.baseAddress! + sent, raw.count - sent, 0)
                if n <= 0 { return false }
                sent += n
            }
            return true
        }
    }

    /// The next line, without its newline; nil once the daemon closes the connection.
    func readLine() -> Data? {
        var chunk = [UInt8](repeating: 0, count: 1 << 20)
        while true {
            if let nl = buffer[(buffer.startIndex + scanned)...].firstIndex(of: 0x0A) {
                let line = Data(buffer[buffer.startIndex..<nl])
                buffer = Data(buffer[(nl + 1)...])
                scanned = 0
                return line
            }
            scanned = buffer.count
            let n = recv(fd, &chunk, chunk.count, 0)
            if n <= 0 { return nil }
            buffer.append(chunk, count: n)
        }
    }
}

/// Talks to the daemon: one long-lived subscription for events (and the clipboard and
/// notification traffic), plus a fresh connection per command so slow ones don't block others.
final class DaemonClient {
    typealias Event = (name: String, object: [String: Any], raw: Data)

    var onEvent: ((Event) -> Void)?
    var onRunning: ((Bool) -> Void)?

    private let writeQueue = DispatchQueue(label: "tether.subscription.write")
    private let callQueue = DispatchQueue(label: "tether.calls", attributes: .concurrent)
    private var subscription: LineSocket?
    private(set) var running = false

    func start() {
        let thread = Thread { [weak self] in self?.listen() }
        thread.name = "tether.subscription"
        thread.start()
    }

    private func listen() {
        while true {
            if let sock = LineSocket.connect(Locations.socket), sock.write(["cmd": "subscribe", "app": true]) {
                writeQueue.sync { subscription = sock }
                setRunning(true)
                while let line = sock.readLine() {
                    guard let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
                    let name = object["ev"] as? String ?? ""
                    DispatchQueue.main.async { self.onEvent?((name, object, line)) }
                }
                writeQueue.sync { subscription = nil }
            }
            setRunning(false)
            Thread.sleep(forTimeInterval: 1.5)
        }
    }

    private func setRunning(_ up: Bool) {
        DispatchQueue.main.async {
            guard self.running != up else { return }
            self.running = up
            self.onRunning?(up)
        }
    }

    /// Fire-and-forget messages on the subscription (clipboard, notification actions).
    func post(_ object: [String: Any]) {
        writeQueue.async { self.subscription?.write(object) }
    }

    /// Runs a command; `done` gets the answer on the main thread. Errors come back as {"ok": false, "error": …}.
    func call(_ cmd: String, _ args: [String: Any] = [:], done: (([String: Any]) -> Void)? = nil) {
        callQueue.async {
            var result: [String: Any]
            if let sock = LineSocket.connect(Locations.socket) {
                var request = args
                request["cmd"] = cmd
                if sock.write(request), let line = sock.readLine(),
                   let object = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                    result = object
                } else {
                    result = ["ok": false, "error": "The Tether service closed the connection"]
                }
            } else {
                result = ["ok": false, "error": "The Tether service isn't running"]
            }
            if let done { DispatchQueue.main.async { done(result) } }
        }
    }

    static func isAlive() -> Bool { LineSocket.connect(Locations.socket) != nil }
}

/// Keeps the Python daemon running as a child of the app, so it shares the app's
/// Local Network permission. If a LaunchAgent already runs it, the app leaves it alone.
final class Supervisor {
    private(set) var process: Process?
    private var stopping = false
    private var recentStarts: [Date] = []
    var onProblem: ((String) -> Void)?

    var installed: Bool { FileManager.default.isExecutableFile(atPath: Locations.daemon) }
    var ownsDaemon: Bool { process?.isRunning == true }

    /// Starts the daemon unless one is already answering. Waits a moment first: a
    /// LaunchAgent may be starting it at login too.
    func ensureRunning(after delay: TimeInterval = 0) {
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) {
            if DaemonClient.isAlive() || self.stopping { return }
            DispatchQueue.main.async { self.launch() }
        }
    }

    private func launch() {
        guard process?.isRunning != true, !stopping else { return }
        guard installed else {
            onProblem?("The Tether service isn't installed. Run install.sh from the macos folder.")
            return
        }
        recentStarts = recentStarts.filter { $0.timeIntervalSinceNow > -60 } + [Date()]
        if recentStarts.count > 5 {
            onProblem?("The Tether service keeps stopping. See ~/Library/Logs/Tether/daemon.log.")
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: Locations.daemon)
        p.arguments = ["daemon"]
        try? FileManager.default.createDirectory(at: Locations.logs, withIntermediateDirectories: true)
        let logURL = Locations.logs.appendingPathComponent("daemon.log")
        rotate(logURL)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self, !self.stopping else { return }
                self.ensureRunning(after: 3)
            }
        }
        do {
            try p.run()
            process = p
        } catch {
            onProblem?("Couldn't start the Tether service: \(error.localizedDescription)")
        }
    }

    private func rotate(_ url: URL) {
        let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        guard size > 2_000_000 else { return }
        let old = url.deletingPathExtension().appendingPathExtension("old.log")
        try? FileManager.default.removeItem(at: old)
        try? FileManager.default.moveItem(at: url, to: old)
    }

    func restart() {
        if let p = process, p.isRunning {
            p.terminate()  // the termination handler starts it again
            return
        }
        recentStarts = []
        let agent = "gui/\(getuid())/\(Locations.launchAgentLabel)"
        let kick = Process()
        kick.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        kick.arguments = ["kickstart", "-k", agent]
        kick.terminationHandler = { k in
            if k.terminationStatus != 0 { self.ensureRunning(after: 0.5) }
        }
        do { try kick.run() } catch { ensureRunning() }
    }

    func stop() {
        stopping = true
        if let p = process, p.isRunning {
            p.terminate()
            p.waitUntilExit()
        }
    }
}
