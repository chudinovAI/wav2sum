import AppKit
import AVFoundation
import ApplicationServices
import CoreAudio

final class Hotkey {
    enum Key: String {
        case rightOption = "right_option", rightCommand = "right_command", fn

        var keyCode: Int64 { switch self { case .rightOption: 61; case .rightCommand: 54; case .fn: 63 } }
        var flag: CGEventFlags { switch self { case .rightOption: .maskAlternate; case .rightCommand: .maskCommand; case .fn: .maskSecondaryFn } }
        var label: String { switch self { case .rightOption: "right ⌥"; case .rightCommand: "right ⌘"; case .fn: "fn" } }
    }

    var key: Key = .rightOption
    var onPress: ((_ shift: Bool, _ control: Bool) -> Void)?
    var onRelease: (() -> Void)?
    var onKeyDown: ((_ keyCode: Int64) -> Void)?
    var onMouseDown: ((_ location: CGPoint?) -> Void)?

    private var tap: CFMachPort?
    private var source: CFRunLoopSource?
    private(set) var isDown = false

    var isActive: Bool { tap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false }
    private(set) var releasedAt: CFTimeInterval = 0

    func start() -> Bool {
        if isActive { return true }
        guard CGPreflightListenEventAccess() else { return false }
        stop()
        let mask = (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)
            | (1 << CGEventType.leftMouseDown.rawValue) | (1 << CGEventType.rightMouseDown.rawValue)
            | (1 << CGEventType.otherMouseDown.rawValue)
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
            onMouseDown?(nil) // Input could have been missed while the tap was disabled.
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
        case .keyDown:
            onKeyDown?(event.getIntegerValueField(.keyboardEventKeycode))
        case .leftMouseDown, .rightMouseDown, .otherMouseDown:
            onMouseDown?(event.location)
        case .flagsChanged where event.getIntegerValueField(.keyboardEventKeycode) == key.keyCode:
            updateKey(pressed: event.flags.contains(key.flag), shift: event.flags.contains(.maskShift),
                      control: event.flags.contains(.maskControl))
        default:
            break
        }
    }

    func updateKey(pressed: Bool, shift: Bool, control: Bool) {
        if pressed && !isDown {
            isDown = true
            Log.write("hotkey down")
            onPress?(shift, control)
        } else if !pressed && isDown {
            isDown = false
            releasedAt = CACurrentMediaTime()
            Log.write("hotkey up")
            onRelease?()
        }
    }
}

final class Microphone {
    var onLevel: ((Float) -> Void)?

    private var engine = AVAudioEngine()
    private let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
    private var converter: AVAudioConverter?
    private var samples: [Float] = []
    private var taken = 0
    private let lock = NSLock()
    private var startedAt = Date()
    private var waiting = (buffer: false, sound: false)

