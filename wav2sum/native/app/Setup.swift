import AppKit
import AVFoundation
import UserNotifications

private final class SettingsForm: NSStackView {
    override var isFlipped: Bool { true }
}

final class SetupWindow: NSObject {
    var onApplying: ((Bool) -> Bool)?
    var onApplied: ((JSON, Bool) -> Void)?
    private let window: NSWindow
    private let mode = NSPopUpButton(frame: .zero, pullsDown: false)
    private let callsEnabled = NSButton(checkboxWithTitle: "Enable call recording and processing", target: nil, action: nil)
    private let autoProcess = NSButton(checkboxWithTitle: "Automatically process completed calls", target: nil, action: nil)
    private let cloudConsent = NSButton(checkboxWithTitle: "I consent to cloud processing and provider charges", target: nil, action: nil)
    private let autoConsent = NSButton(checkboxWithTitle: "I separately consent to automatic cloud uploads", target: nil, action: nil)
    private let callModel = NSPopUpButton(frame: .zero, pullsDown: false)
    private let cleanupModel = NSTextField(string: "mlx-community/gemma-4-E4B-it-qat-4bit")
    private let unsafeModel = NSButton(checkboxWithTitle: "Allow Nemotron even on less than 32 GiB / unknown memory", target: nil, action: nil)
    private let deepgram = NSSecureTextField()
    private let openai = NSSecureTextField()
    private let hfToken = NSSecureTextField()
    private let cloudKeysNote = NSTextField(wrappingLabelWithString: "Cloud keys are stored in Keychain only when you press Save. Both are required; leave blank to keep existing keys.")
    private let detail = NSTextField(wrappingLabelWithString: "Loading settings…")
    private let result = NSTextField(wrappingLabelWithString: "")
    private let applyButton = NSButton(title: "Apply Settings", target: nil, action: nil)
    private var loaded = false
    private var working = false
    private var refreshing = false
    private var refreshRequested = false
    private var refreshPreservesDraft = true
    private var refreshCompletions: [(JSON?) -> Void] = []
    private let initialCloud: Bool
    private var localViews: [NSView] = []
    private var cloudViews: [NSView] = []
    private var callViews: [NSView] = []
    private let permissionStatus = NSTextField(wrappingLabelWithString: "")
    private let permissions: () -> String

    static func permissionSummary() -> String {
        let microphone = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized ? "allowed" : "not granted"
        let input = CGPreflightListenEventAccess() ? "allowed" : "not granted"
        let paste = AXIsProcessTrusted() ? "allowed" : "not granted"
        return "Microphone: \(microphone) · Input Monitoring: \(input) · Accessibility: \(paste). System audio access is checked when recording starts."
    }

