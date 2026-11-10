import AppKit

struct HistoryViewTest {
    private static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    private static func find<T: NSView>(_ view: NSView, _ id: String, as type: T.Type) -> T {
        descendants(view).compactMap { $0 as? T }.first { $0.identifier?.rawValue == id }!
    }

    static func run() {
        guard !NSScreen.screens.isEmpty else {
            print("History view test requires a graphical macOS session")
            exit(77)
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 600),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let view = HistoryView(frame: window.contentView!.bounds)
        window.contentView = view
        let kind = find(view, "history-kind", as: NSSegmentedControl.self)
        let table = find(view, "history-table", as: NSTableView.self)
        let detail = find(view, "history-detail", as: NSTextView.self)
        let process = find(view, "history-process", as: NSButton.self)
        let open = find(view, "history-open", as: NSButton.self)
        let refresh = find(view, "history-refresh", as: NSButton.self)
        let status = find(view, "history-status", as: NSTextField.self)
        let raw: JSON = ["id": "/recordings/a.wav", "title": "Call A", "audio": "/recordings/a.wav",
                         "out_dir": NSNull(), "open_path": NSNull(), "state": "raw",
                         "stage": "", "error": "", "duration_sec": NSNull(), "speakers": []]
        let done: JSON = ["id": "/output/call-a", "title": "Call A", "audio": "/recordings/a.wav",
                          "out_dir": "/output/call-a", "open_path": "/output/call-a/summary.md",
                          "state": "done", "stage": "complete", "error": "", "duration_sec": 12,
                          "speakers": ["Alice", "Bob"]]
        let dictated: JSON = ["id": "dictation-1", "time": "2026-01-01 12:00", "text": "Final words",
                              "raw": "Raw words", "mode": "cleanup"]
        let other: JSON = ["id": "dictation-2", "time": "2026-01-02", "text": "Other words",
                           "raw": "Other raw", "mode": "local"]
        func switchKind(_ segment: Int) {
            kind.selectedSegment = segment
            _ = kind.sendAction(kind.action!, to: kind.target)
        }
        func select(_ row: Int) { table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false) }

        // At the minimum history workspace size both panes and the actions remain usable,
        // including when an error is too long for the compact status line.
        window.setContentSize(NSSize(width: 720, height: 600))
        let longError = String(repeating: "History read failure. ", count: 100)
        view.update(calls: [], dictations: [], canProcess: true, isLoading: false, error: longError)
        view.layoutSubtreeIfNeeded()
        let listFrame = view.convert(table.enclosingScrollView!.bounds, from: table.enclosingScrollView!)
        let detailFrame = view.convert(detail.enclosingScrollView!.bounds, from: detail.enclosingScrollView!)
        precondition(listFrame.maxX <= detailFrame.minX, "history panes must be side by side")
        precondition(view.bounds.contains(view.convert(process.bounds, from: process)))
        precondition(view.bounds.contains(view.convert(refresh.bounds, from: refresh)))
        precondition(detail.string.contains(longError), "the full history error must remain selectable")
        window.setContentSize(NSSize(width: 440, height: 600))

        view.update(calls: [], dictations: [], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 0 && !process.isEnabled && !open.isEnabled)
        precondition(detail.string.isEmpty)
        view.update(calls: [raw], dictations: [dictated, other], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 1 && table.selectedRow == -1 && !process.isEnabled)
        select(0)
        precondition(detail.string.contains("/recordings/a.wav"))
        precondition(process.isEnabled && !open.isEnabled && !detail.isEditable && detail.isSelectable)

        view.update(calls: [done], dictations: [dictated, other], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 1 && table.selectedRow == 0, "raw to processed must keep the selected audio")
        precondition(!process.isEnabled && open.isEnabled && detail.string.contains("Alice") && detail.string.contains("12"))
        view.update(calls: [done], dictations: [dictated, other], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 1 && table.selectedRow == 0, "repeated snapshots must replace rows")
        view.update(calls: [done], dictations: [dictated, other], canProcess: false, isLoading: false, error: "Disconnected")
        precondition(status.stringValue.contains("Disconnected") && !process.isEnabled && !open.isEnabled)
        view.update(calls: [done], dictations: [dictated, other], canProcess: true, isLoading: true, error: nil)
        precondition(!open.isEnabled && !process.isEnabled && !refresh.isEnabled)

        view.update(calls: [done], dictations: [dictated, other], canProcess: true, isLoading: false, error: nil)
        switchKind(1)
        precondition(table.numberOfRows == 2 && table.selectedRow == -1 && !open.isEnabled)
        select(0)
        precondition(detail.string.contains("Final words") && detail.string.contains("Raw words"))
        precondition(detail.string.contains("cleanup") && detail.string.contains("2026-01-01"))
        view.update(calls: [done], dictations: [dictated, other], canProcess: true, isLoading: false, error: nil)
        precondition(table.selectedRow == 0 && detail.string.contains("Final words"))
        view.update(calls: [done], dictations: [other], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 1 && table.selectedRow == -1 && detail.string.isEmpty, "pruning must clear selection")
        switchKind(0)
        precondition(table.selectedRow == 0 && open.isEnabled)
        view.update(calls: [], dictations: [other], canProcess: true, isLoading: false, error: nil)
        precondition(table.selectedRow == -1 && detail.string.isEmpty && !open.isEnabled)

        var failed = raw
        failed["state"] = "failed"; failed["stage"] = "transcription"; failed["error"] = "Synthetic failure"
        view.update(calls: [failed], dictations: [], canProcess: false, isLoading: false, error: nil)
        select(0)
        precondition(detail.string.contains("Synthetic failure") && detail.string.contains("transcription") && !process.isEnabled)
        view.update(calls: [failed], dictations: [], canProcess: true, isLoading: false, error: nil)
        precondition(process.isEnabled)
        view.update(calls: [failed], dictations: [], canProcess: true, isLoading: true, error: nil)
        precondition(!process.isEnabled)
        view.update(calls: [failed], dictations: [], canProcess: true, isLoading: false, error: "Unavailable")
        precondition(!process.isEnabled && status.stringValue.contains("Unavailable"))
        var active = raw
        active["state"] = "recording"
        view.update(calls: [active], dictations: [], canProcess: true, isLoading: false, error: nil)
        precondition(!process.isEnabled && !open.isEnabled)
        active["state"] = "raw"; active["open_path"] = active["audio"]
        view.update(calls: [active], dictations: [], canProcess: true, isLoading: false, error: nil)
        precondition(!process.isEnabled && !open.isEnabled, "raw audio must not be opened as a result")
        switchKind(1)
        precondition(table.numberOfRows == 0)
        switchKind(0)
        view.update(calls: [raw], dictations: [], canProcess: true, isLoading: false, error: nil)
        select(0)
        var duplicate = done
        duplicate["id"] = "/output/another"
        duplicate["out_dir"] = "/output/another"
        duplicate["open_path"] = "/output/another/transcript.txt"
        view.update(calls: [done, duplicate], dictations: [], canProcess: true, isLoading: false, error: nil)
        precondition(table.numberOfRows == 2 && table.selectedRow == -1 && !open.isEnabled,
                     "ambiguous outputs must not silently select a different result")
        window.close()
        print("native history view tests passed")
    }
}