    func start() throws {
        lock.withLock {
            samples.removeAll(keepingCapacity: true)
            taken = 0
        }
        startedAt = Date()
        waiting = (true, true)
        // A long-lived engine keeps the input format it first saw; once the mic's sample rate
        // changes, installTap raises an Objective-C exception that `try` can't catch.
        engine = AVAudioEngine()
        let input = engine.inputNode
        if var device = Self.builtInIfBluetooth() {
            Log.write("microphone: Bluetooth input, recording from the built-in one")
            AudioUnitSetProperty(input.audioUnit!, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                 &device, UInt32(MemoryLayout<AudioDeviceID>.size))
        }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "wav2sum", code: 1, userInfo: [NSLocalizedDescriptionKey: "no input device"])
        }
        Log.write("microphone \(AVCaptureDevice.default(for: .audio)?.localizedName ?? "unknown") \(Int(format.sampleRate)) Hz")
        converter = AVAudioConverter(from: format, to: target)
        input.installTap(onBus: 0, bufferSize: 1600, format: format) { [weak self] buffer, _ in
            self?.consume(buffer)
        }
        engine.prepare()
        try engine.start()
    }

    /// CHU-129: AirPods give silence for up to 0.8 s after the press, the Mac's microphone for 0.19 s,
    /// so with a Bluetooth input we record from the built-in microphone, as Wispr Flow does.
    private static func builtInIfBluetooth() -> AudioDeviceID? {
        let system = AudioObjectID(kAudioObjectSystemObject)
        let bluetooth = [kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE]
        guard let input = property(system, kAudioHardwarePropertyDefaultInputDevice).first,
              property(input, kAudioDevicePropertyTransportType).contains(where: bluetooth.contains)
        else { return nil }
        return property(system, kAudioHardwarePropertyDevices).first {
            property($0, kAudioDevicePropertyTransportType) == [kAudioDeviceTransportTypeBuiltIn]
                && !property($0, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput).isEmpty
        }
    }

    /// A CoreAudio property made of UInt32 values: device ids, stream ids or the transport type.
    private static func property(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector,
                                 _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [UInt32] {
        var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr else { return [] }
        var values = [UInt32](repeating: 0, count: Int(size) / MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &values) == noErr else { return [] }
        return values
    }

    func stop() -> [Float] {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        return lock.withLock { samples }
    }

    /// The samples recorded since the last call.
    func take() -> [Float] {
        lock.withLock {
            defer { taken = samples.count }
            return Array(samples[taken...])
        }
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
        // CHU-129: how long the first word waits for the microphone, e.g. AirPods switching to headset mode.
        let ms = Int(Date().timeIntervalSince(startedAt) * 1000)
        if waiting.buffer {
            waiting.buffer = false
            Log.write("microphone first buffer after \(ms) ms")
        }
        if waiting.sound && rms > 0 {
            waiting.sound = false
            Log.write("microphone first sound after \(ms) ms")
        }
        DispatchQueue.main.async { self.onLevel?(rms) }
    }
}

private final class HUDBackground: NSVisualEffectView {
    var onDrag: ((NSPoint, NSPoint) -> Void)?
    private var dragOffset = NSPoint.zero

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        let point = window.convertPoint(toScreen: event.locationInWindow)
        dragOffset = NSPoint(x: point.x - window.frame.minX, y: point.y - window.frame.minY)
    }

    override func mouseDragged(with event: NSEvent) {
        guard let window else { return }
        onDrag?(window.convertPoint(toScreen: event.locationInWindow), dragOffset)
    }
}

final class HUD {
    enum State { case listening(command: Bool, translate: Bool), thinking, success }

    private let panel: NSPanel
    private let label = NSTextField(labelWithString: "")
    private let meter = LevelView()
    private let check = CheckmarkView()
    private let scroll = NSScrollView()
    private let preview = NSTextView()
    private var generation = 0
    private var positioned = false
    private var localOrigin: NSPoint?
    private var screenObserver: NSObjectProtocol?
    var cloud = false

