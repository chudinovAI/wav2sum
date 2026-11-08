import AppKit

typealias JSON = [String: Any]
final class DaemonClient { var cloud = false; func request(_ command: String, _ params: JSON = [:], reply: ((JSON) -> Void)? = nil) {} }
enum Log { static func write(_ message: String) {} }

@main
struct DictationHandoffTest {
    static func main() {
        var selection: ((String?) -> Void)?
        var response: ((JSON) -> Void)?
        var probe: (() -> Void)?
        var duringInsert: (() -> Void)?
        var duringStart: (() -> Void)?
        var startedBusy = false
        var starts = 0
        var stops = 0
        var failStart = false
        var controller: DictationController!
        var requests: [(String, JSON)] = []
        var chunks: [[Float]] = []
        var copied: [String] = []
        var states: [String] = []
        let effects = DictationController.Effects(
            audio: (start: {
                starts += 1
                startedBusy = controller.busyForHandoff
                duringStart?()
                if failStart { throw NSError(domain: "synthetic", code: 1) }
            }, stop: { stops += 1; return Array(repeating: 0, count: 5000) },
               take: { chunks.isEmpty ? [] : chunks.removeFirst() }),
            context: { around in around ? ["before": "left", "after": "right"] : ["app": "synthetic", "title": "fixture"] },
            selectedText: { selection = $0 },
            target: (begin: {}, invalidate: {}, stop: {},
                     finish: { { _, _, _, _, done in probe = done; duringInsert?() } }),
            hud: (show: { state in
                switch state { case .listening?: states.append("listening"); case .thinking?: states.append("thinking"); case .success?: states.append("success"); case nil: states.append("hidden") }
            }, preview: { _ in }, contains: { _ in false }),
            request: { command, params, reply in
                requests.append((command, params))
                if command == "dictate" { response = reply }
            },
            copy: { copied.append($0) }
        )
        let corrections = CorrectionWatcher(prepare: { "synthetic" }, focusedElement: { nil })
        controller = DictationController(daemon: DaemonClient(), effects: effects, corrections: corrections)
        controller.isEnabled = true
        precondition(!controller.busyForHandoff)
        failStart = true
        controller.hotkey.updateKey(pressed: true, shift: true, control: false)
        precondition(startedBusy && controller.busyForHandoff)
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "failed microphone startup still waits for selection restoration")
        selection?(nil)
        precondition(!controller.busyForHandoff)
        failStart = false
        controller.hotkey.updateKey(pressed: true, shift: true, control: false)
        precondition(startedBusy && controller.busyForHandoff, "starting must be busy before external effects")
        controller.cancel()
        precondition(controller.busyForHandoff, "held key and selection restoration remain pending after cancellation")
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "selection callback may outlive cancellation")
        selection?(nil)
        precondition(!controller.busyForHandoff)

        duringStart = {
            controller.cancel()
            controller.hotkey.updateKey(pressed: false, shift: false, control: false)
            precondition(controller.busyForHandoff, "reentrant cancellation must not expose idle during startup")
        }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        duringStart = nil
        controller.cancel()
        precondition(!controller.busyForHandoff)

        let stoppedBeforeReentrantCancel = stops
        duringStart = {
            controller.cancel()
            controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        duringStart = nil
        precondition(!controller.busyForHandoff, "cancelled startup must not restart recording")
        precondition(stops == stoppedBeforeReentrantCancel + 1, "cancelled startup must stop the acquired microphone")

        let stoppedBeforeRelease = stops
        duringStart = { controller.hotkey.updateKey(pressed: false, shift: false, control: false) }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        duringStart = nil
        precondition(!controller.busyForHandoff, "release during startup must not leave a recording running")
        precondition(stops == stoppedBeforeRelease + 1)

        var busyWhenUIStopped = false
        controller.onBusyChange = { busy in
            if !busy { busyWhenUIStopped = controller.busyForHandoff }
        }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "processing")
        response?(["text": "synthetic"])
        precondition(busyWhenUIStopped, "callback must not expose transient idle before post-processing")
        precondition(controller.busyForHandoff, "pending insertion probe and correction tracking")
        probe?()
        precondition(controller.busyForHandoff, "correction tracking outlives probe")
        controller.corrections.finish()
        precondition(!controller.busyForHandoff)

        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["text": "synthetic"])
        controller.corrections.finish()
        precondition(controller.busyForHandoff, "probe outlives correction tracking")
        probe?()
        precondition(!controller.busyForHandoff)

        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.cancel()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["text": "late result"])
        precondition(!controller.busyForHandoff, "cancelled response must not start post-processing")

        duringInsert = {
            controller.cancel()
            probe?()
            precondition(controller.busyForHandoff, "a reentrant cancellation cannot expose idle inside the reply")
        }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["text": "synthetic"])
        duringInsert = nil
        controller.corrections.finish()
        precondition(!controller.busyForHandoff)

        let startsBeforeNestedPress = starts
        duringInsert = {
            controller.cancel()
            probe?()
            controller.hotkey.updateKey(pressed: true, shift: false, control: false)
            controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        }
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["text": "synthetic"])
        duringInsert = nil
        controller.corrections.finish()
        precondition(starts == startsBeforeNestedPress + 1, "a nested press must not start during reply completion")
        precondition(!controller.busyForHandoff)

        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.hotkey.updateKey(pressed: false, shift: false, control: false) // A short tap prepares hands-free mode.
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "hands-free remains busy after release")
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "hands-free processing remains busy")
        response?([:])
        precondition(!controller.busyForHandoff)
        // The same flow must stream the final chunk before the dictate request, not a test-only shortcut.
        chunks = [[0.25, -0.25], [0.5]]
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        let audio = requests[requests.count - 2], final = requests.last!
        precondition(audio.0 == "dictate_audio" && audio.1["first"] as? Bool == true)
        precondition(audio.1["app"] as? String == "synthetic")
        precondition(final.0 == "dictate" && final.1["streamed"] as? Bool == true)
        precondition(final.1["audio"] as? String == Data([0xff, 0x3f]).base64EncodedString())
        precondition(final.1["before"] as? String == "left" && final.1["after"] as? String == "right")
        response?(["text": "boundary equivalent"])
        precondition(copied.last == "boundary equivalent" && states.suffix(3) == ["listening", "thinking", "success"])
        probe?()
        controller.corrections.finish()
        precondition(!controller.busyForHandoff)

        // Exercise the actual correction waiting and tracking timers using a fake field identity.
        let field = NSObject()
        var value = "before synthetic after", focused = true, reads = 0
        var correction: (String, String)?
        let watcher = CorrectionWatcher(prepare: { "synthetic" },
                                        focusedElement: { focused ? unsafeBitCast(field, to: AXUIElement.self) : nil },
                                        text: { _ in reads += 1; return value })
        watcher.onCorrection = { precondition(watcher.busyForHandoff); correction = ($0, $1) }
        watcher.track("synthetic")
        precondition(watcher.busyForHandoff)
        RunLoop.current.run(until: Date().addingTimeInterval(0.55))
        let waitingReads = reads
        precondition(waitingReads > 0, "the real waiting timer must find the synthetic field")
        value = "before edited after"
        RunLoop.current.run(until: Date().addingTimeInterval(1.05))
        precondition(reads > waitingReads, "the real tracking timer must observe the edit")
        focused = false // finish must use the last tracked edit, not read a different focused field.
        watcher.finish()
        precondition(correction?.0 == "synthetic" && correction?.1 == "edited")
        precondition(!watcher.busyForHandoff)
        let finishedReads = reads
        RunLoop.current.run(until: Date().addingTimeInterval(1.05))
        precondition(reads == finishedReads, "finish cancels both correction timers")
        print("native dictation handoff tests passed")
    }
}
