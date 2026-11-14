import AppKit

final class MainWindow: NSObject {
    struct State {
        let status: String
        let callsEnabled: Bool
        let recording: String
        let elapsed: String
        let recordingActive: Bool
        let recordTitle: String
        let recordEnabled: Bool
        let backgroundTitle: String
        let backgroundEnabled: Bool
        let processing: String
        let dictation: String
        let permissions: String
        let dictationMessage: String
        let dictationIsError: Bool
        let message: String
        let messageIsError: Bool
    }

    var onRecord: (() -> Void)?
    var onSettings: (() -> Void)?
    var onBackground: (() -> Void)?
    let window: NSWindow
    let history = HistoryView(frame: .zero)
    private let status = NSTextField(wrappingLabelWithString: "")
    private let recording = NSTextField(wrappingLabelWithString: "")
    private let elapsed = NSTextField(labelWithString: "00:00")
    private let record = NSButton(title: "Start Call Recording", target: nil, action: nil)
    private let call = NSStackView()
    private let callDivider = NSBox()
    private let background = NSButton(title: "", target: nil, action: nil)
    private let processing = NSTextField(wrappingLabelWithString: "")
    private let dictation = NSTextField(wrappingLabelWithString: "")
    private let permissions = NSTextField(wrappingLabelWithString: "")
    private let dictationMessage = NSTextField(wrappingLabelWithString: "")
    private let message = NSTextField(wrappingLabelWithString: "")
    private let startImage = NSImage(systemSymbolName: "record.circle.fill", accessibilityDescription: nil)?
        .withSymbolConfiguration(.init(paletteColors: [.systemRed]))
    private let stopImage = NSImage(systemSymbolName: "stop.fill", accessibilityDescription: nil)?
        .withSymbolConfiguration(.init(paletteColors: [.systemRed]))

