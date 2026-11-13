import AppKit
import AVFoundation

typealias JSON = [String: Any]
final class DaemonClient { var cloud = false; func request(_ command: String, _ params: JSON = [:], reply: ((JSON) -> Void)? = nil) {} }
enum Log { static func write(_ message: String) {} }

final class FakeMicrophoneDevice {
    var route = "16k"
    var failRoute: String?
    var changed: (() -> Void)?
    var inputs: [(queue: DispatchQueue, control: DispatchQueue, samples: (UnsafeBufferPointer<Float>) -> Bool,
                  failure: (Error) -> Void, changed: (Bool) -> Void)] = []
    var tails: [Int: [Float]] = [:]
    var closed = 0

    var backend: Microphone.Backend {
        Microphone.Backend(
            route: { self.route },
            observe: { queue, changed in
                self.changed = { queue.sync { changed() } }
                return { self.changed = nil }
            },
            open: { route, control, queue, samples, failure, changed in
                if self.failRoute == route { throw NSError(domain: "route", code: 48) }
                let index = self.inputs.count
                self.inputs.append((queue, control, samples, failure, changed))
                return { _ in
                    self.closed += 1
                    if let tail = self.tails.removeValue(forKey: index) {
                        queue.sync {
                            tail.withUnsafeBufferPointer {
                                precondition(!samples($0), "closing input cannot re-enqueue its tail")
                            }
                        }
                    }
                }
            })
    }

    func send(_ values: [Float], input: Int) {
        let receiver = inputs[input]
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: true)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(values.count))!
        buffer.frameLength = AVAudioFrameCount(values.count)
        let samples = buffer.floatChannelData![0]
        for (index, value) in values.enumerated() { samples[index] = value }
        receiver.queue.sync { _ = receiver.samples(UnsafeBufferPointer(start: samples, count: values.count)) }
    }
}

func microphoneTransitions() throws {
    let device = FakeMicrophoneDevice()
    let microphone = Microphone(backend: device.backend)
    var failures: [String] = []
    try microphone.start { failures.append($0.localizedDescription) }
    device.send([0.1, 0.2], input: 0)
    precondition(microphone.take() == [0.1, 0.2])
    device.route = "48k"
    device.tails[0] = [0.25]
    device.changed?()
    device.send([0.9], input: 0)
    device.send([0.3, 0.4], input: 1)
    precondition(microphone.take() == [0.25, 0.3, 0.4], "route changes preserve buffered tail and streaming cursor")
    let second = device.inputs[1]
    second.control.sync { second.changed(false) } // Same device, new hardware format.
    device.send([0.6], input: 1)
    device.send([0.5], input: 2)
    precondition(microphone.take() == [0.5])
    device.tails[2] = [0.55]
    let finished = try microphone.stop(true)
    precondition(finished == [0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.55])
    precondition(microphone.take() == [0.55], "release includes the final unstreamed tail")
    device.send([0.8], input: 2)
    precondition(microphone.take().isEmpty, "stopped input must not append")

    device.failRoute = "broken"
    try microphone.start { failures.append($0.localizedDescription) }
    let oldChanged = device.changed!
    let oldFailure = device.inputs[3].failure
    let oldQueue = device.inputs[3].queue
    device.route = "broken"
    device.changed?()
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    precondition(failures.count == 1, "route-start failure is visible")
    _ = try? microphone.stop(false)
    device.failRoute = nil
    device.route = "16k"
    try microphone.start { failures.append($0.localizedDescription) }
    oldChanged()
    oldQueue.sync { oldFailure(NSError(domain: "stale", code: 1)) }
    device.send([0.7], input: 3)
    device.send([0.5], input: 4)
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    let recovered = try microphone.stop(true)
    precondition(failures.count == 1 && recovered == [0.5], "old callbacks cannot affect a new session")
    try microphone.start { failures.append($0.localizedDescription) }
    let last = device.inputs[5]
    last.queue.sync { last.failure(NSError(domain: "late", code: 1)) }
    do {
        _ = try microphone.stop(true)
        preconditionFailure("a queued capture error must fail normal release")
    } catch { precondition((error as NSError).domain == "late") }
    try microphone.start { failures.append($0.localizedDescription) }
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    precondition(failures.count == 1, "queued failure must not cancel a replacement recording")
    device.tails[6] = [0.6]
    _ = try microphone.stop(false)
    precondition(microphone.take().isEmpty, "cancellation discards buffers delivered during close")
    try microphone.start { failures.append($0.localizedDescription) }
    let stopped = device.inputs[7]
    stopped.control.sync { stopped.changed(true) }
    let stoppedAgain = device.inputs[8]
    stoppedAgain.control.sync { stoppedAgain.changed(true) }
    RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    precondition(failures.count == 2, "repeated unexpected stops report an unrecoverable input")
    _ = try? microphone.stop(false)
}

