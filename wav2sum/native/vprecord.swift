// CHU-143: records a dictation phrase from the default input with Apple's voice processing (noise suppression
// and gain control), until a line or the end of stdin. Writes 16 kHz mono 16-bit WAV.
// Usage: vprecord <out.wav>
import AVFoundation

let engine = AVAudioEngine()
let input = engine.inputNode
try input.setVoiceProcessingEnabled(true)
// Other audio keeps its volume, e.g. the noise played for the "noise" condition.
input.voiceProcessingOtherAudioDuckingConfiguration = .init(enableAdvancedDucking: false, duckingLevel: .min)
let format = input.outputFormat(forBus: 0)
var samples: [Float] = []
let lock = NSLock()
// With voice processing the built-in mic gives 9 identical channels at 48 kHz: the first one is enough.
// Only a tap: touching mainMixerNode makes start() fail with -10875 under voice processing.
input.installTap(onBus: 0, bufferSize: 4800, format: format) { buffer, _ in
    let chunk = UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength))
    lock.withLock { samples.append(contentsOf: chunk) }
}
engine.prepare()
try engine.start()
_ = readLine()
engine.stop()

let recorded = lock.withLock { samples }
let mono = AVAudioFormat(standardFormatWithSampleRate: format.sampleRate, channels: 1)!
let source = AVAudioPCMBuffer(pcmFormat: mono, frameCapacity: AVAudioFrameCount(max(recorded.count, 1)))!
source.frameLength = AVAudioFrameCount(recorded.count)
for (i, sample) in recorded.enumerated() { source.floatChannelData![0][i] = sample }

let settings: [String: Any] = [AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16_000,
                               AVNumberOfChannelsKey: 1, AVLinearPCMBitDepthKey: 16]
let file = try AVAudioFile(forWriting: URL(fileURLWithPath: CommandLine.arguments[1]), settings: settings)
let converter = AVAudioConverter(from: mono, to: file.processingFormat)!
let out = AVAudioPCMBuffer(pcmFormat: file.processingFormat,
                           frameCapacity: AVAudioFrameCount(Double(recorded.count) * 16_000 / format.sampleRate) + 1024)!
// The whole recording in one call: fed chunk by chunk, the converter gave nothing.
var fed = false
var error: NSError?
converter.convert(to: out, error: &error) { _, status in
    if fed { status.pointee = .endOfStream; return nil }
    fed = true
    status.pointee = .haveData
    return source
}
if let error { throw error }
try file.write(from: out)
file.close()
