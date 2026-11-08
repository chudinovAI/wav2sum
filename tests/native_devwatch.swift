import Foundation

@main
struct DevWatchTest {
    static func main() {
        precondition(DevWatch.protocolMarker == "WAV2SUM_DEVWATCH_PROTOCOL_V1")
        if CommandLine.arguments.contains("--pipe-child") || CommandLine.arguments.contains("--eof-child") {
            let eofChild = CommandLine.arguments.contains("--eof-child")
            var disabled = false
            let watch = DevWatch(
                isBusy: { precondition(Thread.isMainThread); return false },
                isReady: { precondition(Thread.isMainThread); return true },
                setEnabled: { enabled in
                    precondition(Thread.isMainThread)
                    if !enabled { disabled = true }
                    if enabled && disabled && eofChild {
                        FileHandle.standardOutput.write(Data("restored\n".utf8))
                        exit(0)
                    }
                },
                respond: { response in
                    precondition(Thread.isMainThread)
                    FileHandle.standardOutput.write(Data("\(response)\n".utf8))
                },
                terminate: { precondition(Thread.isMainThread); exit(0) }
            )
            watch.startReading()
            RunLoop.main.run()
            return
        }
        let pending = DevWatchPending()
        pending.begin()
        precondition(pending.isBusy)
        pending.begin()
        pending.end()
        precondition(pending.isBusy, "all correction requests must finish")
        pending.end()
        precondition(!pending.isBusy)
        pending.begin()
        pending.end(success: false)
        precondition(pending.isBusy, "an unacknowledged correction must block handoff")
        var busy = false
        var ready = true
        var enabled = true
        var responses: [String] = []
        var exits = 0
        let watch = DevWatch(
            isBusy: { busy }, isReady: { ready },
            setEnabled: { enabled = $0 }, respond: { responses.append($0) },
            terminate: { exits += 1 }
        )

        precondition(!watch.takeDaemonStartPermission())
        watch.authorizeDaemonStart()
        precondition(watch.takeDaemonStartPermission())
        precondition(!watch.takeDaemonStartPermission(), "daemon restart permission must be one-shot")
        watch.handle("exit")
        precondition(exits == 0 && enabled && responses.isEmpty)
        watch.handle("protocol")
        precondition(responses == [DevWatch.protocolMarker])
        responses.removeAll()
        busy = true
        watch.handle("prepare")
        precondition(responses == ["busy"] && enabled && !watch.prepared)
        busy = false
        watch.handle("prepare")
        precondition(responses == ["busy", "ready"] && !enabled && watch.prepared)
        watch.updateEnabled()
        precondition(!enabled, "A later ready event must not re-enable dictation")
        busy = true
        watch.handle("exit")
        precondition(exits == 0 && watch.prepared, "A new settings transition must not be interrupted")
        busy = false
        watch.handle("unknown")
        precondition(responses == ["busy", "ready"] && !enabled)
        watch.handle("resume")
        precondition(responses.last == "resumed" && enabled && !watch.prepared)
        watch.handle("exit")
        precondition(exits == 0)
        ready = false
        watch.handle("prepare")
        precondition(responses.last == "ready" && !enabled)
        watch.handle("resume")
        precondition(responses.last == "resumed" && !enabled)
        ready = true
        watch.handle("prepare")
        watch.eof()
        precondition(enabled && !watch.prepared)
        watch.handle("exit")
        precondition(exits == 0, "EOF must revoke exit permission")
        watch.handle("prepare")
        watch.handle("exit")
        precondition(exits == 1 && !enabled && watch.prepared)
        print("native dev-watch state-machine tests passed")
    }
}
