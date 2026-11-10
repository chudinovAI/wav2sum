import AppKit

struct HistoryFlowTest {
    private static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }
    private static func find<T: NSView>(_ app: AppDelegate, _ id: String, as type: T.Type) -> T {
        descendants(app.mainWindow.window.contentView!).compactMap { $0 as? T }
            .first { $0.identifier?.rawValue == id }!
    }
    static func run() {
        guard !NSScreen.screens.isEmpty else {
            print("History flow requires a graphical macOS session")
            exit(77)
        }
        let audio = "/synthetic/custom recordings/call.wav"
        let output = "/synthetic/custom output/call"
        let raw: JSON = ["id": audio, "path": audio, "audio": audio, "title": "A call", "state": "raw"]
        let done: JSON = ["id": output, "path": output, "audio": audio, "title": "A call", "state": "done",
                          "out_dir": output, "open_path": output + "/transcript.txt"]
        let first: JSON = ["id": "first", "time": "2026-11-10", "raw": "first raw", "text": "first text", "mode": "chat"]
        let second: JSON = ["id": "second", "time": "2026-11-10", "raw": "second raw", "text": "second text", "mode": "chat"]
        var opened: [URL] = []
        let app = AppHandoffTest.make(devWatchMode: false, deferHistory: true, openURL: { opened.append($0); return true })
        let daemon = app.daemon
        let table = find(app, "history-table", as: NSTableView.self)
        let kind = find(app, "history-kind", as: NSSegmentedControl.self)
        let process = find(app, "history-process", as: NSButton.self)
        let open = find(app, "history-open", as: NSButton.self)
        let refresh = find(app, "history-refresh", as: NSButton.self)
        let detail = find(app, "history-detail", as: NSTextView.self)
        let stale = daemon.historyReplies.removeFirst()
        daemon.onEvent?(["event": "dictation", "text": "event during snapshot"])
        stale(["ok": true, "calls": [raw], "dictations": [first]])
        precondition(table.numberOfRows == 0 && !process.isEnabled, "stale snapshot must not become actionable")
        daemon.historyReplies.removeFirst()(["ok": true, "calls": [raw], "dictations": [first, second]])
        table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        process.performClick(nil)
        process.performClick(nil)
        precondition(daemon.calls.filter { $0 == "process" }.count == 1)
        precondition(daemon.parameters.last?["path"] as? String == audio)
        precondition(!process.isEnabled)
        daemon.onEvent?(["event": "job", "id": 1, "audio": audio, "state": "queued"])
        daemon.acknowledge(["ok": true, "job": ["id": 1, "audio": audio, "state": "queued"]])
        daemon.historyReplies.removeFirst()(["ok": true, "calls": [raw], "dictations": [first]])
        daemon.historyReplies.removeFirst()(["ok": true, "calls": [done], "dictations": [first, second]])
        precondition(table.numberOfRows == 1 && table.selectedRow == 0 && !process.isEnabled && open.isEnabled)
        open.performClick(nil)
        precondition(opened.map(\.path) == [output + "/transcript.txt"])
        precondition(!daemon.calls.contains("dictate") && !daemon.calls.contains("record_start"))
        kind.selectedSegment = 1
        _ = kind.sendAction(kind.action!, to: kind.target)
        table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        precondition(detail.string.contains("first text"))
        daemon.onEvent?(["event": "history_pruned", "deleted": 1])
        daemon.historyReplies.removeFirst()(["ok": true, "calls": [done], "dictations": [second]])
        precondition(table.numberOfRows == 1 && table.selectedRow == -1 && detail.string.isEmpty)
        refresh.performClick(nil)
        let oldConnection = daemon.historyReplies.removeFirst()
        daemon.loseConnection()
        precondition(!process.isEnabled && !open.isEnabled)
        app.tick()
        daemon.historyReplies.removeFirst()(["ok": true, "calls": [done], "dictations": [second]])
        oldConnection(["ok": true, "calls": [raw], "dictations": [first, second]])
        precondition(table.numberOfRows == 1 && table.selectedRow == -1)
        refresh.performClick(nil)
        daemon.historyReplies.removeFirst()(["ok": false, "error": "synthetic history read failure"])
        precondition(find(app, "history-status", as: NSTextField.self).stringValue.contains("synthetic history read failure"))
        precondition(!process.isEnabled && !open.isEnabled)
        app.dictation.onError?("synthetic microphone failure")
        precondition(find(app, "dictation-message", as: NSTextField.self).stringValue.contains("synthetic microphone failure"))
        app.dictation.phase = .starting
        precondition(find(app, "dictation-message", as: NSTextField.self).stringValue.isEmpty)
        app.dictation.phase = .processing
        precondition(find(app, "dictation-state", as: NSTextField.self).stringValue.contains("Processing"))
        app.dictation.onResult?(false)
        precondition(find(app, "dictation-message", as: NSTextField.self).stringValue.contains("clipboard"))
        precondition(find(app, "permission-state", as: NSTextField.self).stringValue == "synthetic permissions")
        app.mainWindow.window.close()
        let rejected = AppHandoffTest.make(devWatchMode: false, history: ["calls": [done]])
        let rejectedTable = find(rejected, "history-table", as: NSTableView.self)
        let rejectedOpen = find(rejected, "history-open", as: NSButton.self)
        rejectedTable.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        precondition(rejectedOpen.isEnabled)
        rejected.daemon.snapshot["cloud"] = false
        rejected.setupWindow.onApplied?(["completed": true, "mode": "cloud"], false)
        precondition(!rejectedOpen.isEnabled, "mode rejection must disable cached history actions")
        rejected.mainWindow.window.close()
        print("native history flow tests passed")
    }
}
