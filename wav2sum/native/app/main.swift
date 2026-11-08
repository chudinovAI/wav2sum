import AppKit
import AVFoundation
import ServiceManagement
import UserNotifications

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, UNUserNotificationCenterDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let daemon = DaemonClient()
    lazy var dictation = DictationController(daemon: daemon)
    private var cloud = false
    private var setupCompleted = false
    private var applyingSettings = false
    private var waitingForExit = false
    lazy var setupWindow = SetupWindow(initialCloud: CommandLine.arguments.contains("--cloud"))
    private lazy var sharingWindow = SharingWindow()
    private var connectionError: String?
    private var subscribed = false
    private var summaryRequestPending = false
    private var recordingRequestPending = false
    private let pendingActions = DevWatchPending()
    private var deferredSettingsStart = false
    private let devWatchMode: Bool
    var devWatch: DevWatch?
    private let pendingCorrections = DevWatchPending()
    private var settingsUncertain = false
    private let effects: Effects

    struct Effects {
        var wanted: (get: () -> Bool, set: (Bool) -> Void) = (
            { UserDefaults.standard.object(forKey: "daemonWanted") as? Bool ?? true },
            { UserDefaults.standard.set($0, forKey: "daemonWanted") })
        var respond: (String) throws -> Void = { try FileHandle.standardOutput.write(contentsOf: Data("\($0)\n".utf8)) }
        var read: (DevWatch) -> Void = { $0.startReading() }
        var terminate: () -> Void = { NSApp.terminate(nil) }
        var configureNotifications: (UNUserNotificationCenterDelegate) -> Void = { UNUserNotificationCenter.current().delegate = $0 }
        var notify: (String, String, [String: String]) -> Void = { title, body, userInfo in
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            content.userInfo = userInfo
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
        }
        var scheduleTick: (@escaping () -> Void) -> Timer? = { tick in
            Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { _ in tick() }
        }
    }

    init(devWatchMode: Bool = ProcessInfo.processInfo.environment["WAV2SUM_DEV_WATCH"] == "1", effects: Effects = Effects()) {
        self.devWatchMode = devWatchMode
        self.effects = effects
        super.init()
    }

    private var status: JSON = [:]
    private var recordingSeconds = 0.0
    private var dictating = false
    private var startedAt: Date?
    private var reconnectTimer: Timer?

    private var daemonWanted: Bool {
        get { effects.wanted.get() }
        set { effects.wanted.set(newValue) }
    }

    private var isConnected: Bool { daemon.isConnected }
    private var isReady: Bool { setupCompleted && !applyingSettings && !waitingForExit && isConnected && subscribed && connectionError == nil && status["state"] as? String == "ready" }
    private var dictationEnabled: Bool { isReady && !(devWatch?.prepared ?? false) }
    private var isRecording: Bool { status["recording"] is JSON }
    private var jobs: [JSON] { status["jobs"] as? [JSON] ?? [] }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if devWatchMode {
            signal(SIGPIPE, SIG_IGN)
            let watch = DevWatch(
                isBusy: { [weak self] in
                    self.map { $0.dictation.busyForHandoff || $0.pendingCorrections.isBusy || $0.pendingActions.isBusy || $0.settingsUncertain || $0.applyingSettings || $0.waitingForExit || $0.deferredSettingsStart } ?? true
                },
                isReady: { [weak self] in self?.isReady ?? false },
                setEnabled: { [weak self] in self?.dictation.isEnabled = $0 },
                respond: { [weak self] response in
                    do { try self?.effects.respond(response) }
                    catch { self?.devWatch?.eof() }
                },
                terminate: effects.terminate
            )
            devWatch = watch
            effects.read(watch)
        }
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu
        setupWindow.onApplying = { [weak self] applying in
            guard let self else { return false }
            if applying && (self.dictation.busyForHandoff || self.pendingCorrections.isBusy
                || self.pendingActions.isBusy || self.applyingSettings || self.waitingForExit) { return false }
            self.applyingSettings = applying
            if applying { self.settingsUncertain = true }
            self.dictation.isEnabled = false
            if !applying { self.dictation.isEnabled = self.dictationEnabled }
            self.updateIcon()
            return true
        }
        setupWindow.onApplied = { [weak self] settings, restart in
            guard let self else { return }
            self.settingsUncertain = false
            self.setupCompleted = settings["completed"] as? Bool == true
            self.cloud = settings["mode"] as? String == "cloud"
            self.daemon.cloud = self.cloud
            self.daemonWanted = true
            self.waitingForExit = restart
            if restart || !self.isConnected {
                self.devWatch?.authorizeDaemonStart()
                if self.devWatch?.prepared == true { self.deferredSettingsStart = true }
            }
            self.applyingSettings = false
            self.startedAt = nil
            self.dictation.isEnabled = self.dictationEnabled
            self.tick()
        }

        daemon.onEvent = { [weak self] in self?.handle(event: $0) }
        daemon.onDisconnect = { [weak self] in self?.disconnected() }
        dictation.corrections.onCorrection = { [weak self] original, edited in
            guard let self else { return }
            self.pendingCorrections.begin()
            self.daemon.request("correction", ["original": original, "edited": edited]) { [weak self] reply in
                let success = reply["ok"] as? Bool == true
                self?.pendingCorrections.end(success: success)
                if !success { Log.write("dev-watch: correction not confirmed; handoff disabled") }
            }
        }
        dictation.onBusyChange = { [weak self] busy in
            self?.dictating = busy
            self?.updateIcon()
        }

        effects.configureNotifications(self)
        setupWindow.refresh { [weak self] settings in
            guard let self else { return }
            guard let settings else {
                self.connectionError = "Settings unavailable"
                self.setupWindow.show()
                self.updateIcon()
                return
            }
            let requestCloud = CommandLine.arguments.contains("--cloud")
            let needsModeChoice = requestCloud && settings["mode"] as? String != "cloud"
            self.setupCompleted = settings["completed"] as? Bool == true && !needsModeChoice
            self.cloud = settings["mode"] as? String == "cloud"
            self.daemon.cloud = self.cloud
            if !self.setupCompleted { self.setupWindow.show(requestCloud: requestCloud) }
            self.tick()
        }
        reconnectTimer = effects.scheduleTick { [weak self] in self?.tick() }
        updateIcon()
    }

    func tick() {
        guard !(devWatch?.prepared ?? false) else { return }
        deferredSettingsStart = false
        guard setupCompleted && !applyingSettings else { updateIcon(); return }
        if waitingForExit {
            guard !isConnected else { return }
            if daemon.connect(path: DaemonProcess.socketPath) {
                daemon.disconnect()
                return
            }
            waitingForExit = false
            connectionError = nil
        }
        _ = dictation.hotkey.start()
        guard !isConnected else { return }
        if daemon.connect(path: DaemonProcess.socketPath) {
            _ = devWatch?.takeDaemonStartPermission()
            startedAt = nil
            daemon.request("subscribe") { [weak self] reply in self?.apply(status: reply) }
        } else if daemonWanted && Date().timeIntervalSince(startedAt ?? .distantPast) > 90,
                  !devWatchMode || devWatch?.takeDaemonStartPermission() == true {
            startedAt = Date()
            DaemonProcess.start(cloud: cloud)
        }
        updateIcon()
    }

    private func disconnected() {
        status = [:]
        subscribed = false
        connectionError = nil
        summaryRequestPending = false
        dictation.disconnected()
        dictation.isEnabled = false
        updateIcon()
    }

    private func apply(status reply: JSON) {
        guard reply["ok"] as? Bool == true else {
            subscribed = false
            connectionError = reply["error"] as? String ?? "Subscription failed"
            dictation.isEnabled = false
            updateIcon()
            return
        }
        guard (reply["cloud"] as? Bool ?? false) == cloud else {
            subscribed = false
            connectionError = "Background process mode mismatch"
            dictation.isEnabled = false
            updateIcon()
            return
        }
        subscribed = true
        connectionError = reply["state"] as? String == "failed" ? reply["error"] as? String : nil
        status = reply
        if let key = Hotkey.Key(rawValue: status["hotkey"] as? String ?? "") { dictation.hotkey.key = key }
        dictation.isEnabled = dictationEnabled
        updateIcon()
    }

    private func handle(event: JSON) {
        guard subscribed && connectionError == nil else { return }
        switch event["event"] as? String {
        case "dictation_preview":
            dictation.receive(event: event)
        case "state":
            status["state"] = event["state"]
            if event["state"] as? String == "failed" {
                connectionError = event["error"] as? String ?? "Selected models or credentials are unavailable; open Settings"
            }
            dictation.isEnabled = dictationEnabled
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
        if !setupCompleted {
            header = "wav2sum — setup required"
        } else if applyingSettings || waitingForExit {
            header = "wav2sum — applying settings…"
        } else if let error = connectionError {
            header = "wav2sum — \(cloud ? "cloud" : "local") · \(error)"
        } else if !isConnected {
            let state = devWatchMode && startedAt == nil ? "off" : (daemonWanted ? "starting…" : "off")
            header = "wav2sum — \(cloud ? "cloud" : "local") · \(state)"
        } else if !isReady {
            header = "wav2sum — \(cloud ? "cloud" : "local") · loading models…"
        } else {
            let megabytes = (status["memory_mb"] as? Int ?? 0) + (status["llm_memory_mb"] as? Int ?? 0)
            header = String(format: "wav2sum — %@ · ready · %.1f GB", cloud ? "cloud" : "local", Double(megabytes) / 1024)
        }
        menu.addItem(disabled(header))

        let showOn = devWatchMode && !isConnected && startedAt == nil
        let toggle = item(daemonWanted && !showOn ? "Turn Off Background Process" : "Turn On Background Process", #selector(toggleDaemon))
        toggle.isEnabled = !(devWatch?.prepared ?? false) && setupCompleted && !applyingSettings && !waitingForExit && connectionError == nil && (!isConnected || subscribed)
        menu.addItem(toggle)
        menu.addItem(.separator())

        if isRecording {
            let source = ((status["recording"] as? JSON)?["auto"] as? String).map { " (\(appName($0)))" } ?? ""
            let record = item("■ Stop Recording\(source)  \(clock(recordingSeconds))", #selector(toggleRecording))
            record.isEnabled = !(devWatch?.prepared ?? false) && !recordingRequestPending
            menu.addItem(record)
        } else {
            let record = item("● Record Call", #selector(toggleRecording))
            record.isEnabled = isReady && !(devWatch?.prepared ?? false) && !recordingRequestPending
            menu.addItem(record)
        }

        let summary = item(cloud ? "Summarize Last Meeting" : "Summarize Last Meeting (requires cloud mode)", #selector(summarizeLastMeeting))
        summary.isEnabled = !(devWatch?.prepared ?? false) && cloud && isReady && !isRecording && !summaryRequestPending
        menu.addItem(summary)

        let key = dictation.hotkey.key.label
        menu.addItem(disabled(isReady ? "Dictation: hold \(key)  ·  double-tap for hands-free" : "Dictation unavailable"))
        if isReady { menu.addItem(disabled("Edit selection: ⇧ + \(key)")) }
        if isReady { menu.addItem(disabled("Dictate in English: ⌃ + \(key)")) }
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
        menu.addItem(item("Settings…", #selector(openSettings)))
        menu.addItem(item("Share Examples…", #selector(openSharing)))
        menu.addItem(item("Advanced Settings (config.toml)", #selector(openConfig)))
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

    private func actionRequest(_ command: String, reply: @escaping (JSON) -> Void = { _ in }) {
        pendingActions.begin()
        daemon.request(command) { [weak self] response in
            guard let self else { return }
            self.pendingActions.end(success: self.isConnected)
            reply(response)
        }
    }

    @objc private func toggleDaemon() {
        guard !(devWatch?.prepared ?? false) else { return }
        if devWatchMode && !isConnected && startedAt == nil { daemonWanted = true }
        else { daemonWanted.toggle() }
        if daemonWanted {
            startedAt = nil
            devWatch?.authorizeDaemonStart()
            tick()
        } else {
            if isConnected { actionRequest("shutdown") }
        }
    }

    @objc private func toggleRecording() {
        guard !(devWatch?.prepared ?? false), (isRecording || isReady), !recordingRequestPending else { return }
        let stopping = isRecording
        recordingRequestPending = true
        actionRequest(stopping ? "record_stop" : "record_start") { [weak self] reply in
            self?.recordingRequestPending = false
            if let error = reply["error"] as? String {
                self?.notify(title: "Recording", body: error)
            } else if stopping, let path = reply["path"] as? String {
                self?.notify(title: "Recording saved", body: path)
            }
        }
    }

    @objc private func summarizeLastMeeting() {
        guard !(devWatch?.prepared ?? false), cloud && isReady && !isRecording && !summaryRequestPending else { return }
        summaryRequestPending = true
        actionRequest("summarize_last_meeting") { [weak self] reply in
            guard let self else { return }
            self.summaryRequestPending = false
            guard reply["ok"] as? Bool == true else {
                self.notify(title: "Couldn't summarize the meeting", body: reply["error"] as? String ?? "Request failed")
                return
            }
            if reply["already_done"] as? Bool == true, let dir = reply["out_dir"] as? String, !dir.isEmpty {
                let entry = NSMenuItem()
                entry.representedObject = dir
                self.openJob(entry)
            } else if let job = reply["job"] as? JSON, let id = job["id"] as? Int {
                if !self.jobs.contains(where: { $0["id"] as? Int == id }) {
                    self.status["jobs"] = self.jobs + [job]
                    self.updateIcon()
                }
            } else {
                self.notify(title: "Couldn't summarize the meeting", body: "Unexpected response from the background process")
            }
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

    @objc private func openSettings() { setupWindow.show() }

    @objc private func openSharing() { sharingWindow.show() }

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
        guard !(devWatch?.prepared ?? false) else { return }
        if isConnected && subscribed && connectionError == nil { actionRequest("shutdown") }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { NSApp.terminate(nil) }
    }

    private func notifyDone(job: JSON) {
        let name = URL(fileURLWithPath: job["audio"] as? String ?? "").deletingPathExtension().lastPathComponent
        notify(title: "Result ready", body: name, userInfo: ["out_dir": job["out_dir"] as? String ?? ""])
    }

    private func notify(title: String, body: String, userInfo: [String: String] = [:]) {
        effects.notify(title, body, userInfo)
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

#if APP_HANDOFF_TESTING
AppHandoffTest.main()
#else
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
#endif
