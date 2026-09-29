import AppKit
import AVFoundation
import ApplicationServices

final class Hotkey {
    enum Key: String {
        case rightOption = "right_option", rightCommand = "right_command", fn

        var keyCode: Int64 { switch self { case .rightOption: 61; case .rightCommand: 54; case .fn: 63 } }
        var flag: CGEventFlags { switch self { case .rightOption: .maskAlternate; case .rightCommand: .maskCommand; case .fn: .maskSecondaryFn } }
        var label: String { switch self { case .rightOption: "right ⌥"; case .rightCommand: "right ⌘"; case .fn: "fn" } }
    }

    var key: Key = .rightOption
    var onPress: ((_ shift: Bool) -> Void)?
    var onRelease: (() -> Void)?
    var onKeyDown: ((_ keyCode: Int64) -> Void)?

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private var isDown = false

    var isActive: Bool { tap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false }

    func start() -> Bool {
        if isActive { return true }
        guard CGPreflightListenEventAccess() else { return false }
        stop()
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
        tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
            eventsOfInterest: CGEventMask(mask),
            callback: { _, type, event, refcon in
                Unmanaged<Hotkey>.fromOpaque(refcon!).takeUnretainedValue().handle(type, event)
                return Unmanaged.passUnretained(event)
            },
            userInfo: Unmanaged.passUnretained(self).toOpaque())
        guard let tap else { return false }
        source = CFMachPortCreateRunLoopSource(nil, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: tap, enable: true)
        Log.write("hotkey tap active=\(isActive) key=\(key.rawValue)")
        return isActive
    }

    private func stop() {
        if let source { CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes) }
        if let tap { CFMachPortInvalidate(tap) }
        tap = nil
        source = nil
    }

    private func handle(_ type: CGEventType, _ event: CGEvent) {
        switch type {
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
        case .keyDown:
            onKeyDown?(event.getIntegerValueField(.keyboardEventKeycode))
        case .flagsChanged where event.getIntegerValueField(.keyboardEventKeycode) == key.keyCode:
            let pressed = event.flags.contains(key.flag)
            if pressed && !isDown {
                isDown = true
                Log.write("hotkey down")
                onPress?(event.flags.contains(.maskShift))
            } else if !pressed && isDown {
                isDown = false
                Log.write("hotkey up")
                onRelease?()
            }
        default:
            break
        }
    }
}

final class Microphone {
    var onLevel: ((Float) -> Void)?

    private let engine = AVAudioEngine()
    private let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
    private var converter: AVAudioConverter?
    private var samples: [Float] = []
    private let lock = NSLock()

    func start() throws {
        lock.withLock { samples.removeAll(keepingCapacity: true) }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        converter = AVAudioConverter(from: format, to: target)
        input.installTap(onBus: 0, bufferSize: 1600, format: format) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() -> [Float] {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        return lock.withLock { samples }
    }

    private func consume(_ buffer: AVAudioPCMBuffer) {
        guard let converter else { return }
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * target.sampleRate / buffer.format.sampleRate) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var fed = false
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        let chunk = UnsafeBufferPointer(start: out.floatChannelData![0], count: Int(out.frameLength))
        lock.withLock { samples.append(contentsOf: chunk) }
        let rms = sqrt(chunk.reduce(0) { $0 + $1 * $1 } / Float(max(chunk.count, 1)))
        DispatchQueue.main.async { self.onLevel?(rms) }
    }
}

final class HUD {
    enum State { case listening(command: Bool), thinking }

    private let panel: NSPanel
    private let label = NSTextField(labelWithString: "")
    private let meter = LevelView()

    init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 190, height: 40),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        let background = NSVisualEffectView(frame: panel.contentRect(forFrameRect: panel.frame))
        background.material = .hudWindow
        background.state = .active
        background.wantsLayer = true
        background.layer?.cornerRadius = 20
        panel.contentView = background

        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.frame = NSRect(x: 16, y: 11, width: 100, height: 18)
        meter.frame = NSRect(x: 120, y: 10, width: 54, height: 20)
        background.addSubview(label)
        background.addSubview(meter)
    }

    func show(_ state: State) {
        switch state {
        case .listening(let command):
            label.stringValue = command ? "● Command" : "● Listening"
            label.textColor = .systemRed
            meter.isHidden = false
            meter.reset()
        case .thinking:
            label.stringValue = "Typing…"
            label.textColor = .secondaryLabelColor
            meter.isHidden = true
        }
        if let screen = NSScreen.main {
            let frame = screen.visibleFrame
            panel.setFrameOrigin(NSPoint(x: frame.midX - panel.frame.width / 2, y: frame.minY + 60))
        }
        panel.orderFrontRegardless()
    }

    func level(_ rms: Float) { meter.push(rms) }

    func hide() { panel.orderOut(nil) }
}

