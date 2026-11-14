import AppKit
import Foundation

typealias JSON = [String: Any]

final class DaemonClient {
    var onEvent: ((JSON) -> Void)?
    var onDisconnect: (() -> Void)?
    var isConnected = false
    var cloud = true
    var calls: [String] = []
    var replies: [(JSON) -> Void] = []
    var connectResult = true
    var snapshot: JSON = [:]
    var parameters: [JSON] = []
    var history: JSON = ["ok": true, "calls": [JSON](), "dictations": [JSON]()]
    var deferHistory = false
    var historyReplies: [(JSON) -> Void] = []
    var subscribed = false
    func emit(_ event: JSON) { if subscribed { onEvent?(event) } }
    func connect(path: String) -> Bool { isConnected = connectResult; return connectResult }
    func disconnect() { isConnected = false }
    func request(_ cmd: String, _ params: JSON = [:], reply: ((JSON) -> Void)? = nil) {
        calls.append(cmd)
        parameters.append(params)
        if cmd == "history" {
            if deferHistory, let reply { historyReplies.append(reply) }
            else { reply?(history) }
            return
        }
        if cmd == "subscribe" || cmd == "status" {
            let actualCloud = snapshot["cloud"] as? Bool ?? cloud
            if actualCloud != cloud {
                if cmd == "subscribe" { subscribed = false }
                reply?(["ok": false, "error": "daemon mode mismatch"])
                return
            }
            if cmd == "subscribe" { subscribed = true }
            reply?(["ok": true, "cloud": actualCloud, "state": "ready"].merging(snapshot) { _, new in new })
            return
        }
        if let reply { replies.append(reply) }
    }
    func acknowledge(_ reply: JSON = ["ok": true]) { replies.removeFirst()(reply) }
    func loseConnection() {
        subscribed = false
        isConnected = false
        let callbacks = replies; replies.removeAll()
        callbacks.forEach { $0(["ok": false, "error": "connection lost"]) }
        let historyCallbacks = historyReplies; historyReplies.removeAll()
        historyCallbacks.forEach { $0(["ok": false, "error": "connection lost"]) }
        onDisconnect?()
    }
}
final class Hotkey {
    enum Key: String { case rightOption = "right_option"; var label: String { "test" } }
    var key = Key.rightOption
    var isActive = true
    func start() -> Bool { true }
}
final class Corrections { var onCorrection: ((String, String) -> Void)? }
final class DictationController {
    var isEnabled = false
    var busyForHandoff = false
    var onBusyChange: ((Bool) -> Void)?
    enum Phase { case idle, starting, recording, handsFree, processing }
    var phase = Phase.idle { didSet { onStateChange?(phase) } }
    var onStateChange: ((Phase) -> Void)?
    var onError: ((String) -> Void)?
    var onResult: ((Bool) -> Void)?
    let hotkey = Hotkey()
    let corrections = Corrections()
    init(daemon: DaemonClient) {}
    func disconnected() {}
    func receive(event: JSON) {}
}
final class SetupWindow {
    var onApplying: ((Bool) -> Bool)?
    var onApplied: ((JSON, Bool) -> Void)?
    init(initialCloud: Bool) {}
    static func permissionSummary() -> String { "synthetic permissions" }
    var completed = true
    var mode = "cloud"
    var savedCallsEnabled = true
    func refresh(_ completion: (JSON?) -> Void) { completion(["completed": completed, "mode": mode, "calls_enabled": savedCallsEnabled]) }
    func show(requestCloud: Bool = false) {}
}
final class SharingWindow { func show() {} }
enum DaemonProcess {
    static let socketPath = "unused"
    static var starts = 0
    static var launchExit: (() -> Void)?
    static func start(cloud: Bool, onExit: @escaping () -> Void) {
        starts += 1
        launchExit = onExit
    }
}
enum Log { static let directory = "unused"; static func write(_ message: String) {} }
enum Logo { static var menuBarImage: NSImage? { nil } }