    init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 320, height: 80),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.animationBehavior = .none
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.title = "Dictation preview"

        let background = HUDBackground(frame: panel.contentRect(forFrameRect: panel.frame))
        background.material = .hudWindow
        background.state = .active
        background.wantsLayer = true
        background.layer?.cornerRadius = 20
        panel.contentView = background
        background.autoresizingMask = [.width, .height]
        background.toolTip = "Drag to move"
        background.onDrag = { [weak self] point, offset in
            self?.move(to: point, offset: offset)
        }

        scroll.drawsBackground = false
        scroll.hasVerticalScroller = false
        scroll.borderType = .noBorder
        preview.isEditable = false
        preview.isSelectable = false
        preview.drawsBackground = false
        preview.font = .systemFont(ofSize: 14)
        preview.textColor = .labelColor
        preview.textContainerInset = .zero
        preview.textContainer?.lineFragmentPadding = 0
        preview.isVerticallyResizable = true
        preview.isHorizontallyResizable = false
        preview.textContainer?.widthTracksTextView = true
        preview.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        preview.textContainer?.containerSize = NSSize(width: 288, height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = preview
        scroll.isHidden = true
        background.addSubview(scroll)

        label.font = .systemFont(ofSize: 13, weight: .medium)
        label.textColor = .labelColor
        label.frame = NSRect(x: 16, y: 11, width: 100, height: 18)
        meter.frame = NSRect(x: 120, y: 10, width: 54, height: 20)
        check.frame = NSRect(x: 150, y: 10, width: 20, height: 20)
        background.addSubview(label)
        background.addSubview(meter)
        background.addSubview(check)

        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.positioned || self.cloud else { return }
            self.layout()
        }
    }

    deinit {
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
    }

    func show(_ state: State) {
        generation += 1
        panel.alphaValue = 1
        panel.ignoresMouseEvents = cloud
        check.isHidden = true
        switch state {
        case .listening(let command, let translate):
            clearPreview()
            label.stringValue = command ? "● Command" : translate ? "● English" : "● Listening"
            label.textColor = .systemRed
            meter.isHidden = false
            meter.reset()
        case .thinking:
            label.stringValue = "Typing…"
            label.textColor = .secondaryLabelColor
            meter.isHidden = true
        case .success:
            clearPreview()
            label.stringValue = "Success"
            label.textColor = .systemGreen
            meter.isHidden = true
            check.isHidden = false
            check.animate()
            fadeOut(after: 1.0)
        }
        layout()
        if !scroll.isHidden { preview.scrollToEndOfDocument(nil) }
        panel.orderFrontRegardless()
    }

    func level(_ rms: Float) { meter.push(rms) }

    func contains(mouseLocation: CGPoint?) -> Bool {
        guard panel.isVisible, let mouseLocation, let primary = NSScreen.screens.first else { return false }
        let point = NSPoint(x: mouseLocation.x, y: primary.frame.maxY - mouseLocation.y)
        return NSWindow.windowNumber(at: point, belowWindowWithWindowNumber: 0) == panel.windowNumber
    }

    func setPreview(_ text: String) {
        preview.string = text
        scroll.isHidden = text.isEmpty
        if let container = preview.textContainer { preview.layoutManager?.ensureLayout(for: container) }
        preview.scrollToEndOfDocument(nil)
    }

    private func clearPreview() {
        preview.string = ""
        scroll.isHidden = true
    }

    private func move(to point: NSPoint, offset: NSPoint) {
        guard let screen = NSScreen.screens.first(where: { $0.frame.contains(point) })
            ?? panel.screen ?? NSScreen.main ?? NSScreen.screens.first else {
            return
        }
        layout(on: screen, origin: NSPoint(x: point.x - offset.x, y: point.y - offset.y))
    }

    private func layout(on selectedScreen: NSScreen? = nil, origin: NSPoint? = nil) {
        let localScreen = localOrigin.flatMap { point in NSScreen.screens.first { $0.frame.contains(point) } }
        guard let screen = selectedScreen ?? (!cloud && positioned ? localScreen : NSScreen.main)
            ?? NSScreen.main ?? NSScreen.screens.first else { return }
        let visible = screen.visibleFrame
        let width = min(cloud ? 190 : 320, visible.width)
        let height = min(cloud ? 40 : 80, visible.height)
        let initial = cloud
            ? NSPoint(x: visible.midX - width / 2, y: visible.minY + 60)
            : NSPoint(x: visible.maxX - width - 16, y: visible.minY + 16)
        let desired = cloud ? initial : (origin ?? localOrigin ?? initial)
        let x = max(visible.minX, min(desired.x, visible.maxX - width))
        let y = max(visible.minY, min(desired.y, visible.maxY - height))
        panel.setFrame(NSRect(x: x, y: y, width: width, height: height), display: true)
        if !cloud {
            positioned = true
            localOrigin = NSPoint(x: x, y: y)
        }
        label.frame = NSRect(x: 16, y: height - 29, width: 100, height: 18)
        meter.frame = NSRect(x: width - 70, y: height - 30, width: 54, height: 20)
        check.frame = NSRect(x: width - 40, y: height - 30, width: 20, height: 20)
        scroll.frame = NSRect(x: 16, y: 12, width: max(0, width - 32), height: max(0, height - 50))
        preview.setFrameSize(NSSize(width: max(0, width - 32), height: max(preview.frame.height, height - 50)))
    }

    func hide() {
        generation += 1
        clearPreview()
        panel.orderOut(nil)
    }

    /// A newer show() or hide() bumps the generation and cancels a pending fade.
    private func fadeOut(after delay: TimeInterval) {
        let current = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, self.generation == current else { return }
            NSAnimationContext.runAnimationGroup({ context in
                context.duration = 0.3
                self.panel.animator().alphaValue = 0
            }, completionHandler: { [weak self] in
                guard let self, self.generation == current else { return }
                self.panel.orderOut(nil)
                self.panel.alphaValue = 1
            })
        }
    }
}

