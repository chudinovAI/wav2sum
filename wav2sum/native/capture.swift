import AVFoundation
import CoreAudio
import Foundation

struct CAError: Error, CustomStringConvertible {
    let what: String
    let status: OSStatus
    var description: String { "\(what) failed (OSStatus \(status))" }
}

func check(_ status: OSStatus, _ what: String) throws {
    if status != noErr { throw CAError(what: what, status: status) }
}

func address(
    _ selector: AudioObjectPropertySelector,
    _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal
) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func readValue<T: BitwiseCopyable>(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector,
                  _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, initial: T) throws -> T {
    var addr = address(selector, scope)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    try check(AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value), "read property \(selector)")
    return value
}

func readString(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    var value: Unmanaged<CFString>?
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr, let value else { return nil }
    return value.takeRetainedValue() as String
}

func readArray<T: BitwiseCopyable>(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector, of _: T.Type) throws -> [T] {
    var addr = address(selector)
    var size: UInt32 = 0
    try check(AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size), "size of \(selector)")
    let count = Int(size) / MemoryLayout<T>.stride
    guard count > 0 else { return [] }
    let ptr = UnsafeMutablePointer<T>.allocate(capacity: count)
    defer { ptr.deallocate() }
    try check(AudioObjectGetPropertyData(id, &addr, 0, nil, &size, ptr), "read \(selector)")
    return Array(UnsafeBufferPointer(start: ptr, count: count))
}

func inputStreamChannels(_ id: AudioObjectID) throws -> [Int] {
    var addr = address(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeInput)
    var size: UInt32 = 0
    try check(AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size), "stream config size")
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    let abl = raw.bindMemory(to: AudioBufferList.self, capacity: 1)
    try check(AudioObjectGetPropertyData(id, &addr, 0, nil, &size, abl), "stream config")
    return UnsafeMutableAudioBufferListPointer(abl).map { Int($0.mNumberChannels) }
}

struct InputDevice {
    let id: AudioObjectID
    let uid: String
    let name: String
    let channels: Int
}

func inputDevices() throws -> [InputDevice] {
    let ids = try readArray(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices, of: AudioObjectID.self)
    return ids.compactMap { id in
        guard let uid = readString(id, kAudioDevicePropertyDeviceUID),
              let name = readString(id, kAudioObjectPropertyName),
              let channels = try? inputStreamChannels(id).reduce(0, +), channels > 0
        else { return nil }
        return InputDevice(id: id, uid: uid, name: name, channels: channels)
    }
}

func defaultInputDevice() throws -> InputDevice {
    let id: AudioObjectID = try readValue(
        AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice, initial: 0)
    guard let dev = try inputDevices().first(where: { $0.id == id }) else {
        throw CAError(what: "default input device lookup", status: -1)
    }
    return dev
}

func emit(_ fields: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
          var line = String(data: data, encoding: .utf8) else { return }
    line += "\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
}

func fail(_ message: String) -> Never {
    emit(["event": "error", "message": message])
    exit(1)
}

final class StereoWriter {
    private let lock = NSLock()
    private var pending: [Float] = []
    private let file: AVAudioFile
    private let inFormat: AVAudioFormat
    private let converter: AVAudioConverter
    private(set) var framesWritten: AVAudioFramePosition = 0
    var seconds: Double { Double(framesWritten) / converter.outputFormat.sampleRate }

    init(url: URL, deviceRate: Double, outRate: Double) throws {
        inFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: deviceRate, channels: 2, interleaved: false)!
        let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate, channels: 2, interleaved: false)!
        converter = AVAudioConverter(from: inFormat, to: outFormat)!
        converter.sampleRateConverterQuality = AVAudioQuality.high.rawValue
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: outRate,
            AVNumberOfChannelsKey: 2,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
    }

    func append(mic: UnsafeBufferPointer<Float>, sys: UnsafeBufferPointer<Float>, frames: Int) {
        lock.lock()
        pending.reserveCapacity(pending.count + frames * 2)
        for i in 0..<frames {
            pending.append(i < mic.count ? mic[i] : 0)
            pending.append(i < sys.count ? sys[i] : 0)
        }
        lock.unlock()
    }

    @discardableResult
    func flush(final: Bool = false) throws -> (Double, Double)? {
        lock.lock()
        let chunk = pending
        pending.removeAll(keepingCapacity: true)
        lock.unlock()

        let frames = chunk.count / 2
        guard frames > 0 || final else { return nil }

        let input = AVAudioPCMBuffer(pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(max(frames, 1)))!
        input.frameLength = AVAudioFrameCount(frames)
        let l = input.floatChannelData![0], r = input.floatChannelData![1]
        var sumL = 0.0, sumR = 0.0
        for i in 0..<frames {
            l[i] = chunk[2 * i]; r[i] = chunk[2 * i + 1]
            sumL += Double(l[i] * l[i]); sumR += Double(r[i] * r[i])
        }

        let ratio = converter.outputFormat.sampleRate / inFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(frames) * ratio) + 1024
        let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity)!
        var consumed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, outStatus in
            if consumed || frames == 0 {
                outStatus.pointee = final ? .endOfStream : .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return input
        }
        if status == .error { throw error ?? CAError(what: "resample", status: -1) }
        if output.frameLength > 0 {
            try file.write(from: output)
            framesWritten += AVAudioFramePosition(output.frameLength)
        }

        guard frames > 0 else { return nil }
        func db(_ sum: Double) -> Double { max(-120, 10 * log10(sum / Double(frames) + 1e-12)) }
        return (db(sumL), db(sumR))
    }

    func close() throws {
        try flush(final: true)
        file.close()
    }
}

