import Foundation

typealias JSON = [String: Any]

final class DaemonClient {
    var onEvent: ((JSON) -> Void)?
    var onDisconnect: (() -> Void)?
    private(set) var isConnected = false

    private var fd: Int32 = -1
    private var nextID = 1
    private var pending: [Int: (JSON) -> Void] = [:]
    private let writeQueue = DispatchQueue(label: "wav2sum.socket.write")

    func connect(path: String) -> Bool {
        let s = socket(AF_UNIX, SOCK_STREAM, 0)
        guard s >= 0 else { return false }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else { close(s); return false }
        withUnsafeMutableBytes(of: &addr.sun_path) { dst in
            bytes.withUnsafeBytes { dst.copyMemory(from: $0) }
        }
        let connected = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(s, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        } == 0
        guard connected else { close(s); return false }

        fd = s
        isConnected = true
        Thread { [weak self] in self?.readLoop(s) }.start()
        return true
    }

    func disconnect() {
        if fd >= 0 { shutdown(fd, SHUT_RDWR) }
    }

    func request(_ cmd: String, _ params: JSON = [:], reply: ((JSON) -> Void)? = nil) {
        guard isConnected else {
            reply?(["ok": false, "error": "фоновый процесс не запущен"])
            return
        }
        let id = nextID
        nextID += 1
        if let reply { pending[id] = reply }
        var message = params
        message["id"] = id
        message["cmd"] = cmd
        guard var data = try? JSONSerialization.data(withJSONObject: message) else { return }
        data.append(0x0A)
        let fd = self.fd
        writeQueue.async {
            data.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let n = write(fd, raw.baseAddress! + offset, raw.count - offset)
                    if n <= 0 { return }
                    offset += n
                }
            }
        }
    }

    private func readLoop(_ s: Int32) {
        var buffer = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = read(s, &chunk, chunk.count)
            if n <= 0 { break }
            buffer.append(chunk, count: n)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<newline]
                buffer.removeSubrange(buffer.startIndex...newline)
                if let message = (try? JSONSerialization.jsonObject(with: line)) as? JSON {
                    DispatchQueue.main.async { self.dispatch(message) }
                }
            }
        }
        DispatchQueue.main.async {
            close(s)
            self.fd = -1
            self.isConnected = false
            let waiting = self.pending
            self.pending.removeAll()
            waiting.values.forEach { $0(["ok": false, "error": "соединение потеряно"]) }
            self.onDisconnect?()
        }
    }

    private func dispatch(_ message: JSON) {
        if message["event"] != nil {
            onEvent?(message)
        } else if let id = message["id"] as? Int, let reply = pending.removeValue(forKey: id) {
            reply(message)
        }
    }
}

enum Log {
    static let directory = NSHomeDirectory() + "/Library/Application Support/wav2sum"
    private static let path = directory + "/app.log"
    private static let formatter = ISO8601DateFormatter()

    static func write(_ message: String) {
        let line = "\(formatter.string(from: Date())) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        if let handle = FileHandle(forWritingAtPath: path) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: path, contents: data)
        }
    }
}

enum DaemonProcess {
    static let socketPath = Log.directory + "/daemon.sock"

    static func start() {
        guard let command = Bundle.main.object(forInfoDictionaryKey: "W2SServeCommand") as? [String] else {
            Log.write("W2SServeCommand missing from Info.plist")
            return
        }
        let shell = String(cString: getpwuid(getuid()).pointee.pw_shell)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: shell)
        process.arguments = ["-l", "-c", "exec " + command.map(shellQuote).joined(separator: " ")]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        Log.write("starting daemon via \(shell)")
        do { try process.run() } catch { Log.write("failed to start daemon: \(error)") }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