final class LevelView: NSView {
    private var levels = [CGFloat](repeating: 0, count: 7)

    func reset() {
        levels = levels.map { _ in 0 }
        needsDisplay = true
    }

    func push(_ rms: Float) {
        let db = 20 * log10(max(rms, 1e-5))
        levels.removeFirst()
        levels.append(CGFloat(max(0, min(1, (db + 55) / 45))))
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.systemRed.withAlphaComponent(0.85).setFill()
        let width = bounds.width / CGFloat(levels.count)
        for (i, level) in levels.enumerated() {
            let h = max(3, level * bounds.height)
            let rect = NSRect(x: CGFloat(i) * width + 1.5, y: (bounds.height - h) / 2, width: width - 3, height: h)
            NSBezierPath(roundedRect: rect, xRadius: 1.5, yRadius: 1.5).fill()
        }
    }
}

enum FocusedApp {
    static var bundleID: String? { NSWorkspace.shared.frontmostApplication?.bundleIdentifier }

    static var windowTitle: String? {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return nil }
        let app = AXUIElementCreateApplication(pid)
        guard let window = attribute(app, kAXFocusedWindowAttribute) else { return nil }
        return attribute(window as! AXUIElement, kAXTitleAttribute) as? String
    }

    static func selectedText(completion: @escaping (String?) -> Void) {
        let system = AXUIElementCreateSystemWide()
        if let focused = attribute(system, kAXFocusedUIElementAttribute),
           let text = attribute(focused as! AXUIElement, kAXSelectedTextAttribute) as? String, !text.isEmpty {
            completion(text)
            return
        }
        let pasteboard = NSPasteboard.general
        let saved = Clipboard.save()
        let before = pasteboard.changeCount
        Keyboard.press(8, flags: .maskCommand)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            let text = pasteboard.changeCount != before ? pasteboard.string(forType: .string) : nil
            Clipboard.restore(saved)
            completion(text)
        }
    }

    static var focusedElement: AXUIElement? {
        let system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(system, 0.25)
        guard let focused = attribute(system, kAXFocusedUIElementAttribute) else { return nil }
        let element = focused as! AXUIElement
        AXUIElementSetMessagingTimeout(element, 0.25)
        return element
    }

    static func text(of element: AXUIElement) -> String? {
        guard attribute(element, kAXSubroleAttribute) as? String != kAXSecureTextFieldSubrole as String else { return nil }
        return attribute(element, kAXValueAttribute) as? String
    }

    static func ensureAccessibility(prompt: Bool) -> Bool {
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary)
    }

    private static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }
}

enum Keyboard {
    static func press(_ key: CGKeyCode, flags: CGEventFlags) {
        let source = CGEventSource(stateID: .combinedSessionState)
        for down in [true, false] {
            let event = CGEvent(keyboardEventSource: source, virtualKey: key, keyDown: down)
            event?.flags = flags
            event?.post(tap: .cghidEventTap)
        }
    }

    static func insert(_ text: String) {
        let pasteboard = NSPasteboard.general
        let saved = Clipboard.save()
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        let ours = pasteboard.changeCount
        press(9, flags: .maskCommand)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            if pasteboard.changeCount == ours { Clipboard.restore(saved) }
        }
    }
}

enum Clipboard {
    static func save() -> [NSPasteboardItem] {
        (NSPasteboard.general.pasteboardItems ?? []).map { item in
            let copy = NSPasteboardItem()
            for type in item.types {
                if let data = item.data(forType: type) { copy.setData(data, forType: type) }
            }
            return copy
        }
    }

    static func restore(_ items: [NSPasteboardItem]) {
        NSPasteboard.general.clearContents()
        if !items.isEmpty { NSPasteboard.general.writeObjects(items) }
    }
}

/// Follows a dictated text in its field for a minute and reports how the user edited it, so the daemon can learn words.
final class CorrectionWatcher {
    var onCorrection: ((_ original: String, _ edited: String) -> Void)?

    private var element: AXUIElement?
    private var original = ""
    private var before = ""
    private var after = ""
    private var edited: String?
    private var timer: Timer?
    private var deadline = Date.distantPast