    init(initialCloud: Bool, permissions: @escaping () -> String = SetupWindow.permissionSummary) {
        self.initialCloud = initialCloud
        self.permissions = permissions
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 740, height: 740), styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        window.title = "wav2sum Settings"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 680, height: 580)
        window.autorecalculatesKeyViewLoop = true

        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        scroll.identifier = NSUserInterfaceItemIdentifier("settings.form")

        let stack = SettingsForm()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 24
        stack.edgeInsets = NSEdgeInsets(top: 24, left: 24, bottom: 24, right: 24)
        stack.translatesAutoresizingMaskIntoConstraints = false
        scroll.documentView = stack
        let clip = scroll.contentView
        NSLayoutConstraint.activate([
            stack.widthAnchor.constraint(equalTo: clip.widthAnchor),
            stack.heightAnchor.constraint(greaterThanOrEqualTo: clip.heightAnchor)
        ])

        let footer = NSView()
        footer.translatesAutoresizingMaskIntoConstraints = false
        footer.identifier = NSUserInterfaceItemIdentifier("settings.footer")
        result.textColor = .secondaryLabelColor
        result.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        result.isSelectable = true
        result.translatesAutoresizingMaskIntoConstraints = false
        footer.addSubview(result)
        applyButton.target = self
        applyButton.action = #selector(apply)
        applyButton.identifier = NSUserInterfaceItemIdentifier("settings.apply")
        applyButton.keyEquivalent = "\r"
        applyButton.translatesAutoresizingMaskIntoConstraints = false
        footer.addSubview(applyButton)

        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        let content = window.contentView!
        content.addSubview(scroll)
        content.addSubview(separator)
        content.addSubview(footer)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: separator.topAnchor),
            separator.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            separator.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            separator.bottomAnchor.constraint(equalTo: footer.topAnchor),
            footer.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            footer.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            footer.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            result.topAnchor.constraint(equalTo: footer.topAnchor, constant: 12),
            result.leadingAnchor.constraint(equalTo: footer.leadingAnchor, constant: 24),
            result.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -24),
            applyButton.topAnchor.constraint(equalTo: result.bottomAnchor, constant: 8),
            applyButton.trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: -24),
            applyButton.bottomAnchor.constraint(equalTo: footer.bottomAnchor, constant: -16)
        ])

        func label(_ text: String, secondary: Bool = false) -> NSTextField {
            let field = NSTextField(wrappingLabelWithString: text)
            if secondary {
                field.textColor = .secondaryLabelColor
                field.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
            }
            return field
        }
        func add(_ view: NSView, to section: NSStackView, fill: Bool = false) {
            section.addArrangedSubview(view)
            if fill { view.widthAnchor.constraint(equalTo: section.widthAnchor).isActive = true }
        }
        func section(_ title: String, identifier: String) -> NSStackView {
            let section = NSStackView()
            section.orientation = .vertical
            section.alignment = .leading
            section.spacing = 8
            section.identifier = NSUserInterfaceItemIdentifier(identifier)
            let heading = label(title)
            heading.font = .systemFont(ofSize: 15, weight: .semibold)
            add(heading, to: section)
            stack.addArrangedSubview(section)
            section.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -48).isActive = true
            return section
        }
        func button(_ title: String, _ action: Selector) -> NSButton { NSButton(title: title, target: self, action: action) }

        let processing = section("Processing & Automation", identifier: "settings.processing")
        add(label("Choose where audio and text are processed. Local mode never creates summaries. Cloud sends audio to Deepgram and text to OpenAI; provider charges may apply. No cloud fallback occurs.", secondary: true), to: processing, fill: true)
        add(label("Processing mode"), to: processing)
        mode.addItems(withTitles: ["Local", "Cloud"])
        mode.target = self; mode.action = #selector(modeChanged)
        mode.setAccessibilityLabel("Processing mode")
        mode.identifier = NSUserInterfaceItemIdentifier("settings.mode")
        autoProcess.identifier = NSUserInterfaceItemIdentifier("settings.autoProcess")
        cloudConsent.identifier = NSUserInterfaceItemIdentifier("settings.cloudConsent")
        autoConsent.identifier = NSUserInterfaceItemIdentifier("settings.autoConsent")
        autoProcess.target = self; autoProcess.action = #selector(automaticChanged)
        add(mode, to: processing)
        callsEnabled.identifier = NSUserInterfaceItemIdentifier("settings.callsEnabled")
        callsEnabled.target = self; callsEnabled.action = #selector(callsChanged)
        add(callsEnabled, to: processing)
        add(cloudConsent, to: processing)
        add(autoProcess, to: processing)
        add(autoConsent, to: processing)
        let automationNote = label("Automatic local processing produces transcripts only. Automatic cloud processing sends call audio and text to providers and can create summaries.", secondary: true)
        add(automationNote, to: processing, fill: true)

        let local = section("Local Models", identifier: "settings.localModels")
        let downloadNote = label("Call models and diarization are downloaded only when requested.", secondary: true)
        let modelLabel = label("Local call model (Qwen is lighter; Nemotron requires substantially more memory):")
        add(downloadNote, to: local, fill: true)
        add(modelLabel, to: local, fill: true)
        callModel.addItems(withTitles: ["Qwen", "Nemotron"])
        callModel.identifier = NSUserInterfaceItemIdentifier("settings.callModel")
        callModel.setAccessibilityLabel("Local call model")
        add(callModel, to: local)
        add(unsafeModel, to: local)
        add(label("Local cleanup model (currently supported model):"), to: local, fill: true)
        cleanupModel.isEditable = false
        cleanupModel.setAccessibilityLabel("Local cleanup model")
        add(cleanupModel, to: local, fill: true)
        detail.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        add(detail, to: local, fill: true)
        hfToken.placeholderString = "Optional Hugging Face token for this download only"
        hfToken.setAccessibilityLabel("Optional Hugging Face token")
        add(hfToken, to: local, fill: true)
        let diarizationNote = label("Speaker diarization for local calls needs pyannote access. Accept its terms yourself on Hugging Face, then enter a token for preparation. Dictation does not need this token.", secondary: true)
        let terms = button("Open pyannote Access / Terms", #selector(openModelTerms))
        add(diarizationNote, to: local, fill: true)
        add(terms, to: local)
        let prepareButton = button("Prepare / Download Local Models", #selector(prepare))
        prepareButton.identifier = NSUserInterfaceItemIdentifier("settings.prepareModels")
        add(prepareButton, to: local)
        localViews = [local]

        let cloud = section("Cloud Credentials", identifier: "settings.cloudCredentials")
        cloudKeysNote.textColor = .secondaryLabelColor
        cloudKeysNote.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        add(cloudKeysNote, to: cloud, fill: true)
        deepgram.placeholderString = "Deepgram API key"
        openai.placeholderString = "OpenAI API key"
        deepgram.setAccessibilityLabel("Deepgram API key")
        openai.setAccessibilityLabel("OpenAI API key")
        for field in [deepgram, openai] { add(field, to: cloud, fill: true) }
        add(button("Save Cloud Keys", #selector(saveKeys)), to: cloud)
        cloudViews = [cloud]

        let permissionSection = section("Permissions", identifier: "settings.permissions")
        add(label("Permissions are optional until needed. Grant Microphone for dictation, Input Monitoring and Accessibility for the hotkey and paste.", secondary: true), to: permissionSection, fill: true)
        let permissionRow = NSStackView(views: [button("Microphone", #selector(microphone)), button("Input Monitoring", #selector(inputMonitoring)), button("Accessibility", #selector(accessibility))])
        permissionRow.spacing = 6
        add(permissionRow, to: permissionSection)
        let systemAudioButton = button("System Audio Settings", #selector(systemAudio))
        let additionalPermissionRow = NSStackView(views: [systemAudioButton, button("Notifications", #selector(notifications))])
        additionalPermissionRow.spacing = 6
        add(additionalPermissionRow, to: permissionSection)
        permissionStatus.textColor = .secondaryLabelColor
        permissionStatus.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        permissionStatus.setAccessibilityLabel("Permission status")
        add(permissionStatus, to: permissionSection, fill: true)
        callViews = [autoProcess, automationNote, downloadNote, modelLabel, callModel, unsafeModel,
                     detail, hfToken, diarizationNote, terms, systemAudioButton]
        window.center()
        updateModeVisibility()
        updatePermissionStatus()
        window.initialFirstResponder = mode
    }

    func show(requestCloud: Bool = false) {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh { [weak self] _ in
            if requestCloud {
                self?.mode.selectItem(at: 1)
                self?.modeChanged()
            }
        }
    }

    func refresh(preserveDraft: Bool = false, completion: ((JSON?) -> Void)? = nil) {
        refreshRequested = true
        refreshPreservesDraft = refreshPreservesDraft && preserveDraft
        if let completion { refreshCompletions.append(completion) }
        startRefreshIfNeeded()
    }

    private func startRefreshIfNeeded() {
        guard refreshRequested && !refreshing && !working else { return }
        refreshing = true
        applyButton.isEnabled = false
        DaemonProcess.setup(["action": "status"]) { [weak self] reply in
            guard let self else { return }
            let preserveDraft = self.refreshPreservesDraft
            let completions = self.refreshCompletions
            self.refreshing = false
            self.refreshRequested = false
            self.refreshPreservesDraft = true
            self.refreshCompletions = []
            guard reply["ok"] as? Bool == true, let settings = reply["settings"] as? JSON else {
                self.loaded = false
                self.result.stringValue = reply["error"] as? String ?? "Could not read settings."
                self.applyButton.isEnabled = false
                completions.forEach { $0(nil) }
                return
            }
            self.loaded = true
            self.applyButton.isEnabled = !self.working
            let completed = settings["completed"] as? Bool == true
            self.cloudKeysNote.stringValue = reply["bundled_cloud_keys"] as? Bool == true
                ? "This private beta includes cloud keys. No key entry is needed. Your own Keychain keys take priority; use the fields below only to replace them. Provider charges are paid by the key owner."
                : "Cloud keys are stored in Keychain only when you press Save. Both are required; leave blank to keep existing keys."
            let recommendation = reply["recommendation"] as? JSON ?? [:]
            if !preserveDraft {
                self.mode.selectItem(at: (completed ? settings["mode"] as? String == "cloud" : self.initialCloud) ? 1 : 0)
                self.callsEnabled.state = (settings["calls_enabled"] as? Bool ?? true) ? .on : .off
                self.cloudConsent.state = settings["cloud_consent"] as? Bool == true ? .on : .off
                self.autoConsent.state = settings["auto_cloud_consent"] as? Bool == true ? .on : .off
                self.autoProcess.state = settings["auto_process"] as? Bool == true ? .on : .off
                let chosenModel = completed ? settings["call_model"] as? String : recommendation["call_model"] as? String
                self.callModel.selectItem(at: chosenModel == "nemotron" ? 1 : 0)
                self.cleanupModel.stringValue = (completed ? settings["cleanup_model"] as? String : recommendation["cleanup_model"] as? String) ?? self.cleanupModel.stringValue
                self.unsafeModel.state = settings["allow_unsafe_nemotron"] as? Bool == true ? .on : .off
            }
            self.updateModeVisibility()
            self.updatePermissionStatus()
            let hardware = reply["hardware"] as? JSON ?? [:]
            let reason = recommendation["reason"] as? String ?? "Recommendation is provisional; benchmark on this machine."
            let chip = hardware["chip"] as? String ?? "Unknown chip"
            let memory = hardware["memory_gib"].map { "\($0) GiB" } ?? "unknown memory"
            let disk = hardware["free_disk_gib"].map { "\($0) GiB free" } ?? "unknown free disk"
            let modelStatus = reply["models"] as? JSON ?? [:]
            let models = [("Qwen", "qwen"), ("Cleanup", "cleanup"), ("Nemotron", "nemotron"), ("Diarization", "diarization")]
                .map { "\($0.0): \(modelStatus[$0.1] as? Bool == true ? "ready" : "not prepared")" }.joined(separator: " · ")
            let recommendedModel = recommendation["call_model"] as? String ?? "unknown"
            self.detail.stringValue = "Hardware: \(chip), \(memory), \(disk). Recommended call model: \(recommendedModel) (provisional). \(reason)\nModel availability: \(models)"
            self.result.stringValue = preserveDraft ? "Model preparation complete." : (completed ? "Settings loaded." : "Choose a mode and apply settings to start.")
            completions.forEach { $0(settings) }
        }
    }

    @objc private func modeChanged() {
        cloudConsent.state = .off; autoConsent.state = .off
        updateModeVisibility()
    }
    @objc private func automaticChanged() {
        autoConsent.state = .off
        updateModeVisibility()
    }
    @objc private func callsChanged() { updateModeVisibility(); updatePermissionStatus() }
    private func updateModeVisibility() {
        let cloud = mode.indexOfSelectedItem == 1
        let calls = callsEnabled.state == .on
        callViews.forEach { $0.isHidden = !calls }
        localViews.forEach { $0.isHidden = cloud }
        cloudViews.forEach { $0.isHidden = !cloud }
        cloudConsent.isHidden = !cloud
        autoConsent.isHidden = !calls || !cloud || autoProcess.state != .on
        autoProcess.title = cloud ? "Automatically transcribe and summarize completed calls" : "Automatically transcribe completed calls"
    }
    private func updatePermissionStatus() {
        let text = permissions()
        permissionStatus.stringValue = callsEnabled.state == .on ? text : text.replacingOccurrences(of: " System audio access is checked when recording starts.", with: "")
    }
    @objc private func openModelTerms() {
        NSWorkspace.shared.open(URL(string: "https://huggingface.co/pyannote/speaker-diarization-community-1")!)
    }

    private var selectedCallModel: String { callModel.indexOfSelectedItem == 1 ? "nemotron" : "qwen" }
    private func begin() -> Bool {
        guard !working && !refreshing else { return false }
        working = true
        applyButton.isEnabled = false
        return true
    }
    private func finish(_ reply: JSON, success: String) {
        working = false
        applyButton.isEnabled = loaded
        result.stringValue = reply["ok"] as? Bool == true ? success : (reply["error"] as? String ?? "Request failed.")
        startRefreshIfNeeded()
    }
    @objc private func apply() {
        guard loaded, !working, !refreshing else { return }
        let cloud = mode.indexOfSelectedItem == 1
        let automatic = autoProcess.state == .on
        guard !cloud || cloudConsent.state == .on else { result.stringValue = "Cloud mode requires explicit consent to provider processing and charges."; return }
        guard !cloud || callsEnabled.state != .on || !automatic || autoConsent.state == .on else { result.stringValue = "Automatic cloud processing requires separate consent."; return }
        guard begin() else { return }
        guard onApplying?(true) != false else {
            finish(["ok": false, "error": "Finish current work or wait for the background process to be ready before applying settings."], success: "")
            return
        }
        let request: JSON = ["action": "configure", "mode": cloud ? "cloud" : "local", "auto_process": automatic, "calls_enabled": callsEnabled.state == .on,
                             "call_model": selectedCallModel, "cleanup_model": cleanupModel.stringValue,
                             "cloud_consent": cloudConsent.state == .on, "auto_cloud_consent": autoConsent.state == .on,
                             "allow_unsafe_nemotron": unsafeModel.state == .on, "completed": true]
        DaemonProcess.setup(request) { [weak self] reply in
            guard let self else { return }
            self.finish(reply, success: "Settings applied.")
            if reply["ok"] as? Bool == true, let settings = reply["settings"] as? JSON {
                self.onApplied?(settings, reply["restart"] as? Bool == true)
            } else { _ = self.onApplying?(false) }
        }
    }
    @objc private func saveKeys() {
        guard !deepgram.stringValue.isEmpty, !openai.stringValue.isEmpty else { result.stringValue = "Enter both keys to save them."; return }
        guard begin() else { return }
        let request: JSON = ["action": "credentials", "deepgram": deepgram.stringValue, "openai": openai.stringValue]
        deepgram.stringValue = ""; openai.stringValue = ""
        DaemonProcess.setup(request) { [weak self] reply in self?.finish(reply, success: "Keys saved to Keychain. Apply Settings to restart an unavailable background process.") }
    }
    @objc private func prepare() {
        guard begin() else { return }
        result.stringValue = "Preparing models…"
        var request: JSON = ["action": "prepare_models", "call_model": selectedCallModel, "cleanup_model": cleanupModel.stringValue, "calls_enabled": callsEnabled.state == .on,
                             "allow_unsafe_nemotron": unsafeModel.state == .on]
        if !hfToken.stringValue.isEmpty { request["hf_token"] = hfToken.stringValue; hfToken.stringValue = "" }
        DaemonProcess.setup(request) { [weak self] reply in
            guard let self else { return }
            self.finish(reply, success: "Model preparation complete.")
            if reply["ok"] as? Bool == true { self.refresh(preserveDraft: true) }
        }
    }
    @objc private func microphone() { AVCaptureDevice.requestAccess(for: .audio) { _ in } }
    @objc private func inputMonitoring() { CGRequestListenEventAccess() }
    @objc private func accessibility() { _ = FocusedApp.ensureAccessibility(prompt: true) }
    @objc private func systemAudio() { NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture")!) }
    @objc private func notifications() { UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in } }
}