@main
struct DictationHandoffTest {
    static func main() {
        try! microphoneTransitions()
        var selection: ((String?) -> Void)?
        var response: ((JSON) -> Void)?
        var probe: (() -> Void)?
        var duringInsert: (() -> Void)?
        var duringStart: (() -> Void)?
        var startedBusy = false
        var audioFailure: ((Error) -> Void)?
        var starts = 0
        var stops = 0
        var failFinish = false
        var failStart = false
        var pasteTargetAvailable = true
        var controller: DictationController!
        var requests: [(String, JSON)] = []
        var chunks: [[Float]] = []
        var copied: [String] = []
        var states: [String] = []
        var errors: [String] = []
        var deliveries: [Bool] = []
        let effects = DictationController.Effects(
            audio: (start: { failure in
                starts += 1
                audioFailure = failure
                startedBusy = controller.busyForHandoff
                duringStart?()
                if failStart { throw NSError(domain: "synthetic", code: 1) }
            }, stop: { drain in
                stops += 1
                if drain && failFinish { throw NSError(domain: "finish", code: 1) }
                return Array(repeating: 0, count: 5000)
            },
               take: { chunks.isEmpty ? [] : chunks.removeFirst() }),
            context: { around in around ? ["before": "left", "after": "right"] : ["app": "synthetic", "title": "fixture"] },
            selectedText: { selection = $0 },
            target: (begin: {}, invalidate: {}, stop: {},
                     finish: {
                guard pasteTargetAvailable else { return nil }
                return { _, _, _, _, done in probe = done; duringInsert?() }
            }),
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
        controller.onError = { errors.append($0) }
        controller.onResult = { deliveries.append($0) }
        precondition(!controller.busyForHandoff)
        failStart = true
        controller.hotkey.updateKey(pressed: true, shift: true, control: false)
        precondition(startedBusy && controller.busyForHandoff)
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "failed microphone startup still waits for selection restoration")
        selection?(nil)
        precondition(!controller.busyForHandoff)
        precondition(errors.count == 1 && copied.isEmpty && controller.phase == .idle)
        failStart = false
        controller.hotkey.updateKey(pressed: true, shift: true, control: false)
        precondition(startedBusy && controller.busyForHandoff, "starting must be busy before external effects")
        controller.cancel()
        precondition(controller.busyForHandoff, "held key and selection restoration remain pending after cancellation")
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.busyForHandoff, "selection callback may outlive cancellation")
        selection?(nil)
        precondition(!controller.busyForHandoff)

        let previousFailure = audioFailure!
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        previousFailure(NSError(domain: "stale", code: 1))
        precondition(controller.phase == .recording && errors.count == 1)
        audioFailure?(NSError(domain: "route", code: 48))
        precondition(controller.phase == .idle && errors.count == 2, "active route errors cancel and report")
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        precondition(controller.phase == .recording, "next dictation can start after route failure")
        controller.cancel()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        let requestsBeforeFailure = requests.count
        failFinish = true
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        precondition(controller.phase == .idle && errors.count == 3, "release reports pending capture failure")
        precondition(requests.count == requestsBeforeFailure, "partial audio must not reach recognition")
        failFinish = false
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
        pasteTargetAvailable = false
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["text": "clipboard fallback"])
        precondition(copied.last == "clipboard fallback" && deliveries.last == false && !controller.busyForHandoff)
        pasteTargetAvailable = true
        let copiedBeforeError = copied, deliveriesBeforeError = deliveries
        controller.hotkey.updateKey(pressed: true, shift: false, control: false)
        controller.finish()
        controller.hotkey.updateKey(pressed: false, shift: false, control: false)
        response?(["error": "synthetic recognition failure"])
        precondition(errors.last == "synthetic recognition failure" && controller.phase == .idle)
        precondition(copied == copiedBeforeError && deliveries == deliveriesBeforeError && !controller.busyForHandoff)

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