    override init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 740),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init()
        window.title = "wav2sum"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 1000, height: 620)
        window.autorecalculatesKeyViewLoop = true

        let sidebar = NSVisualEffectView()
        sidebar.material = .sidebar
        sidebar.blendingMode = .withinWindow
        sidebar.state = .followsWindowActiveState
        sidebar.translatesAutoresizingMaskIntoConstraints = false
        let heading = NSTextField(labelWithString: "Call recording")
        heading.font = .systemFont(ofSize: 13, weight: .semibold)
        elapsed.font = .monospacedDigitSystemFont(ofSize: 48, weight: .regular)
        recording.font = .systemFont(ofSize: 12)
        recording.textColor = .secondaryLabelColor
        record.identifier = NSUserInterfaceItemIdentifier("record-call")
        record.target = self; record.action = #selector(toggleRecording)
        record.controlSize = .large
        record.bezelStyle = .rounded
        record.font = .systemFont(ofSize: 14, weight: .semibold)
        record.imagePosition = .imageLeading
        record.setAccessibilityHelp("Record microphone and system audio. Processing follows your Settings.")
        [heading, elapsed, recording, record].forEach { call.addArrangedSubview($0) }
        call.orientation = .vertical
        call.alignment = .leading
        call.spacing = 12
        let callHeader = NSStackView(views: [call])
        callHeader.orientation = .vertical
        callHeader.alignment = .leading
        callHeader.spacing = 16
        callHeader.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(callHeader)
        call.widthAnchor.constraint(equalTo: callHeader.widthAnchor).isActive = true
        for view in [elapsed, recording, record] {
            view.widthAnchor.constraint(equalTo: call.widthAnchor).isActive = true
        }
        record.heightAnchor.constraint(equalToConstant: 38).isActive = true

        let dictationHeading = NSTextField(labelWithString: "Dictation")
        dictationHeading.font = .systemFont(ofSize: 13, weight: .semibold)
        callDivider.boxType = .separator
        let secondDivider = NSBox(); secondDivider.boxType = .separator
        let spacer = NSView()
        spacer.setContentHuggingPriority(.init(1), for: .vertical)
        let information = NSStackView(views: [processing, message, callDivider, dictationHeading,
                                             dictation, dictationMessage, permissions, secondDivider, status, spacer])
        information.orientation = .vertical
        information.alignment = .leading
        information.spacing = 12
        information.edgeInsets = NSEdgeInsets(top: 0, left: 20, bottom: 20, right: 20)
        information.translatesAutoresizingMaskIntoConstraints = false
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.documentView = information
        scroll.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(scroll)
        NSLayoutConstraint.activate([
            information.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            information.heightAnchor.constraint(greaterThanOrEqualTo: scroll.contentView.heightAnchor)
        ])
        for view in information.arrangedSubviews where view !== spacer {
            view.widthAnchor.constraint(equalTo: information.widthAnchor, constant: -40).isActive = true
            view.setContentCompressionResistancePriority(.required, for: .vertical)
            view.setContentHuggingPriority(.defaultHigh, for: .vertical)
        }

        let settings = NSButton(title: "Settings…", target: self, action: #selector(openSettings))
        settings.identifier = NSUserInterfaceItemIdentifier("settings")
        settings.image = NSImage(systemSymbolName: "gearshape", accessibilityDescription: nil)
        settings.imagePosition = .imageLeading
        settings.keyEquivalent = ","
        settings.keyEquivalentModifierMask = .command
        background.identifier = NSUserInterfaceItemIdentifier("background-process")
        background.target = self; background.action = #selector(toggleBackground)
        for button in [settings, background] {
            button.controlSize = .small
            button.font = .systemFont(ofSize: 12)
        }
        let footer = NSStackView(views: [settings, background])
        footer.orientation = .vertical
        footer.alignment = .leading
        footer.spacing = 8
        footer.translatesAutoresizingMaskIntoConstraints = false
        let footerDivider = NSBox(); footerDivider.boxType = .separator
        footerDivider.translatesAutoresizingMaskIntoConstraints = false
        sidebar.addSubview(footerDivider)
        sidebar.addSubview(footer)

        let content = window.contentView!
        content.addSubview(sidebar)
        history.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(history)
        NSLayoutConstraint.activate([
            sidebar.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            sidebar.topAnchor.constraint(equalTo: content.topAnchor),
            sidebar.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            sidebar.widthAnchor.constraint(equalToConstant: 280),
            callHeader.topAnchor.constraint(equalTo: sidebar.topAnchor, constant: 24),
            callHeader.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20),
            callHeader.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -20),
            scroll.topAnchor.constraint(equalTo: callHeader.bottomAnchor, constant: 20),
            scroll.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor),
            scroll.bottomAnchor.constraint(equalTo: footerDivider.topAnchor, constant: -12),
            footerDivider.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20),
            footerDivider.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -20),
            footerDivider.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -12),
            footer.leadingAnchor.constraint(equalTo: sidebar.leadingAnchor, constant: 20),
            footer.trailingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: -20),
            footer.bottomAnchor.constraint(equalTo: sidebar.bottomAnchor, constant: -20),
            history.leadingAnchor.constraint(equalTo: sidebar.trailingAnchor, constant: 24),
            history.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
            history.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
            history.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -20)
        ])
        for (view, id) in [(status, "connection-state"), (recording, "recording-state"),
                           (elapsed, "recording-time"), (message, "recording-message"), (processing, "processing-state"),
                           (dictation, "dictation-state"), (permissions, "permission-state"),
                           (dictationMessage, "dictation-message")] {
            view.identifier = NSUserInterfaceItemIdentifier(id)
        }
        for field in [processing, dictation, status, permissions, message, dictationMessage] {
            field.font = .systemFont(ofSize: 12)
        }
        for field in [processing, permissions, status] {
            field.textColor = .secondaryLabelColor
            field.font = .systemFont(ofSize: 11)
        }
        message.isSelectable = true
        dictationMessage.isSelectable = true
        status.isSelectable = true
        window.initialFirstResponder = settings
        window.center()
    }

    func update(_ state: State) {
        let changedMessage = message.stringValue != state.message
        let changedDictationMessage = dictationMessage.stringValue != state.dictationMessage
        status.stringValue = state.status
        call.isHidden = !state.callsEnabled
        processing.isHidden = !state.callsEnabled
        callDivider.isHidden = !state.callsEnabled
        recording.stringValue = state.recording
        elapsed.stringValue = state.elapsed
        record.image = state.recordingActive ? stopImage : startImage
        record.title = state.recordTitle
        record.isEnabled = state.recordEnabled
        background.title = state.backgroundTitle
        background.isEnabled = state.backgroundEnabled
        processing.stringValue = state.processing
        dictation.stringValue = state.dictation
        permissions.stringValue = state.permissions
        dictationMessage.stringValue = state.dictationMessage
        dictationMessage.textColor = state.dictationIsError ? .systemRed : .labelColor
        message.stringValue = state.message
        message.textColor = state.messageIsError ? .systemRed : .labelColor
        message.isHidden = !state.callsEnabled || state.message.isEmpty
        dictationMessage.isHidden = state.dictationMessage.isEmpty
        message.setAccessibilityLabel(state.messageIsError ? "Recording error" : "Recording result")
        dictationMessage.setAccessibilityLabel(state.dictationIsError ? "Dictation error" : "Dictation result")
        if changedMessage && !state.message.isEmpty { reveal(message) }
        if changedDictationMessage && !state.dictationMessage.isEmpty { reveal(dictationMessage) }
    }

    private func reveal(_ field: NSTextField) {
        window.contentView!.layoutSubtreeIfNeeded()
        let height = min(80, field.bounds.height)
        let top = field.isFlipped ? 0 : field.bounds.maxY - height
        field.scrollToVisible(NSRect(x: 0, y: top, width: field.bounds.width, height: height))
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func toggleRecording() { onRecord?() }
    @objc private func openSettings() { onSettings?() }
    @objc private func toggleBackground() { onBackground?() }
}
