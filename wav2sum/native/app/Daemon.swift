import Foundation

typealias JSON = [String: Any]

final class DaemonClient {
    var onEvent: ((JSON) -> Void)?
    var onDisconnect: (() -> Void)?
    private(set) var isConnected = false
    var cloud = false

    private var fd: Int32 = -1
    private var nextID = 1
    private var pending: [Int: (JSON) -> Void] = [:]
    private let writeQueue = DispatchQueue(label: "wav2sum.socket.write")

    func connect(path: String) -> Bool {
        let s = socket(AF_UNIX, SOCK_STREAM, 0)
        guard s >= 0 else { return false }
        var noSigPipe: Int32 = 1
        guard setsockopt(s, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout.size(ofValue: noSigPipe))) == 0
        else { close(s); return false }
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
            reply?(["ok": false, "error": "the background process is not running"])
            return
        }
        let id = nextID
        nextID += 1
        if let reply { pending[id] = reply }
        var message = params
        message["id"] = id
        message["cmd"] = cmd
        message["cloud"] = cloud
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
            waiting.values.forEach { $0(["ok": false, "error": "connection lost"]) }
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

    private static func command(_ action: String) -> (URL, [String], [String: String])? {
        let bundle = Bundle.main
        var environment = ProcessInfo.processInfo.environment
        if bundle.object(forInfoDictionaryKey: "W2SBundledRuntime") as? Bool == true,
           let resources = bundle.resourceURL {
            let root = resources.appendingPathComponent("python")
            let helpers = bundle.bundleURL.appendingPathComponent("Contents/Helpers")
            let python = root.appendingPathComponent("bin/python3")
            guard FileManager.default.isExecutableFile(atPath: python.path) else { return nil }
            for key in environment.keys.filter({ $0.hasPrefix("PYTHON") || $0.hasPrefix("DYLD_") || $0 == "VIRTUAL_ENV" || $0 == "__PYVENV_LAUNCHER__" }) {
                environment.removeValue(forKey: key)
            }
            environment["PYTHONHOME"] = root.path
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PATH"] = helpers.path + ":/usr/bin:/bin:/usr/sbin:/sbin"
            let capture = helpers.appendingPathComponent("wav2sum-capture")
            if FileManager.default.isExecutableFile(atPath: capture.path) {
                environment["WAV2SUM_CAPTURE_HELPER"] = capture.path
            }
            return (python, ["-m", "wav2sum", action], environment)
        }
        guard let serve = bundle.object(forInfoDictionaryKey: "W2SServeCommand") as? [String],
              serve.count >= 2, serve.last == "serve" else { return nil }
        return (URL(fileURLWithPath: serve[0]), Array(serve.dropFirst().dropLast()) + [action], environment)
    }

    static func start(cloud: Bool, onExit: @escaping () -> Void) {
        guard let (executable, arguments, environment) = command("serve") else {
            Log.write("daemon runtime unavailable")
            DispatchQueue.main.async { onExit() }
            return
        }
        let process = Process()
        process.environment = environment
        if Bundle.main.object(forInfoDictionaryKey: "W2SBundledRuntime") as? Bool == true {
            process.executableURL = executable
            process.arguments = arguments + (cloud ? ["--cloud"] : [])
        } else {
            let shell = String(cString: getpwuid(getuid()).pointee.pw_shell)
            process.executableURL = URL(fileURLWithPath: shell)
            process.arguments = ["-l", "-c", "exec " + ([executable.path] + arguments + (cloud ? ["--cloud"] : [])).map(shellQuote).joined(separator: " ")]
        }
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { _ in DispatchQueue.main.async { onExit() } }
        do { try process.run() } catch {
            Log.write("failed to start daemon")
            DispatchQueue.main.async { onExit() }
        }
    }

    static func setup(_ request: JSON, completion: @escaping (JSON) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            var reply: JSON = ["ok": false, "error": "Setup helper is unavailable."]
            if let (executable, arguments, environment) = command("setup"),
               let input = try? JSONSerialization.data(withJSONObject: request) {
                let process = Process()
                let stdin = Pipe()
                let stdout = Pipe()
                process.executableURL = executable
                process.arguments = arguments
                process.environment = environment
                process.standardInput = stdin
                process.standardOutput = stdout
                process.standardError = FileHandle.nullDevice
                do {
                    try process.run()
                    stdin.fileHandleForWriting.write(input)
                    stdin.fileHandleForWriting.closeFile()
                    let output = stdout.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    if let parsed = (try? JSONSerialization.jsonObject(with: output)) as? JSON {
                        reply = parsed
                    } else {
                        reply = ["ok": false, "error": "Setup helper returned an invalid response."]
                    }
                } catch {
                    reply = ["ok": false, "error": "Could not start the setup helper."]
                }
            }
            if var error = reply["error"] as? String {
                for key in ["deepgram", "openai", "hf_token"] {
                    if let secret = request[key] as? String, !secret.isEmpty {
                        error = error.replacingOccurrences(of: secret, with: "[redacted]")
                    }
                }
                reply["error"] = error
            }
            let result = reply
            DispatchQueue.main.async { completion(result) }
        }
    }

    private static func shellQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}