private var watchResponses: [ObjectIdentifier: String] = [:]
extension AppDelegate {
    var handoffTestDaemon: DaemonClient { daemon }
    var handoffTestSetup: SetupWindow { setupWindow }
    func handoffTestTick() { tick() }
    func handoffTestCorrection(_ original: String, _ edited: String) { dictation.corrections.onCorrection?(original, edited) }
    func handoffTestWatch(_ command: String) -> String {
        watchResponses[ObjectIdentifier(self)] = ""
        devWatch?.handle(command)
        return watchResponses[ObjectIdentifier(self)] ?? ""
    }
}

struct AppHandoffTest {
    static func make(devWatchMode: Bool = true, completed: Bool = true, cloud: Bool = true, snapshot: JSON = [:],
                     showWindow: Bool = false,
                     history: JSON = [:], deferHistory: Bool = false,
                     openURL: @escaping (URL) -> Bool = { _ in true },
                     terminate: @escaping () -> Void = { preconditionFailure("unexpected app termination") }) -> AppDelegate {
        var delegate: AppDelegate!
        var wanted = true
        var effects = AppDelegate.Effects()
        effects.wanted = ({ wanted }, { wanted = $0 })
        effects.respond = { watchResponses[ObjectIdentifier(delegate)] = $0 }
        effects.read = { _ in }
        effects.terminate = terminate
        effects.configureNotifications = { _ in }
        effects.notify = { _, _, _ in }
        effects.scheduleTick = { _ in nil }
        effects.showMainWindow = { if showWindow { $0.window.orderFront(nil) } }
        effects.openURL = openURL
        delegate = AppDelegate(devWatchMode: devWatchMode, effects: effects)
        delegate.setupWindow.completed = completed
        delegate.setupWindow.mode = cloud ? "cloud" : "local"
        delegate.setupWindow.savedCallsEnabled = snapshot["calls_enabled"] as? Bool ?? true
        delegate.daemon.snapshot = snapshot
        delegate.daemon.deferHistory = deferHistory
        delegate.daemon.history.merge(history) { _, new in new }
        delegate.applicationDidFinishLaunching(Notification(name: Notification.Name("test")))
        return delegate
    }
    static func invoke(_ delegate: AppDelegate, _ action: String) {
        _ = delegate.perform(NSSelectorFromString(action))
    }
    static func main() {
        _ = NSApplication.shared
        if CommandLine.arguments.contains("--preview") {
            NSApp.setActivationPolicy(.regular)
            let app = make(devWatchMode: false, snapshot: ["calls_enabled": !CommandLine.arguments.contains("--calls-off")],
                           showWindow: true,
                           history: ["dictations": [["id": "preview-dictation", "text": "Synthetic dictation history",
                                                     "time": "Preview", "mode": "local", "raw": "Synthetic dictation history"]]])
            app.mainWindow.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            NSApp.run()
            return
        }
        if CommandLine.arguments.contains("--main-window") { MainWindowTest.run(); return }
        if CommandLine.arguments.contains("--history-view") { HistoryViewTest.run(); return }
        if CommandLine.arguments.contains("--history-flow") { HistoryFlowTest.run(); return }
        for (recording, command) in [(false, "record_start"), (true, "record_stop")] {
            let app = make(), daemon = app.handoffTestDaemon
            if recording { daemon.onEvent?(["event": "recording", "active": true]) }
            invoke(app, "toggleRecording")
            precondition(daemon.calls.last == command)
            precondition(app.handoffTestWatch("prepare") == "busy", "record RPC must block handoff until ACK")
            daemon.acknowledge()
            precondition(app.handoffTestWatch("prepare") == "ready")
            let count = daemon.calls.count
            invoke(app, "toggleRecording")
            invoke(app, "summarizeLastMeeting")
            invoke(app, "toggleDaemon")
            invoke(app, "quit")
            precondition(daemon.calls.count == count, "prepared must reject new mutating actions")
            precondition(app.handoffTestWatch("resume") == "resumed")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "summarizeLastMeeting")
            precondition(daemon.calls.last == "summarize_last_meeting")
            precondition(app.handoffTestWatch("prepare") == "busy")
            daemon.acknowledge(["ok": true, "job": ["id": 1]])
            precondition(app.handoffTestWatch("prepare") == "ready")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "toggleDaemon")
            precondition(daemon.calls.last == "shutdown")
            precondition(app.handoffTestWatch("prepare") == "busy")
            daemon.acknowledge(["ok": false, "error": "refused"])
            precondition(app.handoffTestWatch("prepare") == "ready", "confirmed rejection is not uncertain")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "toggleRecording")
            daemon.loseConnection()
            precondition(app.handoffTestWatch("prepare") == "busy", "uncertain disconnect must fail closed")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            daemon.isConnected = false
            daemon.connectResult = false
            let before = DaemonProcess.starts
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "cloud"], false)
            precondition(DaemonProcess.starts == before + 1, "explicit Apply starts an absent daemon")
            app.handoffTestTick()
            precondition(DaemonProcess.starts == before + 1, "permission is one shot")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "cloud"], false)
            daemon.isConnected = false
            daemon.connectResult = false
            let before = DaemonProcess.starts
            app.handoffTestTick()
            precondition(DaemonProcess.starts == before, "connected nonrestart Apply must not authorize a future crash restart")
            invoke(app, "toggleDaemon")
            precondition(DaemonProcess.starts == before + 1, "Turn On authorizes an absent daemon")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "summarizeLastMeeting")
            invoke(app, "summarizeLastMeeting")
            precondition(daemon.calls.filter { $0 == "summarize_last_meeting" }.count == 1)
            daemon.loseConnection()
            precondition(app.handoffTestWatch("prepare") == "busy", "lost summary ACK is uncertain")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "toggleDaemon")
            daemon.loseConnection()
            precondition(app.handoffTestWatch("prepare") == "busy", "lost shutdown ACK is uncertain")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            app.handoffTestCorrection("old", "new")
            precondition(app.handoffTestWatch("prepare") == "busy", "correction ACK must remain protected")
            daemon.acknowledge(["ok": false])
            precondition(app.handoffTestWatch("prepare") == "busy", "failed correction must remain protected")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            precondition(app.handoffTestWatch("prepare") == "ready")
            daemon.isConnected = false
            daemon.connectResult = false
            let before = DaemonProcess.starts, count = daemon.calls.count
            app.handoffTestTick()
            invoke(app, "toggleDaemon")
            precondition(DaemonProcess.starts == before && daemon.calls.count == count)
            app.handoffTestSetup.onApplying?(true)
            precondition(app.handoffTestWatch("exit") == "")
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "cloud"], false)
            precondition(DaemonProcess.starts == before)
            precondition(app.handoffTestWatch("prepare") == "busy", "deferred explicit Apply cannot be lost")
            _ = app.handoffTestWatch("resume")
            app.handoffTestTick()
            precondition(DaemonProcess.starts == before + 1)
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            daemon.isConnected = false
            daemon.connectResult = true
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "cloud"], false)
            daemon.loseConnection()
            daemon.connectResult = false
            let before = DaemonProcess.starts
            app.handoffTestTick()
            precondition(DaemonProcess.starts == before, "successful connect consumes unused start permission")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "toggleDaemon")
            daemon.acknowledge()
            daemon.loseConnection()
            daemon.connectResult = false
            let before = DaemonProcess.starts
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "cloud"], false)
            precondition(DaemonProcess.starts == before + 1, "Apply after Turn Off must enable the explicitly requested daemon")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            daemon.loseConnection()
            daemon.connectResult = false
            let before = DaemonProcess.starts
            invoke(app, "toggleDaemon")
            precondition(DaemonProcess.starts == before + 1)
            invoke(app, "toggleDaemon") // A launched process cannot be cancelled by flipping the preference.
            precondition(app.handoffTestSetup.onApplying?(true) == false,
                         "a pending launch must reject offline mode and calls changes")
            precondition(DaemonProcess.starts == before + 1)
            daemon.connectResult = true
            daemon.snapshot = ["cloud": true, "calls_enabled": true]
            app.handoffTestTick()
            precondition(daemon.isConnected && app.dictation.isEnabled,
                         "the original daemon must remain reachable without a mode mismatch")
            precondition(app.handoffTestSetup.onApplying?(true) == true,
                         "settings may change after the launched daemon is ready")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            invoke(app, "toggleDaemon")
            daemon.acknowledge()
            daemon.loseConnection()
            daemon.connectResult = false
            precondition(app.handoffTestSetup.onApplying?(true) == true,
                         "a confirmed stopped daemon still permits offline settings")
        }
        do {
            let app = make(), daemon = app.handoffTestDaemon
            daemon.loseConnection()
            daemon.connectResult = false
            invoke(app, "toggleDaemon")
            DaemonProcess.launchExit?()
            invoke(app, "toggleDaemon")
            precondition(app.handoffTestSetup.onApplying?(true) == true,
                         "a confirmed failed launch can be turned off and configured")
        }
        do {
            let app = make(snapshot: ["cloud": false])
            precondition(!app.daemon.subscribed && !app.dictation.isEnabled)
            precondition(app.handoffTestSetup.onApplying?(true) == true,
                         "connected mode rejection must allow explicit Settings recovery")
            app.handoffTestSetup.onApplied?(["completed": true, "mode": "local"], false)
            precondition(app.daemon.subscribed && app.daemon.calls.filter { $0 == "subscribe" }.count == 2,
                         "recovery must really subscribe, not merely request status")
            app.daemon.emit(["event": "state", "state": "failed", "error": "synthetic"])
            precondition(!app.dictation.isEnabled)
            app.daemon.emit(["event": "state", "state": "ready"])
            precondition(app.dictation.isEnabled, "events must reach the recovered subscription")
        }
        do {
            let app = make(completed: false)
            precondition(app.handoffTestSetup.onApplying?(true) == true,
                         "first-run settings can be saved before the daemon is started")
        }
        do {
            let app = make()
            app.handoffTestSetup.onApplying?(true)
            app.handoffTestSetup.onApplying?(false)
            precondition(app.handoffTestWatch("prepare") == "busy", "Settings failure without an Apply ACK is uncertain")
            app.daemon.onEvent?(["event": "state", "state": "ready"])
            precondition(!app.dictation.isEnabled, "an ambiguous settings ACK must not re-enable dictation")
        }
        do {
            var exits = 0
            let app = make(terminate: { exits += 1 }), daemon = app.handoffTestDaemon
            app.handoffTestCorrection("old", "new")
            precondition(app.handoffTestWatch("prepare") == "busy")
            _ = app.handoffTestWatch("exit")
            precondition(exits == 0, "dev-app exit waits for correction ACK")
            daemon.acknowledge()
            precondition(app.handoffTestWatch("prepare") == "ready", "successful correction ACK releases handoff")
            let count = daemon.calls.count
            _ = app.handoffTestWatch("exit")
            precondition(exits == 1 && daemon.calls.count == count, "watcher exits only dev-app, without daemon shutdown")
        }
        do {
            let app = make()
            app.dictation.busyForHandoff = true // Includes startup before first PCM, held key, selection/probe and correction tracking.
            precondition(app.handoffTestSetup.onApplying?(true) == false, "native dictation lifetime must veto configure")
            precondition(app.dictation.isEnabled, "a veto must not cancel or disable the active dictation")
            app.dictation.busyForHandoff = false
            precondition(app.handoffTestWatch("prepare") == "ready", "veto must not create uncertain settings state")
        }
        for correction in [true, false] {
            let app = make(), daemon = app.handoffTestDaemon
            if correction { app.handoffTestCorrection("old", "new") } else { invoke(app, "toggleRecording") }
            precondition(app.handoffTestSetup.onApplying?(true) == false, "pending native ACK must veto configure")
            daemon.acknowledge()
            precondition(app.handoffTestSetup.onApplying?(true) == true)
            precondition(!app.dictation.isEnabled, "acceptance must synchronously prevent new dictation starts")
            daemon.onEvent?(["event": "state", "state": "ready"])
            precondition(!app.dictation.isEnabled, "ready events must not reopen starts while configure is pending")
            precondition(app.handoffTestSetup.onApplying?(true) == false, "a second Apply cannot overlap")
        }
        print("native app handoff tests passed")
    }
}
