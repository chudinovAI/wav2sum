# wav2sum

Call recording and voice dictation for macOS through a native GUI, with an offline-by-default local mode and explicitly selected Cloud mode. Russian and English.

## Installing the internal app

For the small friend beta, use the standalone `Wav2Sum.app` from the internal DMG, not a copied development app.
Move it to Applications and launch it. Python, Swift capture helpers, compatible FFmpeg and its shared libraries
are bundled; recipients do not need Git, uv, Homebrew or Xcode. ASR/cleanup/diarization weights are not bundled;
the small Silero VAD resource comes with its runtime package. Ordinary builds contain no credentials;
an explicitly prepared private beta can include dedicated cloud keys (see the build instructions below).
This is an internal, non-notarized build: verify its origin before opening it. If macOS blocks it, use
System Settings → Privacy & Security → **Open Anyway** for this app, and confirm only if you trust the sender.
Do not disable Gatekeeper. Developer ID/notarization remains a separate distribution prerequisite.
The builder derives the minimum macOS version from its bundled binaries; the installed MLX runtime currently
requires macOS 26.2. Apple Silicon alone does not prove compatibility with an older macOS.

The first-run Settings window starts no daemon until you apply an explicit mode:

- **Local:** prepare/download models, inspect the provisional hardware recommendation and override the call model
  if appropriate. Qwen is the lighter call backend; Nemotron is a manual option on smaller-memory machines.
  Downloads are explicit; ordinary startup does not download Qwen/Gemma. Local processing creates no summaries.
- **Cloud:** explicitly consent to Deepgram/OpenAI transfers and charges. In an ordinary build, save your own or
  separately issued test keys using the secure fields; they go to Keychain, not TOML or logs. A private beta
  reports included access and needs no key entry; the optional fields save your own overrides in Keychain.
- Grant Microphone, Input Monitoring, Accessibility, system-audio and notification permissions through the
  corresponding buttons as needed. The window reports the permissions it can preflight.
- Automatic completed-call processing is **off by default**. Local automation transcribes only; cloud automation
  requires a separate consent to automatic uploads and produces transcripts/summaries.

After Apply, the main window opens with a recording/dictation sidebar and a side-by-side history/detail workspace.
**Start Call Recording** has a timer and Local/Cloud status. **Stop Recording** saves the audio;
processing follows the explicit automatic-processing setting.
Recording errors and the saved path stay in the window even when notifications are disabled. After a disconnect,
recording status is unknown until the app reconnects; another recording cannot be started from stale state.
Use **Open wav2sum** in the menu bar to reopen a closed window. Normal launches show it after completed setup;
dev-watch handoffs do not bring it forward automatically.

In **Settings**, clear **Enable call recording and processing**, then press **Apply Settings** for dictation-only use.
This saves your choice, disables call detection and actions in the background process, and hides call controls/history.
Check it again and apply to restore calls. Existing recordings, results and downloaded models are never removed.
Changing it requires idle recording, dictation, loading and processing; pending work is not interrupted.
With calls disabled, explicit Local model preparation downloads only Qwen and Gemma; it does not require Nemotron
memory overrides, pyannote access or System Audio permission. Dictation still needs its own microphone/hotkey/paste permissions.

The **Calls** and **Dictations** tabs show read-only history from the connected background process.
Select a raw call and press **Process Recording** to queue it explicitly; in Cloud the button is labelled
**Process Recording (Cloud)** and uses the already consented provider mode. Queued/running recordings cannot be
submitted again. **Open Result** opens the saved summary or transcript using the actual configured output path.
Dictations show final/raw text, time and mode; the existing limit of 500 recent entries and seven-day retention
are unchanged. There is no delete action in this window. **Refresh** rereads local files; job, recording,
dictation and retention events refresh the list automatically. Read failures are shown, not presented as empty history.
**⌘R** refreshes history in the main window.
The dictation pane also reports starting/listening/processing, microphone/provider errors, clipboard fallback and
permission status. Permissions still require explicit actions in Settings; an insertion request is not proof of a successful paste.

