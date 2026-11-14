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
    private var callsEnabled = true
    private var applyingSettings = false
    private var waitingForExit = false
    lazy var setupWindow = SetupWindow(initialCloud: CommandLine.arguments.contains("--cloud"))
    lazy var mainWindow = MainWindow()
    private lazy var sharingWindow = SharingWindow()
    private var connectionError: String?
    private var subscribed = false
    private var summaryRequestPending = false
    private enum RecordingRequest { case start, stop }
    private var recordingRequest: RecordingRequest?
    private var recordingRequestPending: Bool { recordingRequest != nil }
    private var recordingStateUnknown = false
    private var recordingMessage = ""
    private var recordingFailed = false
    private var historyCalls: [JSON] = []
    private var historyDictations: [JSON] = []
    private var historyRevision = 0
    private var historyRequestSerial = 0
    private var historyInFlight: Int?
    private var historyDirty = true
    private var historyReadError: String?
    private var historyActionError: String?
    private var historyProcessPending = false
    private var connectionGeneration = 0
    private var dictationMessage = ""
    private var dictationFailed = false
    private var permissionSummary = ""
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
        var showMainWindow: (MainWindow) -> Void = { $0.show() }
        var openURL: (URL) -> Bool = { NSWorkspace.shared.open($0) }
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
    private var launchingID: UUID?
    private var reconnectTimer: Timer?

    private var daemonWanted: Bool {
        get { effects.wanted.get() }
        set { effects.wanted.set(newValue) }
    }

    private var isConnected: Bool { daemon.isConnected }
    private var isReady: Bool { setupCompleted && !applyingSettings && !waitingForExit && !settingsUncertain && isConnected && subscribed && connectionError == nil && status["state"] as? String == "ready" }
    private var dictationEnabled: Bool { isReady && !(devWatch?.prepared ?? false) }
    private var isRecording: Bool { status["recording"] is JSON }
    private var jobs: [JSON] { status["jobs"] as? [JSON] ?? [] }

    private var recordActionTitle: String {
        if let recordingRequest { return recordingRequest == .stop ? "Stopping…" : "Starting…" }
        return isRecording ? "Stop Recording" : "Start Call Recording"
    }
    private var canToggleRecording: Bool {
        callsEnabled && !(devWatch?.prepared ?? false) && !recordingRequestPending && !recordingStateUnknown
            && (isReady || (isRecording && isConnected && subscribed))
    }
    private var backgroundTitle: String {
        let showOn = devWatchMode && !isConnected && startedAt == nil
        return daemonWanted && !showOn ? "Turn Off Background Process" : "Turn On Background Process"
    }
    private var canToggleBackground: Bool {
        !(devWatch?.prepared ?? false) && setupCompleted && !applyingSettings && !waitingForExit
            && launchingID == nil && connectionError == nil && (!isConnected || subscribed)
    }
    private var connectionDescription: String {
        if !setupCompleted { return "Setup required" }
        if applyingSettings || waitingForExit { return "Applying settings…" }
        if settingsUncertain { return "Settings not confirmed; open Settings." }
        if let connectionError { return connectionError }
        if !isConnected {
            return devWatchMode && startedAt == nil ? "Off" : (daemonWanted ? "Starting…" : "Off")
        }
        if !isReady { return "Loading models…" }
        let megabytes = (status["memory_mb"] as? Int ?? 0) + (status["llm_memory_mb"] as? Int ?? 0)
        return String(format: "Ready · %.1f GB", Double(megabytes) / 1024)
    }
    private var recordingDescription: String {
        if recordingStateUnknown { return "Recording status unknown. Reconnect before starting or stopping a call." }
        if isRecording {
            let source = ((status["recording"] as? JSON)?["auto"] as? String)
                .map { "Automatic — \(appName($0))" } ?? "Manual recording"
            return "\(source) · microphone + system audio"
        }
        return "Record your microphone and system audio together."
    }
    private var canProcessHistory: Bool {
        callsEnabled && isReady && !(devWatch?.prepared ?? false) && !historyProcessPending && historyInFlight == nil
            && historyReadError == nil && historyActionError == nil
    }
    private var dictationDescription: String {
        let phase: String
        switch dictation.phase {
        case .idle: phase = dictationEnabled ? "Ready to dictate" : "Dictation unavailable"
        case .starting: phase = "Starting microphone…"
        case .recording: phase = "Listening"
        case .handsFree: phase = "Listening · hands-free"
        case .processing: phase = "Processing dictation…"
        }
        return "\(phase)\nHold \(dictation.hotkey.key.label) to dictate into a text field; double-tap for hands-free. Safe insertion or clipboard fallback."
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        permissionSummary = SetupWindow.permissionSummary()
        mainWindow.onRecord = { [weak self] in self?.toggleRecording() }
        mainWindow.onSettings = { [weak self] in self?.openSettings() }
        mainWindow.onBackground = { [weak self] in self?.toggleDaemon() }
        mainWindow.history.onRefresh = { [weak self] in self?.refreshHistory(clearError: true) }
        mainWindow.history.onProcess = { [weak self] in self?.processHistory($0) }
        mainWindow.history.onOpen = { [weak self] in self?.openHistoryResult($0) }
        dictation.onStateChange = { [weak self] phase in
            guard let self else { return }
            if phase == .starting { self.dictationMessage = ""; self.dictationFailed = false }
            self.updateIcon()
        }
        dictation.onError = { [weak self] error in
            self?.dictationMessage = error
            self?.dictationFailed = true
            self?.updateIcon()
        }
        dictation.onResult = { [weak self] insertionRequested in
            self?.dictationMessage = insertionRequested
                ? "Text copied to the clipboard; insertion requested in the original field."
                : "Text copied to the clipboard. The insertion target changed or is unavailable."
            self?.dictationFailed = false
            self?.updateIcon()
        }
        if devWatchMode {
            signal(SIGPIPE, SIG_IGN)
            let watch = DevWatch(
                isBusy: { [weak self] in
                    self.map { $0.dictation.busyForHandoff || $0.pendingCorrections.isBusy || $0.pendingActions.isBusy || $0.settingsUncertain || $0.applyingSettings || $0.waitingForExit || $0.deferredSettingsStart } ?? true
                },
                isReady: { [weak self] in self?.isReady ?? false },
                setEnabled: { [weak self] enabled in
                    self?.dictation.isEnabled = enabled
                    self?.updateIcon()
                },
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
                || self.pendingActions.isBusy || self.applyingSettings || self.waitingForExit
                || self.isRecording || self.recordingStateUnknown && self.isConnected && self.subscribed
                || self.jobs.contains { ["queued", "running"].contains($0["state"] as? String ?? "") }
                || (self.isConnected && self.subscribed && self.status["state"] as? String == "loading")) { return false }
            if applying && self.launchingID != nil { return false }
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
            if !restart && self.isConnected {
                let generation = self.connectionGeneration
                self.daemon.request(self.subscribed ? "status" : "subscribe") { [weak self] reply in
                    guard let self, generation == self.connectionGeneration else { return }
                    self.apply(status: reply)
                }
            }
            self.tick()
            self.updateIcon()
            self.effects.showMainWindow(self.mainWindow)
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
            self.callsEnabled = settings["calls_enabled"] as? Bool ?? true
            self.daemon.cloud = self.cloud
            if !self.setupCompleted { self.setupWindow.show(requestCloud: requestCloud) }
            self.tick()
            if self.setupCompleted && !self.devWatchMode { self.effects.showMainWindow(self.mainWindow) }
        }
        reconnectTimer = effects.scheduleTick { [weak self] in self?.tick() }
        updateIcon()
    }

    func tick() {
        let permissions = SetupWindow.permissionSummary()
        if permissions != permissionSummary { permissionSummary = permissions; updateIcon() }
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
            launchingID = nil
            let generation = connectionGeneration
            daemon.request("subscribe") { [weak self] reply in
                guard let self, generation == self.connectionGeneration else { return }
                self.apply(status: reply)
            }
        } else if daemonWanted && launchingID == nil && Date().timeIntervalSince(startedAt ?? .distantPast) > 90,
                  !devWatchMode || devWatch?.takeDaemonStartPermission() == true {
            startedAt = Date()
            let id = UUID()
            launchingID = id
            DaemonProcess.start(cloud: cloud) { [weak self] in
                guard let self, self.launchingID == id else { return }
                self.launchingID = nil
                self.updateIcon()
            }
        }
        updateIcon()
    }

    private func disconnected() {
        connectionGeneration += 1
        historyRevision += 1
        historyInFlight = nil
        historyDirty = true
        historyProcessPending = false
        historyReadError = "Background process disconnected"
        if dictation.phase != .idle {
            dictationMessage = "Connection lost; the active dictation was cancelled."
            dictationFailed = true
        }
        status = [:]
        recordingStateUnknown = true
        recordingRequest = nil
        recordingSeconds = 0
        subscribed = false
        connectionError = nil
        summaryRequestPending = false
        dictation.disconnected()
        dictation.isEnabled = false
        renderHistory()
        updateIcon()
    }

    private func apply(status reply: JSON) {
        guard !waitingForExit else { return }
        guard reply["ok"] as? Bool == true else {
            subscribed = false
            connectionError = reply["error"] as? String ?? "Subscription failed"
            dictation.isEnabled = false
            renderHistory()
            updateIcon()
            return
        }
        guard (reply["cloud"] as? Bool ?? false) == cloud else {
            subscribed = false
            connectionError = "Background process mode mismatch; open Settings."
            dictation.isEnabled = false
            renderHistory()
            updateIcon()
            return
        }
        subscribed = true
        connectionError = reply["state"] as? String == "failed"
            ? (reply["error"] as? String ?? "Background process failed; open Settings") : nil
        callsEnabled = reply["calls_enabled"] as? Bool ?? true
        status = reply
        recordingSeconds = (reply["recording"] as? JSON)?["seconds"] as? Double ?? 0
        if recordingStateUnknown { recordingMessage = ""; recordingFailed = false }
        recordingStateUnknown = false
        if let key = Hotkey.Key(rawValue: status["hotkey"] as? String ?? "") { dictation.hotkey.key = key }
        dictation.isEnabled = dictationEnabled
        updateIcon()
        refreshHistory()
    }

    private func handle(event: JSON) {
        guard subscribed else { return }
        switch event["event"] as? String {
        case "dictation_preview":
            if connectionError == nil { dictation.receive(event: event) }
        case "state":
            status["state"] = event["state"]
            if event["state"] as? String == "failed" {
                connectionError = event["error"] as? String ?? "Selected models or credentials are unavailable; open Settings"
            } else if event["state"] as? String == "ready" && (status["cloud"] as? Bool ?? false) == cloud {
                connectionError = nil
            }
            dictation.isEnabled = dictationEnabled
            renderHistory()
        case "recording":
            let active = event["active"] as? Bool == true
            status["recording"] = active ? ["path": event["path"] ?? "", "auto": event["auto"] ?? NSNull()] as JSON : nil
            recordingSeconds = 0
            if !active && !recordingRequestPending, let path = event["path"] as? String {
                recordingMessage = "Recording ended.\n\(path)"
                recordingFailed = false
            }
            if active, let app = event["auto"] as? String {
                notify(title: "Recording \(appName(app)) call", body: "Stops by itself when the call ends.")
            }
            refreshHistory()
        case "level":
            recordingSeconds = event["seconds"] as? Double ?? recordingSeconds
        case "job":
            var list = jobs.filter { ($0["id"] as? Int) != (event["id"] as? Int) }
            list.append(event)
            status["jobs"] = list
            if event["state"] as? String == "done" { notifyDone(job: event) }
            if event["state"] as? String == "failed" { notify(title: "Couldn't process the recording", body: event["error"] as? String ?? "") }
            refreshHistory()
        case "dictation", "history_pruned":
            refreshHistory()
        default:
            break
        }
        updateIcon()
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        menu.addItem(item("Open wav2sum", #selector(openMainWindow)))
        menu.addItem(disabled("wav2sum — \(cloud ? "Cloud" : "Local") · \(connectionDescription)"))

        let toggle = item(backgroundTitle, #selector(toggleDaemon))
        toggle.isEnabled = canToggleBackground
        menu.addItem(toggle)
        menu.addItem(.separator())

        if callsEnabled {
            let record = item(recordActionTitle, #selector(toggleRecording))
            record.isEnabled = canToggleRecording
            menu.addItem(record)
            if isRecording { menu.addItem(disabled("\(clock(recordingSeconds)) · \(recordingDescription)")) }
            let summary = item(cloud ? "Summarize Last Meeting" : "Summarize Last Meeting (requires cloud mode)", #selector(summarizeLastMeeting))
            summary.isEnabled = !(devWatch?.prepared ?? false) && cloud && isReady && !isRecording && !summaryRequestPending
            menu.addItem(summary)
        }

        let key = dictation.hotkey.key.label
        menu.addItem(disabled(isReady ? "Dictation: hold \(key)  ·  double-tap for hands-free" : "Dictation unavailable"))
        if isReady { menu.addItem(disabled("Edit selection: ⇧ + \(key)")) }
        if isReady { menu.addItem(disabled("Dictate in English: ⌃ + \(key)")) }
        for warning in permissionWarnings() {
            menu.addItem(warning)
        }

        let recent = jobs.suffix(5).reversed()
        if callsEnabled && !recent.isEmpty {
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
        if callsEnabled { menu.addItem(item("Open Output Folder", #selector(openOutput))) }
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
        mainWindow.update(.init(
            status: "\(cloud ? "Cloud" : "Local") · \(connectionDescription)",
            callsEnabled: callsEnabled,
            recording: recordingDescription, elapsed: clock(recordingSeconds), recordingActive: isRecording,
            recordTitle: recordActionTitle, recordEnabled: canToggleRecording,
            backgroundTitle: backgroundTitle, backgroundEnabled: canToggleBackground,
            processing: !subscribed ? "Connect to see the automatic processing settings."
                : status["auto_process"] as? Bool == true
                    ? "Stopping saves the audio. Automatic processing is enabled in Settings."
                    : "Stopping saves the audio locally. Automatic processing is off.",
            dictation: dictationDescription, permissions: callsEnabled ? permissionSummary : permissionSummary.replacingOccurrences(of: " System audio access is checked when recording starts.", with: ""),
            dictationMessage: dictationMessage, dictationIsError: dictationFailed,
            message: recordingMessage, messageIsError: recordingFailed))
        mainWindow.history.callsEnabled = callsEnabled
        mainWindow.history.cloud = cloud
        mainWindow.history.setProcessingEnabled(canProcessHistory)
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

    private func refreshHistory(clearError: Bool = false) {
        if clearError { historyReadError = nil; historyActionError = nil }
        historyRevision += 1
        historyDirty = true
        loadHistory()
    }

    private func loadHistory() {
        guard isConnected && subscribed && connectionError == nil else { renderHistory(); return }
        guard historyDirty && historyInFlight == nil else { return }
        historyDirty = false
        historyReadError = nil
        historyRequestSerial += 1
        let token = historyRequestSerial
        let revision = historyRevision
        historyInFlight = token
        renderHistory()
        daemon.request("history") { [weak self] reply in
            guard let self, self.historyInFlight == token else { return }
            self.historyInFlight = nil
            guard self.isConnected && self.subscribed && self.connectionError == nil else { self.renderHistory(); return }
            if revision != self.historyRevision { self.loadHistory(); return }
            if reply["ok"] as? Bool == true,
               let calls = reply["calls"] as? [JSON], let dictations = reply["dictations"] as? [JSON] {
                self.historyCalls = calls
                self.historyDictations = dictations
            } else {
                self.historyReadError = reply["error"] as? String ?? "Could not read history"
            }
            self.renderHistory()
        }
    }

    private func renderHistory() {
        let connection = connectionError ?? (isConnected && subscribed ? nil : "Connect to the background process to read history.")
        mainWindow.history.update(calls: historyCalls, dictations: historyDictations,
                                  canProcess: canProcessHistory, isLoading: historyInFlight != nil,
                                  error: historyReadError ?? historyActionError ?? connection)
    }

    private func processHistory(_ selected: JSON) {
        guard canProcessHistory, let id = selected["id"] as? String,
              let entry = historyCalls.first(where: { $0["id"] as? String == id }),
              let path = entry["path"] as? String,
              ["raw", "failed"].contains(entry["state"] as? String ?? ""),
              entry["out_dir"] as? String == nil else { return }
        historyProcessPending = true
        let generation = connectionGeneration
        updateIcon()
        actionRequest("process", ["path": path]) { [weak self] reply in
            guard let self, generation == self.connectionGeneration else { return }
            self.historyProcessPending = false
            if reply["ok"] as? Bool == true, let job = reply["job"] as? JSON {
                self.status["jobs"] = self.jobs.filter { $0["id"] as? Int != job["id"] as? Int } + [job]
                self.refreshHistory()
            } else {
                self.historyActionError = reply["error"] as? String ?? "Could not process the recording"
                self.renderHistory()
            }
            self.updateIcon()
        }
    }

    private func openHistoryResult(_ path: String) {
        guard isConnected && subscribed && connectionError == nil && historyInFlight == nil
                && historyReadError == nil && historyActionError == nil,
              historyCalls.contains(where: { $0["open_path"] as? String == path }) else { return }
        if !effects.openURL(URL(fileURLWithPath: path)) {
            historyActionError = "Could not open the result"
            renderHistory()
        }
    }

    private func actionRequest(_ command: String, _ params: JSON = [:], reply: @escaping (JSON) -> Void = { _ in }) {
        pendingActions.begin()
        daemon.request(command, params) { [weak self] response in
            guard let self else { return }
            self.pendingActions.end(success: self.isConnected)
            reply(response)
        }
    }

    @objc private func toggleDaemon() {
        guard !(devWatch?.prepared ?? false), launchingID == nil else { return }
        if devWatchMode && !isConnected && startedAt == nil { daemonWanted = true }
        else { daemonWanted.toggle() }
        if daemonWanted {
            startedAt = nil
            devWatch?.authorizeDaemonStart()
            tick()
        } else {
            if isConnected { actionRequest("shutdown") }
        }
        updateIcon()
    }


    @objc private func toggleRecording() {
        guard canToggleRecording else { return }
        let stopping = isRecording
        recordingRequest = stopping ? .stop : .start
        recordingMessage = ""
        recordingFailed = false
        updateIcon()
        actionRequest(stopping ? "record_stop" : "record_start") { [weak self] reply in
            guard let self else { return }
            self.recordingRequest = nil
            if reply["ok"] as? Bool != true {
                self.recordingMessage = reply["error"] as? String ?? "Recording request failed"
                self.recordingFailed = true
                self.notify(title: "Recording", body: self.recordingMessage)
            } else if stopping, let path = reply["path"] as? String {
                self.recordingMessage = "Recording saved locally.\n\(path)"
                self.notify(title: "Recording saved", body: path)
            }
            self.updateIcon()
        }
    }

    @objc private func summarizeLastMeeting() {
        guard callsEnabled, !(devWatch?.prepared ?? false), cloud && isReady && !isRecording && !summaryRequestPending else { return }
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
        _ = effects.openURL(FileManager.default.fileExists(atPath: summary.path) ? summary : URL(fileURLWithPath: dir))
    }

    @objc private func openOutput() {
        guard let dir = status["output_dir"] as? String else {
            recordingMessage = "Connect to locate the configured output folder."
            recordingFailed = true
            updateIcon()
            return
        }
        if !effects.openURL(URL(fileURLWithPath: dir)) {
            recordingMessage = "Could not open the output folder."
            recordingFailed = true
            updateIcon()
        }
    }

    @objc private func openMainWindow() {
        guard setupCompleted else { setupWindow.show(); return }
        updateIcon()
        refreshHistory()
        effects.showMainWindow(mainWindow)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openMainWindow()
        return true
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