/// A green circle with a checkmark that draws itself in.
final class CheckmarkView: NSView {
    private let circle = CAShapeLayer()
    private let tick = CAShapeLayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        circle.fillColor = NSColor.systemGreen.cgColor
        tick.fillColor = nil
        tick.strokeColor = NSColor.white.cgColor
        tick.lineWidth = 2
        tick.lineCap = .round
        tick.lineJoin = .round
        layer?.addSublayer(circle)
        layer?.addSublayer(tick)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func layout() {
        super.layout()
        let b = bounds
        circle.frame = b
        circle.path = CGPath(ellipseIn: b, transform: nil)
        let path = CGMutablePath()
        path.move(to: CGPoint(x: b.width * 0.28, y: b.height * 0.52))
        path.addLine(to: CGPoint(x: b.width * 0.44, y: b.height * 0.36))
        path.addLine(to: CGPoint(x: b.width * 0.72, y: b.height * 0.66))
        tick.frame = b
        tick.path = path
    }

    func animate() {
        layoutSubtreeIfNeeded()
        let pop = CAKeyframeAnimation(keyPath: "transform.scale")
        pop.values = [0.3, 1.15, 1.0]
        pop.keyTimes = [0, 0.6, 1]
        pop.duration = 0.3
        circle.add(pop, forKey: "pop")

        let draw = CABasicAnimation(keyPath: "strokeEnd")
        draw.fromValue = 0
        draw.toValue = 1
        draw.beginTime = CACurrentMediaTime() + 0.15
        draw.duration = 0.25
        draw.fillMode = .backwards
        draw.timingFunction = CAMediaTimingFunction(name: .easeOut)
        tick.add(draw, forKey: "draw")
    }
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

    /// A few characters on each side of the cursor (or of the selection that the dictation replaces), so the daemon can
    /// fit the dictation into the sentence. Nil when the field does not expose its text.
    static func textAroundCursor() -> (before: String, after: String)? {
        guard let element = focusedElement, let value = text(of: element),
              let range = attribute(element, kAXSelectedTextRangeAttribute) else { return nil }
        var selected = CFRange()
        guard AXValueGetValue(range as! AXValue, .cfRange, &selected), selected.location >= 0 else { return nil }
        let field = value as NSString
        let start = min(selected.location, field.length), end = min(start + selected.length, field.length)
        let before = field.substring(with: NSRange(location: max(0, start - 20), length: min(20, start)))
        let after = field.substring(with: NSRange(location: end, length: min(20, field.length - end)))
        return (before, after)
    }

    /// Electron apps (Claude, Slack, VS Code) expose their text fields to Accessibility only when asked to.
    static func exposeTextFields() {
        guard let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return }
        AXUIElementSetAttributeValue(AXUIElementCreateApplication(pid), "AXManualAccessibility" as CFString, kCFBooleanTrue)
    }

    static func ensureAccessibility(prompt: Bool) -> Bool {
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary)
    }

    static func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }
}

/// Once the target changes, returning to it does not make an in-flight paste safe again.
final class PasteTarget {
    private let pid: pid_t?
    private let window: AXUIElement?
    private let element: AXUIElement?
    private let value: String?
    private let selection: CFTypeRef?
    private var changed = false
    private var timer: Timer?
    private var activation: NSObjectProtocol?