Open **Settings…** from the main window or menu bar (**⌘,** in the main window). The grouped form scrolls while
**Apply** stays visible. Mode/model changes are refused during recording, dictation, model loading or queued/running
processing. Accepted changes coordinate an idle daemon restart; no process is forcibly killed.
App choices live in `~/Library/Application Support/wav2sum/app-settings.json`, including the explicitly selected mode
and calls switch. Existing settings without the switch keep calls enabled. There is no user-facing CLI or TUI;
the private `serve`/`setup` bootstrap remains only for communication between the app and its Python runtime.

**Share Reviewed Examples…** opens a read-only view of dictations and learned edits. Filter, **Select All Shown**
or clear selection, edit targets, and explicitly confirm every selected target. Changing a target clears its
confirmation. A separate export consent and save panel create a new private JSONL file; existing files are never
overwritten. Only raw/target text, language, style, schema version and training consent are exported. There are no
audio/call files, app/window names, source paths or automatic network uploads. Review for sensitive information
before manually sharing the file; seven-day history retention does not remove an exported file.

## Local and cloud modes

| | Default local | Explicitly selected Cloud |
|---|---|---|
| Dictation ASR | Qwen3-ASR 1.7B | Deepgram Nova-3 |
| Dictation cleanup, commands, translation | Local Gemma | OpenAI `gpt-6-luna`, Fast |
| Call transcription | Qwen or Nemotron 30B IQ4_NL, on demand | Deepgram Nova-3 with timestamps and speakers |
| Call summary and chapters | Disabled | OpenAI `gpt-6.1-terra` |

