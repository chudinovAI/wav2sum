import AppKit

struct MainWindowTest {
    private static func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    private static func button(_ app: AppDelegate, _ id: String) -> NSButton {
        descendants(app.mainWindow.window.contentView!).compactMap { $0 as? NSButton }
            .first { $0.identifier?.rawValue == id }!
    }

    private static func text(_ app: AppDelegate, _ id: String) -> String {
        descendants(app.mainWindow.window.contentView!).compactMap { $0 as? NSTextField }
            .first { $0.identifier?.rawValue == id }!.stringValue
    }

    private static func recordMenu(_ app: AppDelegate) -> NSMenuItem {
        let menu = NSMenu()
        app.menuNeedsUpdate(menu)
        return menu.items.first { $0.action == NSSelectorFromString("toggleRecording") }!
    }

    static func run() {
        guard !NSScreen.screens.isEmpty else {
            print("Main window test requires a graphical macOS session")
            exit(77)
        }
        do {
            let starts = DaemonProcess.starts
            let app = AppHandoffTest.make(devWatchMode: false, completed: false, showWindow: true)
            precondition(!app.mainWindow.window.isVisible)
            precondition(app.daemon.calls.isEmpty && DaemonProcess.starts == starts, "first run must await Apply")
            app.setupWindow.onApplied?(["completed": true, "mode": "cloud"], false)
            precondition(app.mainWindow.window.isVisible && button(app, "record-call").isEnabled)
            app.mainWindow.window.close()
            AppHandoffTest.invoke(app, "openMainWindow")
            precondition(app.mainWindow.window.isVisible, "closing must not prevent reopening")
            app.mainWindow.window.close()
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false, snapshot: ["state": "loading"], showWindow: true)
            let record = button(app, "record-call")
            precondition(!record.isEnabled && !recordMenu(app).isEnabled)
            app.daemon.onEvent?(["event": "state", "state": "ready"])
            precondition(record.isEnabled && recordMenu(app).isEnabled)
            record.performClick(nil)
            record.performClick(nil)
            precondition(app.daemon.calls.filter { $0 == "record_start" }.count == 1)
            precondition(!record.isEnabled && !recordMenu(app).isEnabled)
            app.daemon.onEvent?(["event": "recording", "active": true, "path": "/synthetic/call.wav"])
            precondition(!record.isEnabled, "an event must not release the pending ACK guard")
            app.daemon.acknowledge()
            precondition(record.isEnabled && record.title == recordMenu(app).title)
            app.daemon.onEvent?(["event": "level", "seconds": 61.0])
            precondition(text(app, "recording-time") == "01:01")
            record.performClick(nil)
            record.performClick(nil)
            precondition(app.daemon.calls.filter { $0 == "record_stop" }.count == 1)
            app.daemon.onEvent?(["event": "recording", "active": false, "path": "/synthetic/call.wav"])
            app.daemon.acknowledge(["ok": true, "path": "/synthetic/call.wav"])
            precondition(record.isEnabled && text(app, "recording-message").contains("/synthetic/call.wav"))
            precondition(!app.daemon.calls.contains("process"), "stop must not request processing")
            record.performClick(nil)
            app.daemon.acknowledge(["ok": false, "error": "synthetic microphone refusal"])
            precondition(record.isEnabled && text(app, "recording-message").contains("synthetic microphone refusal"))
            record.performClick(nil)
            app.daemon.loseConnection()
            precondition(!record.isEnabled && !recordMenu(app).isEnabled)
            precondition(text(app, "recording-state").contains("unknown"))
            app.daemon.snapshot = ["recording": ["path": "/synthetic/active.wav", "seconds": 125.0, "auto": "synthetic.call.app"]]
            app.tick()
            precondition(record.isEnabled && text(app, "recording-time") == "02:05")
            precondition(text(app, "recording-state").contains("synthetic.call.app"))
            app.mainWindow.window.close()
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false, snapshot: ["state": "failed", "error": "synthetic model failure"])
            precondition(!button(app, "record-call").isEnabled)
            precondition(text(app, "connection-state").contains("synthetic model failure"))
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false, cloud: false)
            precondition(text(app, "connection-state").hasPrefix("Local"))
            precondition(text(app, "processing-state").contains("off"))
            app.daemon.snapshot["auto_process"] = true
            app.setupWindow.onApplied?(["completed": true, "mode": "local"], false)
            precondition(text(app, "processing-state").contains("enabled"), "Apply must refresh live processing status")
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false)
            button(app, "background-process").performClick(nil)
            app.daemon.acknowledge()
            app.daemon.loseConnection()
            precondition(!button(app, "record-call").isEnabled)
            precondition(button(app, "background-process").isEnabled)
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false, cloud: false, showWindow: true)
            let window = app.mainWindow.window
            window.setContentSize(window.contentMinSize)
            let record = button(app, "record-call")
            record.performClick(nil)
            app.daemon.acknowledge(["ok": false, "error": String(repeating: "The recording device is unavailable. ", count: 60)])
            app.dictation.onError?(String(repeating: "The dictation provider is unavailable. ", count: 60))
            let content = window.contentView!
            content.layoutSubtreeIfNeeded()
            precondition(content.bounds.contains(record.convert(record.bounds, to: content)),
                         "the primary recording action must remain reachable at the minimum window size")
            for id in ["recording-message", "dictation-message"] {
                let field = descendants(content).compactMap { $0 as? NSTextField }
                    .first { $0.identifier?.rawValue == id }!
                let required = field.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: field.bounds.width, height: 10000))
                precondition(field.bounds.height + 1 >= required.height, "long feedback must wrap without clipping")
            }
            let history = descendants(content).compactMap { $0 as? HistoryView }.first!
            let table = descendants(history).compactMap { $0 as? NSTableView }.first!
            let detail = descendants(history).compactMap { $0 as? NSTextView }.first!
            let raw: JSON = ["id": "/recordings/a.wav", "title": "Call A", "audio": "/recordings/a.wav",
                             "out_dir": NSNull(), "open_path": NSNull(), "state": "raw",
                             "stage": "", "error": "", "duration_sec": NSNull(), "speakers": []]
            history.update(calls: [raw], dictations: [], canProcess: true, isLoading: false, error: nil)
            window.makeFirstResponder(table)
            window.selectNextKeyView(nil)
            table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
            window.makeFirstResponder(table)
            for _ in 0..<descendants(content).count {
                window.selectNextKeyView(nil)
                if window.firstResponder === detail { break }
            }
            precondition(window.firstResponder === detail, "revealing history details must add them to keyboard navigation")
            window.close()
        }
        do {
            let app = AppHandoffTest.make(devWatchMode: false, snapshot: ["calls_enabled": false])
            let record = button(app, "record-call")
            let kind = descendants(app.mainWindow.history).compactMap { $0 as? NSSegmentedControl }.first!
            precondition(record.isHiddenOrHasHiddenAncestor && kind.isHiddenOrHasHiddenAncestor)
            precondition(!text(app, "dictation-state").isEmpty && app.dictation.isEnabled)
            precondition(!descendants(app.mainWindow.window.contentView!).contains {
                $0.identifier?.rawValue == "calls-enabled"
            }, "call configuration belongs only in Settings")
            let menu = NSMenu(); app.menuNeedsUpdate(menu)
            precondition(!menu.items.contains {
                $0.action == NSSelectorFromString("toggleRecording") ||
                $0.action == NSSelectorFromString("summarizeLastMeeting")
            })
            app.daemon.snapshot["calls_enabled"] = true
            app.setupWindow.onApplied?(["completed": true, "mode": "cloud", "calls_enabled": true], false)
            precondition(!record.isHiddenOrHasHiddenAncestor && !kind.isHiddenOrHasHiddenAncestor)
            precondition(app.dictation.isEnabled)
            app.daemon.snapshot["calls_enabled"] = false
            app.setupWindow.onApplied?(["completed": true, "mode": "cloud", "calls_enabled": false], false)
            precondition(record.isHiddenOrHasHiddenAncestor && kind.isHiddenOrHasHiddenAncestor)
            precondition(app.dictation.isEnabled)
        }
        print("native main window tests passed")
    }
}