final class Recorder {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private let writer: StereoWriter
    private let mic: InputDevice
    private let sampleRate: Double

    init(url: URL, mic: InputDevice, outRate: Double) throws {
        self.mic = mic

        let tap = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        tap.uuid = UUID()
        tap.name = "wav2sum system audio"
        tap.isPrivate = true
        tap.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(tap, &tapID), "AudioHardwareCreateProcessTap")

        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: "wav2sum capture",
            kAudioAggregateDeviceUIDKey: "wav2sum-capture-\(UUID().uuidString)",
            kAudioAggregateDeviceMainSubDeviceKey: mic.uid,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: mic.uid]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapUIDKey: tap.uuid.uuidString,
                kAudioSubTapDriftCompensationKey: true,
            ]],
        ]
        try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregateID),
                  "AudioHardwareCreateAggregateDevice")

        sampleRate = try readValue(aggregateID, kAudioDevicePropertyNominalSampleRate, initial: Float64(0))
        writer = try StereoWriter(url: url, deviceRate: sampleRate, outRate: outRate)
    }

    func start() throws {
        let streams = try inputStreamChannels(aggregateID)
        let micChannels = mic.channels
        let writer = self.writer

        var micScratch = [Float](repeating: 0, count: 8192)
        var sysScratch = [Float](repeating: 0, count: 8192)

        try check(AudioDeviceCreateIOProcIDWithBlock(&ioProcID, aggregateID, nil) { _, inData, _, _, _ in
            let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
            var frames = 0
            var channelBase = 0
            var micCount = 0, sysCount = 0
            for i in 0..<min(buffers.count, streams.count) {
                let buf = buffers[i]
                let ch = max(Int(buf.mNumberChannels), 1)
                let n = Int(buf.mDataByteSize) / (MemoryLayout<Float>.size * ch)
                guard let data = buf.mData?.assumingMemoryBound(to: Float.self), n > 0 else {
                    channelBase += streams[i]; continue
                }
                if n > micScratch.count {
                    micScratch = [Float](repeating: 0, count: n)
                    sysScratch = [Float](repeating: 0, count: n)
                }
                if frames == 0 {
                    frames = n
                    for f in 0..<n { micScratch[f] = 0; sysScratch[f] = 0 }
                }
                let isMic = channelBase < micChannels
                for f in 0..<min(n, frames) {
                    var s: Float = 0
                    for c in 0..<ch { s += data[f * ch + c] }
                    if isMic { micScratch[f] += s } else { sysScratch[f] += s }
                }
                if isMic { micCount += ch } else { sysCount += ch }
                channelBase += streams[i]
            }
            guard frames > 0 else { return }
            let micDiv = Float(max(micCount, 1)), sysDiv = Float(max(sysCount, 1))
            for f in 0..<frames { micScratch[f] /= micDiv; sysScratch[f] /= sysDiv }
            micScratch.withUnsafeBufferPointer { m in
                sysScratch.withUnsafeBufferPointer { s in
                    writer.append(mic: m, sys: s, frames: frames)
                }
            }
        }, "AudioDeviceCreateIOProcIDWithBlock")

        try check(AudioDeviceStart(aggregateID, ioProcID), "AudioDeviceStart")
        emit(["event": "started", "mic": mic.name, "device_rate": sampleRate, "streams": streams])
    }

    func flush() throws -> (Double, Double)? { try writer.flush() }

    var seconds: Double { writer.seconds }

    func stop() {
        if let ioProcID {
            AudioDeviceStop(aggregateID, ioProcID)
            AudioDeviceDestroyIOProcID(aggregateID, ioProcID)
        }
        if aggregateID != kAudioObjectUnknown { AudioHardwareDestroyAggregateDevice(aggregateID) }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        do { try writer.close() } catch { emit(["event": "error", "message": "close: \(error)"]) }
    }
}