    func track(_ text: String) {
        finish()
        guard let element = FocusedApp.focusedElement else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.begin(element, text) }
    }

    func finish() {
        if timer != nil { read() }
        timer?.invalidate()
        timer = nil
        if let edited, edited != original { onCorrection?(original, edited) }
        element = nil
        edited = nil
    }

    private func begin(_ element: AXUIElement, _ text: String) {
        guard let value = FocusedApp.text(of: element), let range = value.range(of: text, options: .backwards) else { return }
        self.element = element
        original = text
        before = String(value[..<range.lowerBound].suffix(40))
        after = String(value[range.upperBound...].prefix(40))
        deadline = Date().addingTimeInterval(60)
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            if Date() > self.deadline || !self.read() { self.finish() }
        }
    }

    @discardableResult
    private func read() -> Bool {
        guard let element, let focused = FocusedApp.focusedElement, CFEqual(focused, element),
              let value = FocusedApp.text(of: element),
              let region = Self.region(in: value, between: before, and: after)
        else { return false }
        edited = region
        return true
    }

    static func region(in value: String, between before: String, and after: String) -> String? {
        guard let start = before.isEmpty ? value.startIndex : value.range(of: before)?.upperBound,
              let end = after.isEmpty ? value.endIndex : value.range(of: after, range: start..<value.endIndex)?.lowerBound,
              start < end
        else { return nil }
        return String(value[start..<end])
    }
}

final class DictationController {
    var isEnabled = false
    var onBusyChange: ((Bool) -> Void)?

    let hotkey = Hotkey()
    let corrections = CorrectionWatcher()
    private let mic = Microphone()
    private let hud = HUD()
    private let daemon: DaemonClient

    private enum Phase { case idle, recording, handsFree, processing }
    private var phase = Phase.idle
    private var pressedAt = Date.distantPast
    private var lastTapAt = Date.distantPast
    private var context: JSON = [:]
    private var command = false

    private let tapWindow = 0.3
    private let doubleTapWindow = 0.4

    init(daemon: DaemonClient) {
        self.daemon = daemon
        mic.onLevel = { [weak self] in self?.hud.level($0) }
        hotkey.onPress = { [weak self] in self?.pressed(shift: $0) }
        hotkey.onRelease = { [weak self] in self?.released() }
        hotkey.onKeyDown = { [weak self] in self?.keyDown($0) }
    }

    private func pressed(shift: Bool) {
        guard isEnabled else {
            Log.write("hotkey ignored: daemon not ready")
            return
        }
        switch phase {
        case .idle:
            pressedAt = Date()
            let doubleTap = pressedAt.timeIntervalSince(lastTapAt) < doubleTapWindow
            begin(command: shift, handsFree: doubleTap)
        case .handsFree:
            finish()
        case .recording, .processing:
            break
        }
    }

    private func released() {
        guard phase == .recording else { return }
        if Date().timeIntervalSince(pressedAt) < tapWindow {
            lastTapAt = Date()
            cancel()
        } else {
            finish()
        }
    }

    private func keyDown(_ keyCode: Int64) {
        switch phase {
        case .recording: cancel()
        case .handsFree where keyCode == 53: cancel()
        default: break
        }
    }

    private func begin(command: Bool, handsFree: Bool) {
        corrections.finish()
        self.command = command
        context = ["app": FocusedApp.bundleID ?? "", "title": FocusedApp.windowTitle ?? "", "command": command]
        if command {
            FocusedApp.selectedText { [weak self] text in self?.context["selection"] = text ?? "" }
        }
        do {
            try mic.start()
        } catch {
            Log.write("microphone failed: \(error)")
            return
        }
        phase = handsFree ? .handsFree : .recording
        hud.show(.listening(command: command))
        onBusyChange?(true)
    }

    private func cancel() {
        _ = mic.stop()
        phase = .idle
        hud.hide()
        onBusyChange?(false)
    }

    private func finish() {
        let samples = mic.stop()
        guard samples.count > 16_000 / 4 else {
            Log.write("too short: \(samples.count) samples")
            cancel()
            return
        }
        Log.write("dictating \(samples.count / 16) ms from \(context["app"] ?? "")")
        phase = .processing
        hud.show(.thinking)

        var request = context
        request["audio"] = Self.encode(samples)
        daemon.request("dictate", request) { [weak self] reply in
            guard let self else { return }
            self.phase = .idle
            self.hud.hide()
            self.onBusyChange?(false)
            if let text = reply["text"] as? String, !text.isEmpty {
                Log.write("inserting \(text.count) chars, accessibility=\(AXIsProcessTrusted())")
                Keyboard.insert(text)
                if !self.command { self.corrections.track(text) }
            } else if let error = reply["error"] as? String {
                Log.write("dictation failed: \(error)")
                NSSound.beep()
            }
        }
    }

    private static func encode(_ samples: [Float]) -> String {
        var pcm = Data(capacity: samples.count * 2)
        for s in samples {
            var v = Int16(max(-1, min(1, s)) * 32767).littleEndian
            withUnsafeBytes(of: &v) { pcm.append(contentsOf: $0) }
        }
        return pcm.base64EncodedString()
    }
}