    init() {
        let processID = NSWorkspace.shared.frontmostApplication?.processIdentifier
        pid = processID
        if let processID, let focusedWindow = FocusedApp.attribute(AXUIElementCreateApplication(processID), kAXFocusedWindowAttribute) {
            window = (focusedWindow as! AXUIElement)
        } else {
            window = nil
        }
        let focused = FocusedApp.focusedElement
        element = focused
        value = focused.flatMap { FocusedApp.text(of: $0) }
        selection = focused.flatMap { FocusedApp.attribute($0, kAXSelectedTextRangeAttribute) }
        activation = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didActivateApplicationNotification, object: nil, queue: .main
        ) { [weak self] notification in
            guard let self,
                  let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            else { return }
            if app.processIdentifier != self.pid { self.changed = true }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
            self?.check()
        }
    }

    func invalidate() { changed = true }

    func stop() {
        timer?.invalidate()
        timer = nil
        if let activation { NSWorkspace.shared.notificationCenter.removeObserver(activation) }
        activation = nil
    }

    func canPaste(inputMonitoring: Bool) -> Bool {
        check()
        // A generic app/window AX element cannot identify the field. Text may be unavailable
        // even for a real field; in that case monitored input plus stable identity is required.
        let role = element.flatMap { FocusedApp.attribute($0, kAXRoleAttribute) as? String }
        return !changed && inputMonitoring && (role == kAXTextFieldRole as String || role == kAXTextAreaRole as String)
    }

    func observeInsertion(_ text: String, session: String, releasedAt: CFTimeInterval,
                          readyAt: CFTimeInterval, sentAt: CFTimeInterval, completion: @escaping () -> Void) {
        guard let element, let value, let selection,
              CFGetTypeID(selection) == AXValueGetTypeID(),
              AXValueGetType(selection as! AXValue) == .cfRange else {
            Log.write("dictation insertion session=\(session) unavailable: target text/selection")
            completion()
            return
        }
        var range = CFRange()
        let field = value as NSString
        guard AXValueGetValue(selection as! AXValue, .cfRange, &range),
              range.location >= 0, range.location <= field.length,
              range.length >= 0, range.length <= field.length - range.location else {
            Log.write("dictation insertion session=\(session) unavailable: invalid selection")
            completion()
            return
        }
        let expected = field.replacingCharacters(in: NSRange(location: range.location, length: range.length), with: text)
        guard expected != value else {
            Log.write("dictation insertion session=\(session) unavailable: unchanged value")
            completion()
            return
        }
        var lower = sentAt
        Timer.scheduledTimer(withTimeInterval: 0.025, repeats: true) { timer in
            let started = CACurrentMediaTime()
            guard started - sentAt < 2 else {
                timer.invalidate()
                Log.write("dictation insertion session=\(session) unconfirmed: timeout")
                completion()
                return
            }
            let current = FocusedApp.text(of: element)
            let observed = CACurrentMediaTime()
            if current == expected {
                timer.invalidate()
                let ready = Int((readyAt - releasedAt) * 1000)
                let sent = Int((sentAt - releasedAt) * 1000)
                let first = Int((lower - releasedAt) * 1000)
                let last = Int(ceil((observed - releasedAt) * 1000))
                Log.write("dictation insertion session=\(session) ready_ms=\(ready) sent_ms=\(sent) observed_ms=\(first)..\(last)")
                completion()
            } else if current == nil {
                timer.invalidate()
                Log.write("dictation insertion session=\(session) unavailable: target text")
                completion()
            } else {
                lower = started
            }
        }
    }

    private func check() {
        guard !changed else { return }
        guard let pid, NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
              let element, let current = FocusedApp.focusedElement, CFEqual(element, current)
        else { changed = true; return }
        if let window {
            guard let currentWindow = FocusedApp.attribute(AXUIElementCreateApplication(pid), kAXFocusedWindowAttribute),
                  CFEqual(window, currentWindow) else { changed = true; return }
        }
        if FocusedApp.text(of: current) != value { changed = true; return }
        let currentSelection = FocusedApp.attribute(current, kAXSelectedTextRangeAttribute)
        switch (selection, currentSelection) {
        case (nil, nil): break
        case let (old?, new?) where CFEqual(old, new): break
        default: changed = true
        }
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

}

enum Clipboard {
    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

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

/// Waits for a dictated text to be pasted somewhere, follows it in that field for three minutes and reports how the
/// user edited it, so the daemon can learn words.
final class CorrectionWatcher {
    var onCorrection: ((_ original: String, _ edited: String) -> Void)?