Selecting Cloud and confirming consent authorizes sending audio to Deepgram and text to OpenAI. The choice lives in
app settings, not `config.toml`. There is no automatic switch between modes or providers on errors. Ordinary dictation cleanup may
retain the raw ASR text if cleanup fails, as before; translation failures do not paste untranslated text.
Cloud dictation starts one chunked Deepgram HTTP upload with the first audio and sends PCM while you speak.
Releasing the key sends the remaining tail and EOF; only the complete HTTP response becomes the transcript.
There is no live text preview, second ASR pass or automatic replay. The app sends PCM to the local daemon every
250 ms in cloud mode, every second in local mode. Local sessions use independent incremental Silero state and
retain only the active recognition tail; their preview text is never treated as the final transcript.
This overlaps request setup/upload, not a guaranteed provider inference phase. Deepgram's binary-audio/encoding
docs do not specify chunked buffering guarantees; support and full-tail delivery were exercised on synthetic inputs.
In `auto`, Deepgram HTTP requests include RU/EN
[language hints](https://developers.deepgram.com/docs/nova-3-language-prompting) while keeping `language=multi`.
Hints bias detection toward Russian/English; they do not restrict languages or guarantee correct recognition.
Explicit `ru`/`en` requests remain fixed-language and do not send hints.
Cloud dictation uses [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) with
[`service_tier=fast`](https://developers.openai.com/api/docs/guides/fast-mode); call summaries default to
[GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra), without explicitly requesting Fast.
Both use Chat Completions with `reasoning_effort=none`. Fast is a paid processing tier, not part of the model ID.
Cleanup sends the raw transcript to the LLM without regex self-correction or semantic rejection of its edits.
Actual API failures or an empty result retain raw text; explicit vocabulary/snippets and cursor formatting still apply.

Each OpenAI client reuses one direct HTTP/1.1 connection, with serialized requests and verified TLS.
During cloud dictation with cleanup enabled, incoming audio starts a best-effort background TCP/TLS preconnect
and renews a detected closed peer while speech continues. Preconnect sends no HTTP request, credentials or text,
and consumes no model tokens. There is no idle reconnect timer.
An unfinished/failed preconnect never blocks the foreground request; it uses the normal connection path instead.
An ambiguous failed POST is never replayed automatically. Configured proxies keep the existing urllib path,
without direct preconnect or connection reuse. `unload` and owner shutdown close the connection and invalidate
pending preconnect results; DNS/connect may finish later, but cannot publish a late socket into the client.

Cloud credentials are stored in macOS Keychain (`service=wav2sum.cloud`), not `config.toml` or logs.
Enter both keys in the secure fields in **Settings → Cloud Credentials**, then press **Save Cloud Keys**.
Leave the fields blank to keep existing keys. Environment variables override Keychain at runtime for explicit
development/CI use. Local processing never accesses Keychain.
Provider usage is billable and subject to each provider's data-handling policies.

Use **Settings** for a coordinated idle mode switch. Keychain credentials also work when Finder/Launch Services
does not inherit shell exports. Locked/denied Keychain access fails clearly; background processing never opens an
authentication prompt. Unlock Keychain and retry the explicit Settings action if macOS denies access.
A client/daemon mode mismatch disables processing rather than uploading through the other mode.
The selected mode is visible in the main window and menu bar.

## Usage

- **Dictation.** Hold right ⌥ and speak. Local shows raw recognition in a read-only 320×80 preview in the
  lower-right corner; drag its background to move it without taking focus. Its position stays through updates and
  subsequent dictations until the app exits. Cloud shows a 190×40 status panel centered near the bottom, without live
  text preview. Nothing is inserted while you speak. Release to finish recognition and cleanup, then paste
  once; the result stays on the clipboard. If the app, window, field, text or cursor changes before completion,
  or the target cannot be verified through Accessibility, the result is only copied. Double-tap for hands-free mode.
  Esc cancels recording or a pending insertion; late replies from a cancelled dictation are ignored. Cancellation
  also closes its cloud stream and prevents a still-running final result from being saved to history.
  An already-running local inference or LLM request may finish, but its cancelled result is discarded.
  Microphone changes keep the audio already captured in the current dictation; switching hardware can leave a short
  gap. With a Bluetooth default input, dictation prefers the Mac's built-in microphone. If capture cannot continue,
  the main window shows a microphone error and you can start another dictation after an input is available.
- **Edit by voice.** Select some text, hold ⇧ + right ⌥, and say what to do ("make it shorter", "translate to English");
  the result replaces the selection.
- **Dictate in English.** Hold ⌃ + right ⌥ and speak Russian or English; clean English text is pasted at the cursor.
  If the selected LLM fails, nothing is pasted and you hear a beep.
- **Calls.** When enabled, recording starts by itself when Zoom, Teams, or Webex takes the microphone and stops once the call ends.
  To record anything else, use **Start Call Recording** in the main window (or menu bar), then **Stop Recording**.
  Recordings are saved automatically. By default, stopping a recording or starting the background process does not enqueue processing;
  the explicit automatic-processing setting applies only after a successful stop, never retrospectively at startup.
  Otherwise select the raw call in the main window and press **Process Recording**.
  Local mode creates a transcript and metadata; cloud also creates summary/chapters. Results use the configured output directory.
  Cloud summary points cite source utterances, for example `[U2 00:01–00:02]`; fabricated, hidden or missing
  references are rejected. Long monologues are split without inventing finer timestamps, and reduction keeps
  source references within the context limit. A valid citation proves source identity, not semantic entailment.
  Local call windows join nearby speech up to 20 seconds (Qwen) or 50 seconds (Nemotron), without crossing speaker
  changes; overlapping or uncovered diarization remains unattributed. Spoken self-corrections stay in the transcript.
  The daemon rejects processing an active or finalizing recording, a duplicate queued or running job, ambiguous
  recordings with the same filename stem, or an existing output directory, including partial output.
  If capture fails while stopping, the daemon clears the recording state and leaves any partial WAV in place.
- **Summarize Last Meeting.** In cloud mode, use this menu-bar action for the most recent completed recording
  owned by wav2sum. Recording start time takes precedence over result-file modification time. A raw recording is
  transcribed and summarized; an existing transcript is summarized without reading or transcribing audio again.
  An existing summary is opened, never overwritten. Running work is reused; active recordings and incomplete
  outputs are refused. New results use the existing completion notification, without stealing focus to open them.
  The action is disabled in local mode.
- **Learning from edits.** If you fix a term or a name right after pasting a dictation ("гитхаб" → "GitHub"), wav2sum remembers
  it and writes it that way next time. Learned words live in `~/Library/Application Support/wav2sum/vocabulary.json`.
- **Language.** Qwen (local dictation), Nemotron (local calls) and Deepgram (cloud) recognize both languages.
  In `auto`, Qwen/Nemotron are not forced to one language and Deepgram uses `multi`; the final text chooses the
  Russian or English cleanup/summary prompt. Explicit `language = "ru"` or `"en"` sets the recognition and text language.
  The tested Nemotron worker validates a `language` option but does not use it to constrain recognition; for that
  backend an explicit language selects the output/summary language, not a guaranteed recognition restriction.
- **History and summaries.** Open the main window's **Calls** or **Dictations** history. The GUI is read-only.
  Dictation history older than seven days is permanently removed at daemon startup and then hourly, including
  history accumulated before this version. The GUI refreshes after cleanup. Vocabulary, learned-edit examples
  (`edits.jsonl`), call recordings and call results are not pruned. Entries with unknown or timezone-naive dates
  are retained; malformed history or a write failure leaves the original file unchanged.

After updating an existing installation, quit the old menu-bar app first, then open the updated **Wav2Sum.app**.

**Starting the updated daemon activates the seven-day rule on existing dictation history.** This history removal
is irreversible; it does not move individual entries to the Trash.

## Config

`~/.config/wav2sum/config.toml`:

```toml
me = "Андрей"
them = "Илья"
language = "auto"  # or "ru" / "en"
summary_model = "gpt-6.1-terra"  # cloud call summary only
auto_record = true
call_apps = ["us.zoom", "com.microsoft.teams2", "Cisco-Systems.Spark"]  # bundle id prefixes

[dictation]
model = "mlx-community/gemma-4-E4B-it-qat-4bit"
learn = true
dictionary = ["Kubernetes", "GigaAM"]
snippets = { "мой имейл" = "me@example.com" }
```

The retired top-level `model` setting for local summaries is ignored with a warning, not sent to OpenAI as a model
ID. Use `summary_model` for cloud summaries. `[dictation].model` still selects the local Gemma cleanup model.

`dictionary` and learned spellings also supply ASR vocabulary hints: Deepgram `keyterm`, Qwen contextual `prompt`,
and Nemotron worker `prompt`. Hints are frozen with the style/language/corrections at the first audio chunk,
deduplicated, and bounded to 50 terms / 500 UTF-8 bytes, explicit dictionary first. They do not authorize an
unsupported replacement of a sound-alike word. Newline/control-containing terms are excluded.
Mixed Cyrillic/Latin tokens can use a supplied spelling only when exact transliteration matches it
(`Немotron` → `Nemotron`); unrelated sound-alikes are not guessed and no correction is learned from this operation.

## Development

### Checkout environment

```fish
uv sync
uv run --offline --no-sync python -c 'from wav2sum.build import install_app; print(install_app())'
open ~/Applications/Wav2Sum.app
```

Local dictation cleanup runs in-process on MLX with `mlx-community/gemma-4-E4B-it-qat-4bit`, explicitly prepared
from Settings and unloaded after idle time. Local calls produce transcripts only: no summary model is loaded.

Dictation uses [Qwen3-ASR 1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf) through native Transformers
(at least 5.13), pinned to revision `bcd2b5b7f32b480ab5790554cfa8347f246a14f3`. Its weights are about 4.1 GB;
inference runs locally: FP16 on Apple Silicon/MPS, FP32 on CPU. Call transcription can use a separate on-demand
Qwen instance with windows up to 20 seconds or Nemotron 3.5 Transcribe 30B IQ4_NL through
[speech.cpp 0.9.4](https://github.com/nyosegawa/speech.cpp/releases/tag/v0.9.4), with windows up to 50 seconds.
The Nemotron worker exits after success/error; cache hits do not load it. Backend identity separates call caches.
Settings preparation installs verified Nemotron assets in `~/.local/share/wav2sum/models`; weights are about 19 GB.
Explicit `WAV2SUM_SPEECH_BIN` / `WAV2SUM_NEMOTRON_MODEL` overrides and the existing study installation remain usable.
For local speaker diarization, accept the [pyannote model terms](https://huggingface.co/pyannote/speaker-diarization-community-1)
yourself, then prepare it with a Hugging Face token. Setup uses the token in memory for that action only. A complete
pinned prepared snapshot loads without the token; existing HF authentication is also supported. Pyannote telemetry
is disabled. An unavailable local model never causes cloud fallback.

The explicit developer install command above builds the development app; open the resulting bundle normally.
Complete Settings on first launch, including explicit permission actions. This development bundle still depends
on its project environment. Keep using the same checkout and `.venv`: the bundle stores its Python interpreter path.
Starting the app starts configured model warmup and, only when calls are enabled, configured call detection.
Inspect the main window without launching a second app or manually starting another daemon.

### Development watch

`devwatch.run` is an explicit development-only watcher for `~/Applications/Wav2Sum.app`, not the standalone friend
bundle. It watches the native app and icon Swift files, builds a signed candidate, and switches only after the current
hotkey, dictation, insertion probe, correction tracking, and correction acknowledgement have finished. A single edit
can take about 210 seconds to locate and follow; further dictations or an unconfirmed correction can extend the wait
indefinitely. A brief hotkey gap remains. The watcher does not stop or restart
an existing daemon, recording, or job; Settings and the background-process menu retain their explicit user actions.
Python daemon changes still require a separate daemon restart.

One-time bootstrap: finish active work and close all Wav2Sum app instances, including standalone copies. Their normal
Quit action requests daemon shutdown; choose an idle time. Build the development bundle from this checkout without
starting the app:

```fish
uv run --offline --no-sync python -c 'from wav2sum.build import bootstrap_watch_app; print(bootstrap_watch_app())'
```

The watcher requires the named signing identity `wav2sum` (or `WAV2SUM_SIGNING_IDENTITY`) to match the installed
bundle; ad-hoc or mismatched signatures and standalone targets are refused before bootstrap replaces anything. The
bundle contains a signed source digest to detect stale builds without changing its `Info.plist`. From the same checkout
and Python environment, start it with:

```fish
uv run --offline --no-sync python -c 'from wav2sum.devwatch import run; run(cloud=True)'
```

Pass `cloud=True` only for the explicitly chosen Cloud mode; use `cloud=False` for Local. Keep that terminal open and do not launch
another Wav2Sum app or installer in parallel. If the daemon is off, choose **Turn On Background Process** explicitly.
**Warning:** starting a daemon applies the seven-day deletion policy to existing dictation history. Ctrl-C waits for
current dictation and corrections, then closes only the owned dev app; the daemon keeps running. An in-flight control
reply is consumed before stopping. Start the same command to open a new dev session. A second Ctrl-C, or interruption
after an exit/swap has begun, can require manual recovery.
Normal app **Quit**, unlike the watcher's app-only exit, still requests daemon shutdown.

The ordinary installer and watcher share one snapshot/build/sign pipeline. Editor saves after a candidate is accepted
for handoff are queued for the next build; they do not roll back that verified candidate. Failed builds retry with
increasing delay and retain only the latest attempt from this session. Successful handoff removes its owned scratch
and previous bundle after the new child's protocol acknowledgement. If a swap or launch outcome is uncertain, the
watcher stops without forcing a process or repeating the exchange and retains its bundles for manual recovery.
It never cleans pre-existing directories; old artifacts from earlier versions and explicit installer/bootstrap
backups still need manual removal. The watcher's signed source digest frames filenames and file boundaries, while
`Info.plist` remains unchanged.

### Checks

```fish
uv run pytest --cov && uv run ruff check && uv run ruff format --check
```

### Keeping permissions across updates

macOS ties privacy permissions to the app's signature. By default the app is signed ad hoc, so every rebuild after an
update asks for them again. To keep them, create a signing certificate once: Keychain Access → Certificate Assistant →
Create a Certificate…, name `wav2sum`, identity type Self-Signed Root, certificate type Code Signing. Then run
the explicit developer install command above and grant the permissions one last time.

### Building an internal standalone app

From the locked project environment on an Apple Silicon Mac with Xcode command-line tools:

```fish
uv run --offline --no-sync python -m wav2sum.distribution --output /tmp/wav2sum-internal
```

Choose a **new** output directory. This builds a standalone `.app` and DMG without installing/restarting the working
app. The build downloads and SHA-256 verifies pinned FFmpeg 8.1.3 source, builds an LGPL configuration with no
external codec libraries/network support, and includes source, licenses and reproduction instructions. System
FFmpeg is not changed. Compatible TorchCodec plugins are relocated with it; copied wheel build-machine rpaths
are removed only when they do not resolve a host dependency. Signing uses the existing configured mechanism and
bundle id; notarization is not performed. Review all component redistribution licenses before sharing.
The builder refuses external runtime symlinks/editable dependencies and copies recorded runtime wheel files,
not home directories, HF caches, recordings, transcripts or histories. It never discovers credentials implicitly.

Before creating the image, it moves the bundle, launches the embedded interpreter with an isolated home/config,
checks imports and helper resolution, and decodes a synthetic WAV with bundled FFmpeg and TorchCodec.
The recipient's declared minimum OS is audited from the actual Mach-O dependencies.

#### Private beta with preconfigured Cloud

The owner can explicitly build a private app with dedicated friend keys:

```fish
uv run --offline --no-sync python -m wav2sum.distribution \
    --private-beta --output /tmp/wav2sum-private-beta
```

Run in an interactive terminal. The builder prompts for both keys with echo disabled; it does **not** take
existing keys from environment variables or Keychain. Cancelled, echoed, redirected or empty input creates
no new output. Choose a fresh output path for each build. `Wav2Sum-Private-Beta.dmg` and its app contain an
extractable credentials resource; share them privately, not in a public release, repository or Homebrew tap.
The output directory is owner-only; this does not hide the keys from recipients.

Friends still select Cloud and explicitly consent, but do not need to enter keys. Included keys are read from
the private bundle, not silently imported into Keychain. Lookup precedence is explicit environment override,
own Keychain item, then beta resource; Keychain denial fails closed. Save Cloud Keys remains available for
own replacements. Passive status reports only resource presence, never its contents or Keychain values.
Local mode and automatic-upload consent rules are unchanged. Revoking the dedicated keys disables their
future use; issuing replacements requires a new private build or entering own keys in Settings.


### Local measurements

`python -m wav2sum.latency` reads personal dictation history; it is not a synthetic benchmark.
Metric definitions and insertion-probe limits are in [ARCHITECTURE](ARCHITECTURE.md#метрики-и-исследовательские-инструменты).
`python -m wav2sum.factcheck --cloud [call folder ...]` sends transcript text to OpenAI for a new report;
without `--cloud` it refuses before reading calls. Authorize the selected real calls separately.
The model runner below performs actual local inference; it does not measure the full app or API latency.

```fish
uv run --offline --no-sync python -m wav2sum.model_benchmark --call-model qwen
uv run --offline --no-sync python -m wav2sum.model_benchmark --call-model nemotron
```

For a friend installation in `/Applications`, quit the app first to avoid overlapping model workloads, then use
the embedded runner without installing uv/Python (adjust the app path if installed elsewhere):

```fish
env PATH="/Applications/Wav2Sum.app/Contents/Helpers:/usr/bin:/bin:/usr/sbin:/sbin" \
    "/Applications/Wav2Sum.app/Contents/Resources/python/bin/python3" \
    -I -B -m wav2sum.model_benchmark --call-model qwen
```

The default uses a synthetic local `say` phrase. For a reproducible corpus, pass an explicit audio file plus
`--reference` text and `--language ru`, `en` or `auto`; optional `--cleanup-reference` supplies the expected edited
text separately. No audio/text is uploaded. Reports omit text unless `--include-text` is explicitly supplied.
Cold means first load in that process, not cleared OS caches; at least three warm runs give samples/median/p95
(p95 is unstable with few samples). Memory is sampled process-tree RSS, not complete unified-memory accounting;
swap is system-wide. The runner's ASR then cleanup stages are isolated, not the full background-app working set.

The checked internal build is accompanied by `Wav2Sum-Benchmark-Corpus.zip`: synthetic RU/EN/mixed WAVs,
spoken references and a hash manifest. Extract it once and use the same files on every Mac, rather than
regenerating speech with each machine's default voice. For example, append
`--wav /path/to/corpus/ru.wav --reference /path/to/corpus/ru.txt --language ru` to the embedded command above;
repeat with each backend. `embedded-model-results.json` beside that build records six actual packaged-runtime
ASR/cleanup runs on the available M5 Max/48 GiB. It does not validate M4 profiles or semantic cleanup quality.

RAM-class recommendations for 16/24/32/48 GiB are **provisional**: Qwen below 48, Nemotron candidate at 48+.
Below 32 GiB or unknown memory, Nemotron requires an explicit override. These are conservative candidate-selection
rules, not measured M4/Pro/Max guarantees. Short synthetic RU/EN/mixed checks on M5 Max/48 GiB:

| Call backend | Isolated sampled peak RSS, GiB | Full-workload sampled peak RSS, GiB | System swap during full-workload smoke |
|---|---:|---:|---|
| Qwen | 7.7–8.0 | 10.9 | Unchanged |
| Nemotron | 17.4–17.6 | 17.8 | Increased |

The full-workload smoke kept Qwen/Gemma dictation loaded while processing real call VAD/pyannote/ASR on synthetic
audio. Other processes were not controlled, so the system-wide swap increase is not attributable solely to wav2sum.
These short TTS checks do not establish long-call quality or safety on friends' M4 machines. Run the same corpus
on their actual configurations before treating a profile as validated. Older recorded cleanup WER against a
spoken reference is lexical distance, not a semantic cleanup-quality score.

### Research-only Core ML converter

The retained GigaAM encoder converter writes
`~/Library/Application Support/wav2sum/gigaam-encoder.mlpackage` (clip lengths `s5`, `s10`, `s20`)
and compares it with Torch using local recordings. It is not used by the current Qwen/Nemotron app.
Run it only when explicitly investigating that converter, not as a routine verification command.

```fish
uv run --group coreml python -m wav2sum.coreml
```

### Dictation phrase set

To check a change to dictation (audio, VAD, ASR, cleanup) on your own voice, read the 46 phrases of
`wav2sum/evalset.py` once in each of four conditions: `quiet` (a quiet room), `airpods` (connect AirPods first: the
recording takes the default input), `noise` (play noise or music from the Mac's speakers), `whisper` (half voice).
Enter starts and stops each phrase, and phrases already recorded are skipped, so to read one again delete its file
in `~/wav2sum/evalset/<condition>/`. The measurement only reads the recordings and prints WER and correctly written
terms per condition, for the raw ASR and after the LLM cleanup.

```fish
uv run python -m wav2sum.evalset record quiet
uv run python -m wav2sum.evalset
```