@_silgen_name("responsibility_spawnattrs_setdisclaim")
func responsibility_spawnattrs_setdisclaim(_ attrs: UnsafeMutablePointer<posix_spawnattr_t?>, _ disclaim: Int32) -> Int32

func reexecDisclaimed() {
    let marker = "WAV2SUM_CAPTURE_DISCLAIMED"
    guard getenv(marker) == nil, let exe = Bundle.main.executablePath else { return }
    setenv(marker, "1", 1)

    var attr: posix_spawnattr_t?
    posix_spawnattr_init(&attr)
    posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETEXEC))
    _ = responsibility_spawnattrs_setdisclaim(&attr, 1)

    let argv = CommandLine.arguments.map { strdup($0) } + [nil]
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, exe, nil, &attr, argv, environ)
    emit(["event": "warning", "message": "disclaimed re-exec failed (\(rc)); permissions go to the parent app"])
}

var args = Array(CommandLine.arguments.dropFirst())

if args.contains("--list-devices") {
    do {
        let def = try? defaultInputDevice()
        for dev in try inputDevices() {
            print("\(dev.uid == def?.uid ? "*" : " ") \(dev.name)\t[\(dev.uid)]\t\(dev.channels)ch")
        }
        exit(0)
    } catch { fail("\(error)") }
}

func appsUsingInput() -> [String] {
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard let ids = try? readArray(system, kAudioHardwarePropertyProcessObjectList, of: AudioObjectID.self) else { return [] }
    let apps = ids.compactMap { id -> String? in
        guard (try? readValue(id, kAudioProcessPropertyIsRunningInput, initial: UInt32(0))) == 1,
              let bundleID = readString(id, kAudioProcessPropertyBundleID), !bundleID.isEmpty
        else { return nil }
        return bundleID
    }
    return Set(apps).sorted()
}

if args.contains("--watch-inputs") {
    var last: [String]?
    let timer = DispatchSource.makeTimerSource(queue: .main)
    timer.schedule(deadline: .now(), repeating: 1)
    timer.setEventHandler {
        if getppid() == 1 { exit(0) }
        let apps = appsUsingInput()
        if apps != last { emit(["event": "inputs", "apps": apps]) }
        last = apps
    }
    timer.resume()
    dispatchMain()
}

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    let value = args[i + 1]
    args.removeSubrange(i...(i + 1))
    return value
}

reexecDisclaimed()

let micQuery = option("--mic")
let outRate = Double(option("--sample-rate") ?? "16000") ?? 16_000
guard let outPath = args.first else {
    FileHandle.standardError.write("usage: wav2sum-capture <out.wav> [--mic <name|uid>] [--sample-rate 16000] | --list-devices | --watch-inputs\n".data(using: .utf8)!)
    exit(2)
}

let mic: InputDevice
do {
    if let micQuery {
        guard let found = try inputDevices().first(where: { $0.uid == micQuery || $0.name == micQuery }) else {
            fail("microphone '\(micQuery)' not found; see --list-devices")
        }
        mic = found
    } else {
        mic = try defaultInputDevice()
    }
} catch { fail("\(error)") }

let url = URL(fileURLWithPath: outPath)
try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)

let recorder: Recorder
do {
    recorder = try Recorder(url: url, mic: mic, outRate: outRate)
    try recorder.start()
} catch { fail("\(error)") }

let flushTimer = DispatchSource.makeTimerSource(queue: .main)
flushTimer.schedule(deadline: .now() + 0.25, repeating: 0.25)
flushTimer.setEventHandler {
    do {
        if let (micDb, sysDb) = try recorder.flush() {
            emit(["event": "level", "t": (recorder.seconds * 100).rounded() / 100,
                  "mic_db": micDb.rounded(), "sys_db": sysDb.rounded()])
        }
    } catch { fail("write: \(error)") }
}
flushTimer.resume()

func shutdown() {
    flushTimer.cancel()
    recorder.stop()
    emit(["event": "stopped", "path": url.path, "seconds": (recorder.seconds * 100).rounded() / 100])
    exit(0)
}

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler(handler: shutdown)
    src.resume()
    signalSources.append(src)
}

dispatchMain()