    private var element: AXUIElement?
    private var original = ""
    private var before = ""
    private var after = ""
    private var edited: String?
    private var timer: Timer?
    private var waiting: Timer?
    private var deadline = Date.distantPast
    private var tracking = false
    private var finishing = false
    var busyForHandoff: Bool { tracking || finishing }

    private let prepare: () -> String
    private let focusedElement: () -> AXUIElement?
    private let text: (AXUIElement) -> String?

    init(prepare: @escaping () -> String = { FocusedApp.exposeTextFields(); return FocusedApp.bundleID ?? "" },
         focusedElement: @escaping () -> AXUIElement? = { FocusedApp.focusedElement },
         text: @escaping (AXUIElement) -> String? = FocusedApp.text) {
        self.prepare = prepare
        self.focusedElement = focusedElement
        self.text = text
    }

    func track(_ text: String) {
        finish()
        tracking = true
        let app = prepare()
        let deadline = Date().addingTimeInterval(30)
        waiting = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] timer in
            guard let self else { return timer.invalidate() }
            if Date() > deadline {
                Log.write("corrections: pasted text not found in \(app)")
                return self.finish()
            }
            guard let element = self.focusedElement(), self.text(element)?.contains(text) == true else { return }
            self.waiting?.invalidate()
            self.waiting = nil
            Log.write("corrections: following the text in \(app)")
            if !self.begin(element, text) { self.finish() }
        }
    }

    func finish() {
        finishing = true
        defer { tracking = false; finishing = false }
        waiting?.invalidate()
        waiting = nil
        if timer != nil { read() }
        timer?.invalidate()
        timer = nil
        if let edited, edited != original {
            Log.write("corrections: sending an edit of \(edited.count) chars")
            onCorrection?(original, edited)
        }
        element = nil
        edited = nil
    }

    private func begin(_ element: AXUIElement, _ text: String) -> Bool {
        guard let value = self.text(element), let range = value.range(of: text, options: .backwards) else { return false }
        self.element = element
        original = text
        before = String(value[..<range.lowerBound].suffix(40))
        after = String(value[range.upperBound...].prefix(40))
        deadline = Date().addingTimeInterval(180)
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self else { return }
            if Date() > self.deadline || !self.read() { self.finish() }
        }
        return true
    }

    /// False once the text can't be followed any more; while another field has the focus it just waits.
    @discardableResult
    private func read() -> Bool {
        guard let element else { return false }
        guard let focused = focusedElement(), CFEqual(focused, element) else { return true }
        guard let value = self.text(element),
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
    var onStateChange: ((Phase) -> Void)?
    var onError: ((String) -> Void)?
    var onResult: ((_ insertionRequested: Bool) -> Void)?

    let hotkey = Hotkey()
    let corrections: CorrectionWatcher
    private let effects: Effects
    private let daemon: DaemonClient

    enum Phase { case idle, starting, recording, handsFree, processing }
    private(set) var phase = Phase.idle { didSet { onStateChange?(phase) } }
    private var pendingSelections = 0
    private var pendingProbes = 0
    private var startingCalls = 0
    private var finishingReplies = 0
    var busyForHandoff: Bool {
        phase != .idle || hotkey.isDown || pendingSelections > 0 || pendingProbes > 0
            || startingCalls > 0 || finishingReplies > 0 || corrections.busyForHandoff
    }

    private var pressedAt = Date.distantPast
    private var lastTapAt = Date.distantPast
    private var context: JSON = [:]
    private var command = false
    private var translate = false
    private var streamTimer: Timer?
    private var streamed = false
    private var session: String?

    private let tapWindow = 0.3
    private let doubleTapWindow = 0.4

    struct Effects {
        var audio: (start: () throws -> Void, stop: () -> [Float], take: () -> [Float])
        var context: (_ aroundCursor: Bool) -> JSON
        var selectedText: (@escaping (String?) -> Void) -> Void
        typealias Insertion = (String, String, CFTimeInterval, CFTimeInterval, @escaping () -> Void) -> Void
        var target: (begin: () -> Void, invalidate: () -> Void, stop: () -> Void, finish: () -> Insertion?)
        var hud: (show: (HUD.State?) -> Void, preview: (String) -> Void, contains: (CGPoint?) -> Bool)
        var request: (String, JSON, ((JSON) -> Void)?) -> Void
        var copy: (String) -> Void

        static func live(daemon: DaemonClient, hotkey: Hotkey) -> Effects {
            let mic = Microphone(), hud = HUD()
            var target: PasteTarget?
            mic.onLevel = { hud.level($0) }
            return Effects(
                audio: (mic.start, mic.stop, mic.take),
                context: { around in
                    if !around { return ["app": FocusedApp.bundleID ?? "", "title": FocusedApp.windowTitle ?? ""] }
                    guard let text = FocusedApp.textAroundCursor() else { return [:] }
                    return ["before": text.before, "after": text.after]
                },
                selectedText: FocusedApp.selectedText,
                target: (begin: { FocusedApp.exposeTextFields(); target = PasteTarget() },
                         invalidate: { target?.invalidate() }, stop: { target?.stop(); target = nil },
                         finish: {
                    let saved = target
                    let paste = saved?.canPaste(inputMonitoring: hotkey.isActive) == true
                    saved?.stop(); target = nil
                    guard paste, let saved else { return nil }
                    return { text, session, releasedAt, readyAt, done in
                        let sentAt = CACurrentMediaTime()
                        Keyboard.press(9, flags: .maskCommand)
                        saved.observeInsertion(text, session: session, releasedAt: releasedAt,
                                               readyAt: readyAt, sentAt: sentAt, completion: done)
                    }
                }),
                hud: (show: { state in
                    hud.cloud = daemon.cloud
                    if let state { hud.show(state) } else { hud.hide() }
                }, preview: hud.setPreview, contains: hud.contains),
                request: { daemon.request($0, $1, reply: $2) }, copy: Clipboard.copy)
        }
    }

    init(daemon: DaemonClient, effects: Effects? = nil, corrections: CorrectionWatcher = CorrectionWatcher()) {
        self.daemon = daemon
        self.effects = effects ?? .live(daemon: daemon, hotkey: hotkey)
        self.corrections = corrections
        hotkey.onPress = { [weak self] in self?.pressed(shift: $0, control: $1) }
        hotkey.onRelease = { [weak self] in self?.released() }
        hotkey.onKeyDown = { [weak self] in self?.keyDown($0) }
        hotkey.onMouseDown = { [weak self] location in
            guard let self, !self.effects.hud.contains(location) else { return }
            self.effects.target.invalidate()
        }
    }

    func receive(event: JSON) {
        guard event["event"] as? String == "dictation_preview",
              let session, event["session"] as? String == session,
              phase == .recording || phase == .handsFree || phase == .processing,
              let text = event["text"] as? String else { return }
        effects.hud.preview(text)
    }

    func disconnected() {
        if phase != .idle { cancel() }
        else { effects.hud.show(nil) }
    }

    private func pressed(shift: Bool, control: Bool) {
        guard isEnabled else {
            Log.write("hotkey ignored: daemon not ready")
            return
        }
        switch phase {
        case .idle:
            guard startingCalls == 0, finishingReplies == 0 else { return }
            pressedAt = Date()
            let doubleTap = pressedAt.timeIntervalSince(lastTapAt) < doubleTapWindow
            begin(command: shift, translate: control && !shift, handsFree: doubleTap)
        case .handsFree:
            finish()
        case .starting, .recording, .processing:
            break
        }
    }

    private func released() {
        if phase == .starting { cancel(); return }
        guard phase == .recording else { return }
        if Date().timeIntervalSince(pressedAt) < tapWindow {
            lastTapAt = Date()
            cancel()
        } else {
            finish(releasedAt: hotkey.releasedAt)
        }
    }

    private func keyDown(_ keyCode: Int64) {
        switch phase {
        case .recording: cancel()
        case .handsFree, .processing:
            if keyCode == 53 { cancel() } else { effects.target.invalidate() }
        default: break
        }
    }

    private func begin(command: Bool, translate: Bool, handsFree: Bool) {
        startingCalls += 1
        defer { startingCalls -= 1 }
        phase = .starting
        let session = UUID().uuidString
        self.session = session
        corrections.finish()
        guard phase == .starting, self.session == session else { return }
        self.command = command
        self.translate = translate
        context = effects.context(false)
        context["command"] = command
        context["translate"] = translate
        if command {
            pendingSelections += 1
            let selected: (String?) -> Void = { [weak self] text in
                guard let self else { return }
                defer { self.pendingSelections -= 1 }
                guard self.session == session else { return }
                self.context["selection"] = text ?? ""
            }
            effects.selectedText(selected)
        }
        guard phase == .starting, self.session == session else { return }
        do {
            try effects.audio.start()
        } catch {
            Log.write("microphone failed: \(error)")
            self.session = nil
            phase = .idle
            onError?("Microphone: \(error.localizedDescription)")
            return
        }
        guard phase == .starting, self.session == session else {
            _ = effects.audio.stop()
            return
        }
        phase = handsFree ? .handsFree : .recording
        effects.target.begin()
        effects.hud.show(.listening(command: command, translate: translate))
        onBusyChange?(true)
        // Cloud sends small PCM frames; local coalesces audio while one recognition is in flight.
        streamed = false
        streamTimer = Timer.scheduledTimer(withTimeInterval: daemon.cloud ? 0.25 : 1, repeats: true) { [weak self] _ in self?.stream() }
        // Read after the microphone starts: a slow field must not cut off the first word.
        if !command { context.merge(effects.context(true)) { _, new in new } }
    }

    private func stream() {
        guard let session else { return }
        let chunk = effects.audio.take()
        guard !chunk.isEmpty else { return }
        var request: JSON = ["audio": Self.encode(chunk), "first": !streamed, "session": session]
        if !streamed {
            request["app"] = context["app"]
            request["title"] = context["title"]
        }
        effects.request("dictate_audio", request, nil)
        streamed = true
    }

    func cancel() {
        guard phase != .idle else { return }
        streamTimer?.invalidate()
        streamTimer = nil
        if phase == .recording || phase == .handsFree {
            _ = effects.audio.stop()
        }
        if streamed, let session { effects.request("dictate_cancel", ["session": session], nil) }
        session = nil
        streamed = false
        effects.target.stop()
        effects.hud.show(nil)
        phase = .idle
        onBusyChange?(false)
    }

    func finish(releasedAt: CFTimeInterval = CACurrentMediaTime()) {
        streamTimer?.invalidate()
        streamTimer = nil
        let samples = effects.audio.stop()
        phase = .processing
        guard samples.count > 16_000 / 4 else {
            Log.write("too short: \(samples.count) samples")
            cancel()
            return
        }
        Log.write("dictating \(samples.count / 16) ms from \(context["app"] ?? "")")
        guard let session else { cancel(); return }
        effects.hud.show(.thinking)
        stream()

        var request = context
        request["audio"] = Self.encode(effects.audio.take())
        request["streamed"] = streamed
        request["session"] = session
        let completion: (JSON) -> Void = { [weak self] reply in
            guard let self, self.session == session, self.phase == .processing else { return }
            self.finishingReplies += 1
            defer { self.finishingReplies -= 1 }
            self.session = nil
            self.streamed = false
            let insert = self.effects.target.finish()
            if let text = reply["text"] as? String, !text.isEmpty {
                let readyAt = CACurrentMediaTime()
                Log.write("dictation ready after \(Int((readyAt - releasedAt) * 1000)) ms")
                Log.write("copying \(text.count) chars to the clipboard")
                self.effects.copy(text)
                if let insert {
                    self.pendingProbes += 1
                    let done: () -> Void = { [weak self] in self?.pendingProbes -= 1 }
                    insert(text, session, releasedAt, readyAt, done)
                } else {
                    Log.write("dictation target changed or unavailable; kept text on clipboard")
                }
                self.effects.hud.show(.success)
                if insert != nil && !self.command && !self.translate { self.corrections.track(text) }
                self.onResult?(insert != nil)
            } else {
                self.effects.hud.show(nil)
                if let error = reply["error"] as? String {
                    Log.write("dictation failed: \(error)")
                    self.onError?(error)
                    NSSound.beep()
                }
            }
            self.phase = .idle
            self.onBusyChange?(false)
        }
        effects.request("dictate", request, completion)
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
