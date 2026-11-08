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
    func connect(path: String) -> Bool { isConnected = connectResult; return connectResult }
    func disconnect() { isConnected = false }
    func request(_ cmd: String, _ params: JSON = [:], reply: ((JSON) -> Void)? = nil) {
        calls.append(cmd)
        if cmd == "subscribe" { reply?(["ok": true, "cloud": cloud, "state": "ready"]); return }
        if let reply { replies.append(reply) }
    }
    func acknowledge(_ reply: JSON = ["ok": true]) { replies.removeFirst()(reply) }
    func loseConnection() {
        isConnected = false
        let callbacks = replies; replies.removeAll()
        callbacks.forEach { $0(["ok": false, "error": "connection lost"]) }
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
    func refresh(_ completion: (JSON?) -> Void) { completion(["completed": true, "mode": "cloud"]) }
    func show(requestCloud: Bool = false) {}
}
final class SharingWindow { func show() {} }
enum DaemonProcess {
    static let socketPath = "unused"
    static var starts = 0
    static func start(cloud: Bool) { starts += 1 }
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
    static func make(terminate: @escaping () -> Void = { preconditionFailure("unexpected app termination") }) -> AppDelegate {
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
        delegate = AppDelegate(devWatchMode: true, effects: effects)
        delegate.applicationDidFinishLaunching(Notification(name: Notification.Name("test")))
        return delegate
    }
    static func invoke(_ delegate: AppDelegate, _ action: String) {
        _ = delegate.perform(NSSelectorFromString(action))
    }
    static func main() {
        _ = NSApplication.shared
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
            invoke(app, "toggleDaemon") // Turn Off while the previous start has not connected.
            invoke(app, "toggleDaemon") // An explicit retry must not wait for the passive 90-second throttle.
            precondition(DaemonProcess.starts == before + 2, "explicit rapid retry bypasses the passive start throttle")
            app.handoffTestTick()
            precondition(DaemonProcess.starts == before + 2, "retry permission remains one shot")
        }
        do {
            let app = make()
            app.handoffTestSetup.onApplying?(true)
            app.handoffTestSetup.onApplying?(false)
            precondition(app.handoffTestWatch("prepare") == "busy", "Settings failure without an Apply ACK is uncertain")
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
