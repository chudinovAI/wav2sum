import Foundation

final class DevWatchPending {
    private var count = 0
    private var failed = false
    var isBusy: Bool { count > 0 || failed }

    func begin() { count += 1 }
    func end(success: Bool = true) {
        count -= 1
        if !success { failed = true }
    }
}

// All state transitions and callbacks run on the main queue.
final class DevWatch {
    static let protocolMarker = "WAV2SUM_DEVWATCH_PROTOCOL_V1"

    private let isBusy: () -> Bool
    private let isReady: () -> Bool
    private let setEnabled: (Bool) -> Void
    private let respond: (String) -> Void
    private let terminate: () -> Void
    private(set) var prepared = false
    private var daemonStartAuthorized = false

    func authorizeDaemonStart() { daemonStartAuthorized = true }
    func takeDaemonStartPermission() -> Bool {
        defer { daemonStartAuthorized = false }
        return daemonStartAuthorized
    }

    init(isBusy: @escaping () -> Bool, isReady: @escaping () -> Bool,
         setEnabled: @escaping (Bool) -> Void, respond: @escaping (String) -> Void,
         terminate: @escaping () -> Void) {
        self.isBusy = isBusy
        self.isReady = isReady
        self.setEnabled = setEnabled
        self.respond = respond
        self.terminate = terminate
    }

    func updateEnabled() {
        setEnabled(isReady() && !prepared)
    }

    func handle(_ command: String) {
        switch command {
        case "protocol":
            respond(Self.protocolMarker)
        case "prepare":
            guard !isBusy() else { respond("busy"); return }
            prepared = true
            updateEnabled()
            respond("ready")
        case "resume":
            prepared = false
            updateEnabled()
            respond("resumed")
        case "exit":
            if prepared && !isBusy() { terminate() }
        default:
            break
        }
    }

    func eof() {
        prepared = false
        updateEnabled()
    }

    func startReading() {
        Thread {
            var buffer = Data()
            while true {
                let data = FileHandle.standardInput.availableData
                if data.isEmpty { break }
                buffer.append(data)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let line = String(data: buffer[buffer.startIndex..<newline], encoding: .utf8)
                    buffer.removeSubrange(buffer.startIndex...newline)
                    if let line { DispatchQueue.main.async { self.handle(line) } }
                }
            }
            DispatchQueue.main.async { self.eof() }
        }.start()
    }
}
