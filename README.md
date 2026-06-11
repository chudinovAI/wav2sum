# wav2sum

Audio → speaker-attributed transcript → meeting summary. Runs locally (MPS / CUDA / CPU).

## Install

```bash
uv sync
uv tool install -e .
ollama pull gemma4:26b-a4b-it-q4_K_M
export HF_TOKEN="hf_..."
```

Default summary model: `gemma4:26b-a4b-it-q4_K_M` (override with `--model`).

## Usage

```bash
wav2sum meeting.wav                                          # speakers default to 0=Я, 1=Илья
wav2sum meeting.wav --speaker "0=Alex" --speaker "1=Maria"   # override speaker names
wav2sum meeting.wav --model llama3.1:8b --no-timestamps -v

# Speaker count (default: 2). A range turns on auto-detection.
wav2sum meeting.wav --num-speakers 4
wav2sum meeting.wav --min-speakers 2 --max-speakers 5

# Reuse cached diarization/transcription off; recompute everything.
wav2sum meeting.wav --no-cache
```

Writes `transcript.txt`, `summary.md` and `meta.json` to `output/`.

### Record a call (`wav2sum record`)

Records your mic + the other side's system audio into one wav. Stop with Ctrl-C.

```bash
wav2sum record                       # → ./recordings/call-<timestamp>.wav
wav2sum record --summarize           # record, then transcribe + summarize
wav2sum record -o zoom.wav --summarize
```

Device names are configurable via `--output-device` / `--input-device` / `--headphones`.

One-time macOS setup (Audio MIDI Setup):

- `brew install blackhole-2ch switchaudio-osx`
- Multi-Output **"Call output"** = headphones + BlackHole 2ch (headphones as Master, Drift Correction on BlackHole). Select it as the speaker in your call app.
- Aggregate **"Call recorder"** = built-in mic + BlackHole 2ch.
