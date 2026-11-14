import AppKit

private final class HistoryCell: NSTableCellView {
    let primary = NSTextField(labelWithString: "")
    let secondary = NSTextField(labelWithString: "")

    override var backgroundStyle: NSView.BackgroundStyle {
        didSet {
            primary.textColor = backgroundStyle == .emphasized ? .alternateSelectedControlTextColor : .labelColor
            secondary.textColor = backgroundStyle == .emphasized ? .alternateSelectedControlTextColor : .secondaryLabelColor
        }
    }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        primary.font = .systemFont(ofSize: 13, weight: .medium)
        secondary.font = .systemFont(ofSize: 11)
        secondary.textColor = .secondaryLabelColor
        primary.lineBreakMode = .byTruncatingTail
        secondary.lineBreakMode = .byTruncatingTail
        textField = primary
        let labels = NSStackView(views: [primary, secondary])
        labels.orientation = .vertical
        labels.alignment = .leading
        labels.spacing = 2
        labels.translatesAutoresizingMaskIntoConstraints = false
        addSubview(labels)
        NSLayoutConstraint.activate([
            labels.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 8),
            labels.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -8),
            labels.centerYAnchor.constraint(equalTo: centerYAnchor),
            primary.widthAnchor.constraint(equalTo: labels.widthAnchor),
            secondary.widthAnchor.constraint(equalTo: labels.widthAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("HistoryCell is created programmatically") }
}

final class HistoryView: NSView, NSTableViewDataSource, NSTableViewDelegate {
    var onProcess: ((JSON) -> Void)?
    var onOpen: ((String) -> Void)?
    var onRefresh: (() -> Void)?
    var cloud = false { didSet { if oldValue != cloud { updateActions() } } }
    var callsEnabled = true {
        didSet {
            guard oldValue != callsEnabled else { return }
            kind.isHidden = !callsEnabled
            if !callsEnabled { kind.selectedSegment = 1 }
            reloadSelection()
        }
    }

