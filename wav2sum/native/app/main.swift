import AppKit
import AVFoundation
import ServiceManagement
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let daemon = DaemonClient()
    private lazy var dictation = DictationController(daemon: daemon)

    private var status: JSON = [:]
    private var recordingSeconds = 0.0
    private var dictating = false
    private var startedAt: Date?
    private var reconnectTimer: Timer?

    private var daemonWanted: Bool {
        get { UserDefaults.standard.object(forKey: "daemonWanted") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "daemonWanted") }
    }

    private var isConnected: Bool { daemon.isConnected }
    private var isReady: Bool { isConnected && status["state"] as? String == "ready" }
    private var isRecording: Bool { status["recording"] is JSON }
    private var jobs: [JSON] { status["jobs"] as? [JSON] ?? [] }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu

        daemon.onEvent = { [weak self] in self?.handle(event: $0) }
        daemon.onDisconnect = { [weak self] in self?.disconnected() }
        dictation.corrections.onCorrection = { [weak self] original, edited in
            self?.daemon.request("correction", ["original": original, "edited": edited])
        }
        dictation.onBusyChange = { [weak self] busy in
            self?.dictating = busy
            self?.updateIcon()
        }

        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        _ = FocusedApp.ensureAccessibility(prompt: true)
        CGRequestListenEventAccess()
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
        Log.write("launched: accessibility=\(AXIsProcessTrusted()) inputMonitoring=\(CGPreflightListenEventAccess()) "
            + "microphone=\(AVCaptureDevice.authorizationStatus(for: .audio).rawValue)")

        reconnectTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.tick() }
        tick()
        updateIcon()
    }

    private func tick() {
        _ = dictation.hotkey.start()
        guard !isConnected else { return }
        if daemon.connect(path: DaemonProcess.socketPath) {
            startedAt = nil
            daemon.request("subscribe") { [weak self] reply in self?.apply(status: reply) }
        } else if daemonWanted && Date().timeIntervalSince(startedAt ?? .distantPast) > 90 {
            startedAt = Date()
            DaemonProcess.start()
        }
        updateIcon()
    }

    private func disconnected() {
        status = [:]
        dictation.isEnabled = false
        updateIcon()
    }

    private func apply(status reply: JSON) {
        guard reply["ok"] as? Bool == true else { return }
        status = reply
        if let key = Hotkey.Key(rawValue: status["hotkey"] as? String ?? "") { dictation.hotkey.key = key }
        dictation.isEnabled = isReady
        updateIcon()
    }

    private func handle(event: JSON) {
        switch event["event"] as? String {
        case "state":
            status["state"] = event["state"]
            dictation.isEnabled = isReady
        case "recording":
            let active = event["active"] as? Bool == true
            status["recording"] = active ? ["path": event["path"] ?? "", "auto": event["auto"] ?? NSNull()] as JSON : nil
            recordingSeconds = 0
            if active, let app = event["auto"] as? String {
                notify(title: "Recording \(appName(app)) call", body: "Stops by itself when the call ends.")
            }
        case "level":
            recordingSeconds = event["seconds"] as? Double ?? recordingSeconds
        case "job":
            var list = jobs.filter { ($0["id"] as? Int) != (event["id"] as? Int) }
            list.append(event)
            status["jobs"] = list
            if event["state"] as? String == "done" { notifyDone(job: event) }
            if event["state"] as? String == "failed" { notify(title: "Couldn't process the recording", body: event["error"] as? String ?? "") }
        default:
            break
        }
        updateIcon()
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        let header: String
        if !isConnected {
            header = daemonWanted ? "wav2sum — starting…" : "wav2sum — off"
        } else if !isReady {
            header = "wav2sum — loading models…"
        } else {
            let megabytes = (status["memory_mb"] as? Int ?? 0) + (status["llm_memory_mb"] as? Int ?? 0)
            header = String(format: "wav2sum — ready · %.1f GB", Double(megabytes) / 1024)
        }
        menu.addItem(disabled(header))

        let toggle = item(daemonWanted ? "Turn Off Background Process" : "Turn On Background Process", #selector(toggleDaemon))
        menu.addItem(toggle)
        menu.addItem(.separator())

        if isRecording {
            let source = ((status["recording"] as? JSON)?["auto"] as? String).map { " (\(appName($0)))" } ?? ""
            menu.addItem(item("■ Stop Recording\(source)  \(clock(recordingSeconds))", #selector(toggleRecording)))
        } else {
            let record = item("● Record Call", #selector(toggleRecording))
            record.isEnabled = isReady
            menu.addItem(record)
        }

        let key = dictation.hotkey.key.label
        menu.addItem(disabled(isReady ? "Dictation: hold \(key)  ·  double-tap for hands-free" : "Dictation unavailable"))
        if isReady { menu.addItem(disabled("Edit selection: ⇧ + \(key)")) }
        for warning in permissionWarnings() {
            menu.addItem(warning)
        }

        let recent = jobs.suffix(5).reversed()
        if !recent.isEmpty {
            menu.addItem(.separator())
            menu.addItem(disabled("Recordings"))
            for job in recent {
                let name = URL(fileURLWithPath: job["audio"] as? String ?? "").deletingPathExtension().lastPathComponent
                let state = job["state"] as? String ?? ""
                let suffix = switch state {
                case "done": ""
                case "failed": " — failed"
                case "queued": " — queued"
                default: " — \((job["stage"] as? String ?? "").lowercased())…"
                }
                let entry = item("\(name)\(suffix)", #selector(openJob(_:)))
                entry.representedObject = job["out_dir"] as? String
                entry.isEnabled = state == "done"
                menu.addItem(entry)
            }
        }

        menu.addItem(.separator())
        menu.addItem(item("Open Output Folder", #selector(openOutput)))
        menu.addItem(item("Settings (config.toml)", #selector(openConfig)))
        menu.addItem(item("Logs", #selector(openLog)))
        let login = item("Open at Login", #selector(toggleLoginItem))
        login.state = SMAppService.mainApp.status == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(item("Quit", #selector(quit)))
    }

    private func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        return item
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func updateIcon() {
        guard let button = statusItem.button else { return }
        let busy = !isReady || jobs.contains { ["queued", "running"].contains($0["state"] as? String) }
        if isRecording || dictating {
            let symbol = isRecording ? "record.circle.fill" : "mic.fill"
            button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "wav2sum")?
                .withSymbolConfiguration(.init(paletteColors: [.systemRed]))
        } else {
            button.image = Logo.menuBarImage
        }
        button.appearsDisabled = !isConnected
        let needsPermission = isReady && !dictation.hotkey.isActive
        button.title = isRecording ? " \(clock(recordingSeconds))" : needsPermission ? " !" : (isConnected && busy ? " …" : "")
    }

    @objc private func toggleDaemon() {
        daemonWanted.toggle()
        if daemonWanted {
            tick()
        } else {
            daemon.request("shutdown")
        }
    }

    @objc private func toggleRecording() {
        daemon.request(isRecording ? "record_stop" : "record_start") { [weak self] reply in
            if let error = reply["error"] as? String { self?.notify(title: "Recording", body: error) }
        }
    }

    @objc private func openJob(_ sender: NSMenuItem) {
        guard let dir = sender.representedObject as? String else { return }
        let summary = URL(fileURLWithPath: dir).appendingPathComponent("summary.md")
        NSWorkspace.shared.open(FileManager.default.fileExists(atPath: summary.path) ? summary : URL(fileURLWithPath: dir))
    }

    @objc private func openOutput() {
        let dir = NSString(string: "~/wav2sum/output").expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: dir))
    }

    @objc private func openConfig() {
        let path = NSString(string: "~/.config/wav2sum/config.toml").expandingTildeInPath
        if !FileManager.default.fileExists(atPath: path) {
            try? FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: path, contents: Data())
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: Log.directory))
    }

    private func permissionWarnings() -> [NSMenuItem] {
        var missing: [(String, String)] = []
        if !dictation.hotkey.isActive { missing.append(("Input Monitoring", "Privacy_ListenEvent")) }
        if !AXIsProcessTrusted() { missing.append(("Accessibility", "Privacy_Accessibility")) }
        if AVCaptureDevice.authorizationStatus(for: .audio) != .authorized { missing.append(("Microphone", "Privacy_Microphone")) }
        return missing.map { name, pane in
            let entry = item("⚠︎ No \(name) access — Open Settings", #selector(openPrivacy(_:)))
            entry.representedObject = pane
            return entry
        }
    }

    @objc private func openPrivacy(_ sender: NSMenuItem) {
        guard let pane = sender.representedObject as? String else { return }
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)")!)
    }

    @objc private func toggleLoginItem() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled { try service.unregister() } else { try service.register() }
        } catch {
            notify(title: "Open at Login", body: error.localizedDescription)
        }
    }

    @objc private func quit() {
        if isConnected { daemon.request("shutdown") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
    }

    private func notifyDone(job: JSON) {
        let name = URL(fileURLWithPath: job["audio"] as? String ?? "").deletingPathExtension().lastPathComponent
        notify(title: "Summary ready", body: name, userInfo: ["out_dir": job["out_dir"] as? String ?? ""])
    }

    private func notify(title: String, body: String, userInfo: [String: String] = [:]) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.userInfo = userInfo
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let dir = response.notification.request.content.userInfo["out_dir"] as? String, !dir.isEmpty {
            let item = NSMenuItem()
            item.representedObject = dir
            openJob(item)
        }
        completionHandler()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

private func appName(_ bundleID: String) -> String {
    guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else { return bundleID }
    return FileManager.default.displayName(atPath: url.path).replacingOccurrences(of: ".app", with: "")
}

private func clock(_ seconds: Double) -> String {
    let s = Int(seconds)
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%02d:%02d", s / 60, s % 60)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
