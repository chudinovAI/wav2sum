import AppKit

private final class ShareExample {
    let id: String
    let raw: String
    let text: String
    let language: String
    let style: String
    let source: String
    var target: String
    var selected = false
    var confirmed = false

    init?(_ data: JSON) {
        guard let id = data["id"] as? String, let raw = data["raw"] as? String,
              let text = data["text"] as? String, let target = data["target"] as? String else { return nil }
        self.id = id; self.raw = raw; self.text = text; self.target = target
        self.language = data["language"] as? String ?? ""
        self.style = data["style"] as? String ?? ""
        self.source = data["source"] as? String ?? ""
    }
}

final class SharingWindow: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSTextViewDelegate, NSSearchFieldDelegate {
    private let window: NSWindow
    private let search = NSSearchField()
    private let table = NSTableView()
    private let raw = NSTextView()
    private let clean = NSTextView()
    private let target = NSTextView()
    private let confirm = NSButton(checkboxWithTitle: "I confirm this target is a human-reviewed training example", target: nil, action: nil)
    private let message = NSTextField(wrappingLabelWithString: "")
    private var examples: [ShareExample] = []
    private var shown: [ShareExample] = []
    private var current: ShareExample?
    private var updating = false
    private var exporting = false

    override init() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 820, height: 760), styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        super.init()
        window.title = "Share Reviewed Examples"
        window.isReleasedWhenClosed = false
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 7
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 18, bottom: 16, right: 18)
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView?.addSubview(stack)
        if let content = window.contentView {
            NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: content.leadingAnchor), stack.trailingAnchor.constraint(equalTo: content.trailingAnchor), stack.topAnchor.constraint(equalTo: content.topAnchor), stack.bottomAnchor.constraint(equalTo: content.bottomAnchor)])
        }
        func button(_ title: String, _ action: Selector) -> NSButton { NSButton(title: title, target: self, action: action) }
        func add(_ view: NSView) { stack.addArrangedSubview(view) }
        add(NSTextField(wrappingLabelWithString: "Review your own text. Select examples, edit the target if needed, and confirm each target. Export creates a new local file; nothing is uploaded or changed in your history."))
        search.placeholderString = "Filter examples"
        search.delegate = self
        search.target = self; search.action = #selector(filterChanged)
        search.widthAnchor.constraint(equalToConstant: 400).isActive = true
        add(search)
        let choices = NSStackView(views: [button("Select All Shown", #selector(selectShown)), button("Clear Selection", #selector(clearSelection)), button("Reload", #selector(reload))])
        add(choices)
        for (id, title, width) in [("selected", "Selected", 65.0), ("source", "Source", 75.0), ("preview", "Example", 500.0), ("reviewed", "Confirmed", 80.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title; column.width = width
            table.addTableColumn(column)
        }
        table.delegate = self; table.dataSource = self
        table.allowsMultipleSelection = false
        let tableScroll = NSScrollView()
        tableScroll.documentView = table; tableScroll.hasVerticalScroller = true; tableScroll.borderType = .bezelBorder
        tableScroll.heightAnchor.constraint(equalToConstant: 180).isActive = true
        add(tableScroll)
        tableScroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -36).isActive = true
        func textArea(_ title: String, _ text: NSTextView, editable: Bool) {
            add(NSTextField(labelWithString: title))
            text.isEditable = editable; text.isSelectable = true; text.font = NSFont.systemFont(ofSize: 12)
            let scroll = NSScrollView(); scroll.documentView = text; scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
            scroll.heightAnchor.constraint(equalToConstant: 70).isActive = true
            add(scroll)
            scroll.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -36).isActive = true
        }
        textArea("Raw transcription", raw, editable: false)
        textArea("Cleaned text", clean, editable: false)
        textArea("Training target (edit as needed)", target, editable: true)
        target.delegate = self
        confirm.target = self; confirm.action = #selector(confirmTarget)
        add(confirm)
        add(button("Export Selected…", #selector(exportSelected)))
        add(message)
        window.center()
        showCurrent(nil)
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        reload()
    }
    @objc private func reload() {
        guard !exporting else { return }
        message.stringValue = "Loading examples…"
        DaemonProcess.setup(["action": "examples"]) { [weak self] reply in
            guard let self else { return }
            guard reply["ok"] as? Bool == true, let values = reply["examples"] as? [JSON] else {
                self.message.stringValue = reply["error"] as? String ?? "Could not read examples."
                return
            }
            self.examples = values.compactMap(ShareExample.init)
            self.showCurrent(nil)
            self.refilter()
            self.message.stringValue = "\(self.examples.count) examples available. Reload discards selections and edits."
        }
    }
    private func refilter() {
        let query = search.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        shown = examples.filter { example in
            query.isEmpty || [example.raw, example.text, example.target, example.language, example.style, example.source].contains { $0.localizedCaseInsensitiveContains(query) }
        }
        table.reloadData()
        if let current, let index = shown.firstIndex(where: { $0 === current }) {
            table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
        } else { showCurrent(nil) }
    }
    @objc private func filterChanged() { refilter() }
    func controlTextDidChange(_ obj: Notification) { if obj.object as? NSSearchField === search { refilter() } }
    func numberOfRows(in tableView: NSTableView) -> Int { shown.count }
    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard row >= 0, row < shown.count, let identifier = tableColumn?.identifier.rawValue else { return nil }
        let example = shown[row]
        if identifier == "selected" {
            let check = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggleSelection(_:)))
            check.state = example.selected ? .on : .off; check.tag = row
            return check
        }
        let value: String
        switch identifier {
        case "source": value = example.source
        case "reviewed": value = example.confirmed ? "Yes" : "No"
        default: value = example.text.replacingOccurrences(of: "\n", with: " ")
        }
        return NSTextField(labelWithString: value)
    }
    func tableViewSelectionDidChange(_ notification: Notification) {
        let row = table.selectedRow
        showCurrent(row >= 0 && row < shown.count ? shown[row] : nil)
    }
    private func showCurrent(_ example: ShareExample?) {
        current = example; updating = true
        raw.string = example?.raw ?? ""; clean.string = example?.text ?? ""; target.string = example?.target ?? ""
        target.isEditable = example != nil
        confirm.isEnabled = example != nil
        confirm.state = example?.confirmed == true ? .on : .off
        updating = false
    }
    func textDidChange(_ notification: Notification) {
        guard !updating, let example = current, notification.object as? NSTextView === target else { return }
        example.target = target.string; example.confirmed = false; confirm.state = .off
        table.reloadData()
    }
    @objc private func confirmTarget() {
        guard let example = current else { return }
        example.confirmed = confirm.state == .on
        table.reloadData()
    }
    @objc private func toggleSelection(_ sender: NSButton) {
        guard sender.tag >= 0, sender.tag < shown.count else { return }
        shown[sender.tag].selected = sender.state == .on
    }
    @objc private func selectShown() { shown.forEach { $0.selected = true }; table.reloadData() }
    @objc private func clearSelection() { examples.forEach { $0.selected = false }; table.reloadData() }
    @objc private func exportSelected() {
        guard !exporting else { return }
        let selected = examples.filter { $0.selected }
        guard !selected.isEmpty else { message.stringValue = "Select at least one example."; return }
        guard selected.allSatisfy({ $0.confirmed && !$0.target.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            message.stringValue = "Review and confirm every selected, nonempty target before export."; return
        }
        let consent = NSAlert()
        consent.messageText = "Export reviewed examples for training?"
        consent.informativeText = "The selected raw and target text, language, and style will be written to a new local JSONL file. You are responsible for sharing it. No network upload occurs."
        consent.addButton(withTitle: "I Consent to Export")
        consent.addButton(withTitle: "Cancel")
        guard consent.runModal() == .alertFirstButtonReturn else { return }
        let panel = NSSavePanel()
        panel.title = "Save reviewed training examples"
        panel.nameFieldStringValue = "wav2sum-examples.jsonl"
        panel.message = "Choose a new file. Existing files are never overwritten."
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let selections: [JSON] = selected.map { ["id": $0.id, "target": $0.target, "confirmed": true] }
        exporting = true
        message.stringValue = "Exporting…"
        DaemonProcess.setup(["action": "export_examples", "path": url.path, "consent": true, "examples": selections]) { [weak self] reply in
            guard let self else { return }
            self.exporting = false
            self.message.stringValue = reply["ok"] as? Bool == true ? "Exported \(reply["count"] as? Int ?? selections.count) examples to \(url.lastPathComponent)." : (reply["error"] as? String ?? "Export failed.")
        }
    }
}
