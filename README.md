# wav2sum

A local Krisp + Wispr Flow for macOS: call recording with a summary, and voice dictation into any app. Russian and English.

![wav2sum tui](docs/tui.jpg)

## Setup

```bash
uv tool install -e .
wav2sum app
```

The LLMs run in-process on MLX; on first use they download from Hugging Face (`mlx-community/gemma-4-E4B-it-qat-4bit`
for dictation, `mlx-community/gemma-4-12B-it-qat-4bit` for summaries) and unload after a few idle minutes.

`wav2sum app` puts an icon in the menu bar and starts the background process. On first launch, allow Microphone,
Accessibility, Input Monitoring, and System Audio Recording.

If the app is already installed from this checkout, keep using its existing project environment:

```fish
uv run --offline --no-sync wav2sum app
uv run --offline --no-sync wav2sum status
```

Run these commands from the repository. The app stores the Python interpreter path in its bundle; switching between
this `.venv` and the global `uv tool` environment rebuilds the app. `status` only reads the background process state.
Starting the app enables the configured call detection and model warmup, so use `status` when you only need to check it.

## Usage

- **Dictation.** Hold right ⌥, speak, release, and the text is pasted at the cursor and stays on the clipboard; if you
  switch apps before it is ready, it is only copied. Double-tap for hands-free mode; Esc cancels.
- **Edit by voice.** Select some text, hold ⇧ + right ⌥, and say what to do ("make it shorter", "translate to English");
  the result replaces the selection.
- **Dictate in English.** Hold ⌃ + right ⌥ and speak Russian or English; clean English text is pasted at the cursor.
  If the local LLM fails, nothing is pasted and you hear a beep.
- **Calls.** Recording starts by itself when Zoom, Teams, or Webex takes the microphone and stops once the call ends.
  To record anything else, choose "Record Call" in the menu bar, then "Stop Recording". The transcript and summary
  show up in `~/wav2sum/output/`.
- **Learning from edits.** If you fix a term or a name right after pasting a dictation ("гитхаб" → "GitHub"), wav2sum remembers
  it and writes it that way next time. Learned words live in `~/Library/Application Support/wav2sum/vocabulary.json`.
- **Language.** Each call and each dictation is checked for Russian or English. Russian goes to GigaAM, English to
  Parakeet, and the summary and the cleanup are written in the same language. Set `language = "ru"` or `"en"` to
  skip the check.
- **History and summaries.** Run `wav2sum tui`. Press `x` on a call or a dictation to delete it; calls go to the Trash.
- **An existing file.** Run `wav2sum meeting.mp3`.

## Config

`~/.config/wav2sum/config.toml`:

```toml
me = "Андрей"
them = "Илья"
language = "auto"  # or "ru" / "en"
model = "mlx-community/gemma-4-12B-it-qat-4bit"  # summaries
auto_record = true
call_apps = ["us.zoom", "com.microsoft.teams2", "Cisco-Systems.Spark"]  # bundle id prefixes

[dictation]
model = "mlx-community/gemma-4-E4B-it-qat-4bit"
learn = true
dictionary = ["Kubernetes", "GigaAM"]
snippets = { "мой имейл" = "me@example.com" }
```

## Keeping permissions across updates

macOS ties privacy permissions to the app's signature. By default the app is signed ad hoc, so every rebuild after an
update asks for them again. To keep them, create a signing certificate once: Keychain Access → Certificate Assistant →
Create a Certificate…, name `wav2sum`, identity type Self-Signed Root, certificate type Code Signing. Then run
`wav2sum app` and grant the permissions one last time.

## Development

```bash
uv run pytest --cov && uv run ruff check && uv run ruff format --check
```

To try the GigaAM encoder on the Neural Engine (CHU-97), convert it to Core ML. The script saves
`~/Library/Application Support/wav2sum/gigaam-encoder.mlpackage` (one function per clip length: `s5`, `s10`, `s20`)
and prints how close it is to torch on clips from your recordings. The app does not use the package yet.

```bash
uv run --group coreml python -m wav2sum.coreml
```

To check a change to dictation (audio, VAD, ASR, cleanup) on your own voice, read the 46 phrases of
`wav2sum/evalset.py` once in each of four conditions: `quiet` (a quiet room), `airpods` (connect AirPods first: the
recording takes the default input), `noise` (play noise or music from the Mac's speakers), `whisper` (half voice).
Enter starts and stops each phrase, and phrases already recorded are skipped, so to read one again delete its file
in `~/wav2sum/evalset/<condition>/`. The measurement only reads the recordings and prints WER and correctly written
terms per condition, for the raw ASR and after the LLM cleanup.

```bash
uv run python -m wav2sum.evalset record quiet
uv run python -m wav2sum.evalset
```
