import AppKit
import AVFoundation
import UserNotifications

final class SetupWindow: NSObject {
    var onApplying: ((Bool) -> Bool)?
    var onApplied: ((JSON, Bool) -> Void)?
    private let window: NSWindow
    private let mode = NSPopUpButton(frame: .zero, pullsDown: false)
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
    private let initialCloud: Bool
    private var localViews: [NSView] = []
    private var cloudViews: [NSView] = []
    private let permissionStatus = NSTextField(wrappingLabelWithString: "")
    private let permissions: () -> String

    init(initialCloud: Bool, permissions: @escaping () -> String = {
        let microphone = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized ? "allowed" : "not granted"
        let input = CGPreflightListenEventAccess() ? "allowed" : "not granted"
        let paste = AXIsProcessTrusted() ? "allowed" : "not granted"
        return "Microphone: \(microphone) · Input Monitoring: \(input) · Accessibility: \(paste). System audio access is checked when recording starts."
    }) {
        self.initialCloud = initialCloud
        self.permissions = permissions
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 660, height: 740), styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        window.title = "wav2sum Settings"
        window.isReleasedWhenClosed = false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 18, left: 20, bottom: 18, right: 20)
        let scroll = NSScrollView(frame: window.contentView!.bounds)
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        window.contentView?.addSubview(scroll)
        scroll.documentView = stack
        stack.translatesAutoresizingMaskIntoConstraints = false
        let clip = scroll.contentView
        NSLayoutConstraint.activate([
            stack.widthAnchor.constraint(equalTo: clip.widthAnchor),
            stack.heightAnchor.constraint(greaterThanOrEqualTo: clip.heightAnchor)
        ])
        func label(_ text: String) -> NSTextField { NSTextField(wrappingLabelWithString: text) }
        func add(_ view: NSView) { stack.addArrangedSubview(view); view.widthAnchor.constraint(lessThanOrEqualTo: stack.widthAnchor, constant: -40).isActive = true }
        func button(_ title: String, _ action: Selector) -> NSButton { NSButton(title: title, target: self, action: action) }
        add(label("Choose where audio and text are processed. Local mode never creates summaries. Cloud sends audio to Deepgram and text to OpenAI; provider charges may apply. No cloud fallback occurs."))
        mode.addItems(withTitles: ["Local", "Cloud"])
        mode.target = self; mode.action = #selector(modeChanged)
        autoProcess.target = self; autoProcess.action = #selector(automaticChanged)
        add(mode)
        add(cloudConsent)
        add(autoProcess)
        add(autoConsent)
        add(label("Automatic local processing produces transcripts only. Automatic cloud processing sends call audio and text to providers and can create summaries."))
        let localStart = stack.arrangedSubviews.count
        add(label("Downloads: Qwen about 4.1 GB, cleanup about 6.8 GB; Nemotron adds about 19 GB. Downloads start only when requested."))
        add(label("Local call model (Qwen is lighter; Nemotron requires substantially more memory):"))
        callModel.addItems(withTitles: ["Qwen", "Nemotron"])
        add(callModel)
        add(unsafeModel)
        add(label("Local cleanup model (currently supported model):"))
        cleanupModel.isEditable = false
        cleanupModel.widthAnchor.constraint(equalToConstant: 500).isActive = true
        add(cleanupModel)
        add(detail)
        hfToken.placeholderString = "Optional Hugging Face token for this download only"
        hfToken.widthAnchor.constraint(equalToConstant: 450).isActive = true
        add(hfToken)
        add(label("Speaker diarization for local calls needs pyannote access. Accept its terms yourself on Hugging Face, then enter a token for preparation. Dictation does not need this token."))
        add(button("Open pyannote Access / Terms", #selector(openModelTerms)))
        add(button("Prepare / Download Local Models", #selector(prepare)))
        localViews = Array(stack.arrangedSubviews[localStart...])
        let cloudStart = stack.arrangedSubviews.count
        add(cloudKeysNote)
        deepgram.placeholderString = "Deepgram API key"
        openai.placeholderString = "OpenAI API key"
        for field in [deepgram, openai] { field.widthAnchor.constraint(equalToConstant: 450).isActive = true; add(field) }
        add(button("Save Cloud Keys", #selector(saveKeys)))
        cloudViews = Array(stack.arrangedSubviews[cloudStart...])
        add(label("Permissions are optional until needed. Grant Microphone for dictation, Input Monitoring and Accessibility for the hotkey and paste, and System Audio Recording for calls."))
        let permissions = NSStackView(views: [button("Microphone", #selector(microphone)), button("Input Monitoring", #selector(inputMonitoring)), button("Accessibility", #selector(accessibility)), button("System Audio Settings", #selector(systemAudio)), button("Notifications", #selector(notifications))])
        permissions.spacing = 6
        add(permissions)
        add(permissionStatus)
        applyButton.target = self
        applyButton.action = #selector(apply)
        add(applyButton)
        add(result)
        window.center()
        updateModeVisibility()
        updatePermissionStatus()
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
        DaemonProcess.setup(["action": "status"]) { [weak self] reply in
            guard let self else { return }
            guard reply["ok"] as? Bool == true, let settings = reply["settings"] as? JSON else {
                self.loaded = false
                self.result.stringValue = reply["error"] as? String ?? "Could not read settings."
                self.applyButton.isEnabled = false
                completion?(nil)
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
            completion?(settings)
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
    private func updateModeVisibility() {
        let cloud = mode.indexOfSelectedItem == 1
        localViews.forEach { $0.isHidden = cloud }
        cloudViews.forEach { $0.isHidden = !cloud }
        cloudConsent.isHidden = !cloud
        autoConsent.isHidden = !cloud || autoProcess.state != .on
        autoProcess.title = cloud ? "Automatically transcribe and summarize completed calls" : "Automatically transcribe completed calls"
    }
    private func updatePermissionStatus() {
        permissionStatus.stringValue = permissions()
    }
    @objc private func openModelTerms() {
        NSWorkspace.shared.open(URL(string: "https://huggingface.co/pyannote/speaker-diarization-community-1")!)
    }

    private var selectedCallModel: String { callModel.indexOfSelectedItem == 1 ? "nemotron" : "qwen" }
    private func begin() -> Bool {
        guard !working else { return false }
        working = true
        applyButton.isEnabled = false
        return true
    }
    private func finish(_ reply: JSON, success: String) {
        working = false
        applyButton.isEnabled = loaded
        result.stringValue = reply["ok"] as? Bool == true ? success : (reply["error"] as? String ?? "Request failed.")
    }
    @objc private func apply() {
        guard loaded, !working else { return }
        let cloud = mode.indexOfSelectedItem == 1
        let automatic = autoProcess.state == .on
        guard !cloud || cloudConsent.state == .on else { result.stringValue = "Cloud mode requires explicit consent to provider processing and charges."; return }
        guard !cloud || !automatic || autoConsent.state == .on else { result.stringValue = "Automatic cloud processing requires separate consent."; return }
        guard begin() else { return }
        guard onApplying?(true) != false else {
            finish(["ok": false, "error": "Finish the current dictation or pending action before applying settings."], success: "")
            return
        }
        let request: JSON = ["action": "configure", "mode": cloud ? "cloud" : "local", "auto_process": automatic,
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
        var request: JSON = ["action": "prepare_models", "call_model": selectedCallModel, "cleanup_model": cleanupModel.stringValue,
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