    private let kind = NSSegmentedControl(labels: ["Calls", "Dictations"], trackingMode: .selectOne, target: nil, action: nil)
    private let table = NSTableView()
    private let detail = NSTextView()
    private let process = NSButton(title: "Process Recording", target: nil, action: nil)
    private let open = NSButton(title: "Open Result", target: nil, action: nil)
    private let refresh = NSButton(title: "Refresh", target: nil, action: nil)
    private let status = NSTextField(wrappingLabelWithString: "")
    private let placeholder = NSTextField(wrappingLabelWithString: "")
    private let placeholderSymbol = NSImageView()
    private let detailFont = NSFont.systemFont(ofSize: 13)
    private let detailTitleFont = NSFont.systemFont(ofSize: 20, weight: .semibold)
    private let retention = NSTextField(wrappingLabelWithString: "Shows the last 500 dictations. Entries older than seven days are pruned; entries of unknown age are retained.")
    private var calls: [JSON] = []
    private var dictations: [JSON] = []
    private var selectedCall: JSON?
    private var selectedDictation: JSON?
    private var canProcess = false
    private var isLoading = false
    private var error: String?
    private var changingSelection = false

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        kind.identifier = NSUserInterfaceItemIdentifier("history-kind")
        kind.setAccessibilityLabel("History type")
        kind.selectedSegment = 0
        kind.target = self; kind.action = #selector(changeKind)
        table.identifier = NSUserInterfaceItemIdentifier("history-table")
        table.setAccessibilityLabel("History entries")
        table.delegate = self; table.dataSource = self
        table.allowsMultipleSelection = false
        table.autoresizingMask = [.width]
        table.headerView = nil
        table.style = .inset
        table.rowHeight = 48
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("entry"))
        column.title = "History"; column.resizingMask = .autoresizingMask
        table.addTableColumn(column)
        let tableScroll = NSScrollView()
        tableScroll.documentView = table
        tableScroll.hasVerticalScroller = true
        tableScroll.autohidesScrollers = true
        tableScroll.borderType = .noBorder
        tableScroll.translatesAutoresizingMaskIntoConstraints = false
        detail.identifier = NSUserInterfaceItemIdentifier("history-detail")
        detail.setAccessibilityLabel("History entry details")
        detail.isEditable = false; detail.isSelectable = true
        detail.font = detailFont
        detail.textColor = .labelColor
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineSpacing = 4
        detail.defaultParagraphStyle = paragraph
        detail.textContainerInset = NSSize(width: 12, height: 12)
        detail.minSize = NSSize(width: 0, height: 0)
        detail.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        detail.isVerticallyResizable = true; detail.isHorizontallyResizable = false
        detail.autoresizingMask = [.width]
        detail.textContainer?.widthTracksTextView = true
        let detailScroll = NSScrollView()
        detailScroll.documentView = detail
        detailScroll.hasVerticalScroller = true
        detailScroll.autohidesScrollers = true
        detailScroll.borderType = .noBorder
        detailScroll.translatesAutoresizingMaskIntoConstraints = false
        placeholder.textColor = .secondaryLabelColor
        placeholder.font = .systemFont(ofSize: 14, weight: .medium)
        placeholder.alignment = .center
        placeholderSymbol.image = NSImage(systemSymbolName: "doc.text", accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: 36, weight: .light))
        placeholderSymbol.contentTintColor = .tertiaryLabelColor
        placeholderSymbol.setAccessibilityElement(false)
        let emptyState = NSStackView(views: [placeholderSymbol, placeholder])
        emptyState.orientation = .vertical
        emptyState.spacing = 12
        emptyState.translatesAutoresizingMaskIntoConstraints = false
        let detailPane = NSView()
        detailPane.translatesAutoresizingMaskIntoConstraints = false
        detailPane.addSubview(detailScroll)
        detailPane.addSubview(emptyState)
        process.identifier = NSUserInterfaceItemIdentifier("history-process")
        process.setAccessibilityLabel("Process selected recording")
        process.target = self; process.action = #selector(processSelected)
        open.identifier = NSUserInterfaceItemIdentifier("history-open")
        open.setAccessibilityLabel("Open selected result")
        open.target = self; open.action = #selector(openSelected)
        refresh.identifier = NSUserInterfaceItemIdentifier("history-refresh")
        refresh.setAccessibilityLabel("Refresh history")
        refresh.keyEquivalent = "r"
        refresh.keyEquivalentModifierMask = .command
        refresh.target = self; refresh.action = #selector(refreshHistory)
        status.identifier = NSUserInterfaceItemIdentifier("history-status")
        status.setAccessibilityLabel("History status")
        status.isSelectable = true
        status.maximumNumberOfLines = 2
        status.lineBreakMode = .byTruncatingTail
        status.font = .systemFont(ofSize: 12)
        retention.textColor = .secondaryLabelColor
        retention.font = .systemFont(ofSize: 11)
        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        let header = NSStackView(views: [kind, spacer, refresh])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 8
        let listLabel = NSTextField(labelWithString: "History")
        let detailLabel = NSTextField(labelWithString: "Details")
        for label in [listLabel, detailLabel] {
            label.font = .systemFont(ofSize: 12, weight: .semibold)
            label.textColor = .secondaryLabelColor
        }
        let listPane = NSStackView(views: [listLabel, tableScroll])
        listPane.orientation = .vertical; listPane.alignment = .leading; listPane.spacing = 6
        let details = NSStackView(views: [detailLabel, detailPane])
        details.orientation = .vertical; details.alignment = .leading; details.spacing = 6
        let content = NSStackView(views: [listPane, details])
        content.orientation = .horizontal; content.alignment = .top; content.spacing = 12
        let actions = NSStackView(views: [process, open])
        actions.orientation = .horizontal; actions.spacing = 8
        let stack = NSStackView(views: [header, content, status, retention, actions])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        // Native control bounds can extend beyond the alignment rects used by NSStackView.
        let controlInsets = NSEdgeInsets(
            top: max(kind.alignmentRectInsets.top, refresh.alignmentRectInsets.top),
            left: max(kind.alignmentRectInsets.left, process.alignmentRectInsets.left),
            bottom: max(process.alignmentRectInsets.bottom, open.alignmentRectInsets.bottom),
            right: max(refresh.alignmentRectInsets.right, open.alignmentRectInsets.right)
        )
        let listMinimum = listPane.widthAnchor.constraint(greaterThanOrEqualToConstant: 220)
        let detailMinimum = details.widthAnchor.constraint(greaterThanOrEqualToConstant: 300)
        listMinimum.priority = .defaultHigh
        detailMinimum.priority = .defaultHigh
        let listProportion = listPane.widthAnchor.constraint(equalTo: content.widthAnchor, multiplier: 0.34)
        listProportion.priority = .defaultLow
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: controlInsets.left),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -controlInsets.right),
            stack.topAnchor.constraint(equalTo: topAnchor, constant: controlInsets.top),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -controlInsets.bottom),
            header.widthAnchor.constraint(equalTo: stack.widthAnchor),
            content.widthAnchor.constraint(equalTo: stack.widthAnchor),
            status.widthAnchor.constraint(equalTo: stack.widthAnchor),
            retention.widthAnchor.constraint(equalTo: stack.widthAnchor),
            listPane.heightAnchor.constraint(equalTo: content.heightAnchor),
            details.heightAnchor.constraint(equalTo: content.heightAnchor),
            details.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            tableScroll.widthAnchor.constraint(equalTo: listPane.widthAnchor),
            detailPane.widthAnchor.constraint(equalTo: details.widthAnchor),
            detailScroll.leadingAnchor.constraint(equalTo: detailPane.leadingAnchor),
            detailScroll.trailingAnchor.constraint(equalTo: detailPane.trailingAnchor),
            detailScroll.topAnchor.constraint(equalTo: detailPane.topAnchor),
            detailScroll.bottomAnchor.constraint(equalTo: detailPane.bottomAnchor),
            emptyState.centerXAnchor.constraint(equalTo: detailPane.centerXAnchor),
            emptyState.centerYAnchor.constraint(equalTo: detailPane.centerYAnchor),
            emptyState.widthAnchor.constraint(lessThanOrEqualTo: detailPane.widthAnchor, constant: -32),
            listMinimum, detailMinimum, listProportion,
        ])
        showSelection()
    }

    required init?(coder: NSCoder) { fatalError("HistoryView is created programmatically") }

    func update(calls: [JSON], dictations: [JSON], canProcess: Bool, isLoading: Bool, error: String?) {
        self.calls = calls; self.dictations = dictations
        self.canProcess = canProcess; self.isLoading = isLoading; self.error = error
        selectedCall = resolveCall(selectedCall, in: calls)
        selectedDictation = resolveID(selectedDictation, in: dictations)
        reloadSelection()
    }
    func setProcessingEnabled(_ enabled: Bool) {
        guard enabled != canProcess else { return }
        canProcess = enabled
        updateActions()
    }


    private func nonempty(_ value: Any?) -> String? {
        guard let string = value as? String, !string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return string
    }

    private func resolveID(_ previous: JSON?, in entries: [JSON]) -> JSON? {
        guard let id = nonempty(previous?["id"]) else { return nil }
        return entries.first { nonempty($0["id"]) == id }
    }

    private func resolveCall(_ previous: JSON?, in entries: [JSON]) -> JSON? {
        if let match = resolveID(previous, in: entries) { return match }
        guard let previous, nonempty(previous["out_dir"]) == nil,
              let audio = nonempty(previous["audio"]) else { return nil }
        let matches = entries.filter { nonempty($0["audio"]) == audio && nonempty($0["out_dir"]) != nil }
        return matches.count == 1 ? matches[0] : nil
    }

    private var showingCalls: Bool { callsEnabled && kind.selectedSegment == 0 }
    private var entries: [JSON] { showingCalls ? calls : dictations }

    private func reloadSelection() {
        changingSelection = true
        table.reloadData()
        let selected = showingCalls ? selectedCall : selectedDictation
        if let id = nonempty(selected?["id"]), let row = entries.firstIndex(where: { nonempty($0["id"]) == id }) {
            table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
        } else {
            table.deselectAll(nil)
        }
        changingSelection = false
        showSelection()
    }

    @objc private func changeKind() { reloadSelection() }
    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard entries.indices.contains(row) else { return nil }
        let entry = entries[row]
        let primary: String
        let secondary: String
        if showingCalls {
            primary = nonempty(entry["title"]) ?? nonempty(entry["audio"]) ?? "Untitled call"
            let state = stateTitle(nonempty(entry["state"]))
            if let stage = nonempty(entry["stage"]), let code = entry["state"] as? String,
               code == "queued" || code == "running" || code == "failed" {
                secondary = "\(state) · \(stage)"
            } else {
                secondary = state
            }
        } else {
            primary = (nonempty(entry["text"]) ?? "Empty dictation").replacingOccurrences(of: "\n", with: " ")
            secondary = nonempty(entry["time"]) ?? "Unknown time"
        }
        let identifier = NSUserInterfaceItemIdentifier("history-entry-cell")
        let cell = tableView.makeView(withIdentifier: identifier, owner: self) as? HistoryCell ?? HistoryCell()
        cell.identifier = identifier
        cell.primary.stringValue = primary
        cell.secondary.stringValue = secondary
        cell.setAccessibilityLabel("\(primary), \(secondary)")
        return cell
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !changingSelection else { return }
        let row = table.selectedRow
        let selected = entries.indices.contains(row) ? entries[row] : nil
        if showingCalls { selectedCall = selected } else { selectedDictation = selected }
        showSelection()
    }

    private func stateTitle(_ state: String?) -> String {
        switch state {
        case "raw": return "Ready to process"
        case "recording": return "Recording"
        case "queued": return "Queued"
        case "running": return "Processing"
        case "failed": return "Failed"
        case "done": return "Completed"
        default: return "Unknown"
        }
    }

    private func showSelection() {
        if showingCalls, let call = selectedCall {
            let duration: String
            if let seconds = call["duration_sec"] as? NSNumber { duration = "\(seconds) s" } else { duration = "Unknown" }
            let speakers = (call["speakers"] as? [String])?.joined(separator: ", ") ?? ""
            var lines = [nonempty(call["title"]) ?? "Untitled call", "",
                         stateTitle(nonempty(call["state"])), "Duration: \(duration)",
                         "Source: \(nonempty(call["audio"]) ?? "Unknown")"]
            if !speakers.isEmpty { lines.append("Speakers: \(speakers)") }
            if let stage = nonempty(call["stage"]) { lines.append("Stage: \(stage)") }
            if let failure = nonempty(call["error"]) { lines.append("Error: \(failure)") }
            if let output = nonempty(call["out_dir"]) { lines.append("Output folder: \(output)") }
            if let result = nonempty(call["open_path"]) { lines.append("Result: \(result)") }
            detail.string = lines.joined(separator: "\n")
        } else if !showingCalls, let dictation = selectedDictation {
            detail.string = "Time: \(nonempty(dictation["time"]) ?? "Unknown")\nMode: \(nonempty(dictation["mode"]) ?? "Unknown")\n\nFinal text:\n\(dictation["text"] as? String ?? "")\n\nRaw text:\n\(dictation["raw"] as? String ?? "")"
        } else {
            detail.string = ""
        }
        if let error = nonempty(error) {
            if !detail.string.isEmpty { detail.string += "\n\n" }
            detail.string += "History error: \(error)"
        }
        detail.textStorage?.addAttribute(.font, value: detailFont,
                                         range: NSRange(location: 0, length: (detail.string as NSString).length))
        if showingCalls, selectedCall != nil {
            let firstLine = (detail.string as NSString).lineRange(for: NSRange(location: 0, length: 0))
            detail.textStorage?.addAttribute(.font, value: detailTitleFont, range: firstLine)
        }
        placeholder.isHidden = !detail.string.isEmpty
        placeholderSymbol.isHidden = placeholder.isHidden
        if isLoading {
            placeholder.stringValue = "Loading history…"
        } else if entries.isEmpty {
            placeholder.stringValue = showingCalls ? "No calls in history." : "No dictations in history."
        } else {
            placeholder.stringValue = showingCalls ? "Select a call to see its details." : "Select a dictation to see its details."
        }
        process.isHidden = !showingCalls
        open.isHidden = !showingCalls
        retention.isHidden = showingCalls
        if let error = nonempty(error) {
            status.stringValue = "History error: \(error)"
            status.textColor = .systemRed
        } else if isLoading {
            status.stringValue = "Loading history…"
            status.textColor = .secondaryLabelColor
        } else if entries.isEmpty {
            status.stringValue = showingCalls ? "No calls in history." : "No dictations in history."
            status.textColor = .secondaryLabelColor
        } else {
            let noun = showingCalls ? "call" : "dictation"
            status.stringValue = "\(entries.count) \(noun)\(entries.count == 1 ? "" : "s")"
            status.textColor = .secondaryLabelColor
        }
        status.toolTip = status.stringValue
        updateActions()
    }

    private func processable(_ call: JSON) -> Bool {
        let state = nonempty(call["state"])
        return (state == "raw" || state == "failed") && nonempty(call["audio"]) != nil && nonempty(call["out_dir"]) == nil && nonempty(call["open_path"]) == nil
    }

    private func resultPath(_ call: JSON) -> String? {
        guard nonempty(call["out_dir"]) != nil, let path = nonempty(call["open_path"]), path != nonempty(call["audio"]) else { return nil }
        return path
    }

    private func updateActions() {
        process.title = cloud ? "Process Recording (Cloud)" : "Process Recording"
        let available = !isLoading && nonempty(error) == nil
        process.isEnabled = showingCalls && available && canProcess && selectedCall.map(processable) == true
        open.isEnabled = showingCalls && available && selectedCall.flatMap(resultPath) != nil
        refresh.isEnabled = !isLoading
    }

    @objc private func processSelected() {
        guard process.isEnabled, let selectedCall, processable(selectedCall) else { return }
        onProcess?(selectedCall)
    }
    @objc private func openSelected() {
        guard open.isEnabled, let selectedCall, let path = resultPath(selectedCall) else { return }
        onOpen?(path)
    }
    @objc private func refreshHistory() { if refresh.isEnabled { onRefresh?() } }
}
