# wav2sum

A local Krisp + Wispr Flow for macOS: call recording with a summary, and voice dictation into any app. Russian only.

![wav2sum tui](docs/tui.jpg)

## Setup

```bash
uv tool install -e .
ollama pull gemma4:e4b-it-qat
wav2sum app
```

`wav2sum app` puts an icon in the menu bar and starts the background process. On first launch, allow Microphone,
Accessibility, Input Monitoring, and System Audio Recording.

## Usage

- **Dictation.** Hold right ⌥, speak, release, and the text is typed into the active app. Double-tap for hands-free mode;
  Esc cancels.
- **Edit by voice.** Select some text, hold ⇧ + right ⌥, and say what to do ("make it shorter", "translate to English").
- **Calls.** In the menu bar, choose "Записать созвон" (Record call), then "Остановить" (Stop). The transcript and
  summary show up in `~/wav2sum/output/`.
- **History and summaries.** Run `wav2sum tui`.
- **An existing file.** Run `wav2sum meeting.mp3`.

## Config

`~/.config/wav2sum/config.toml`:

```toml
me = "Андрей"
them = "Илья"
model = "gemma4:e4b-it-qat"

[dictation]
model = "gemma4:e4b-it-qat"
dictionary = ["Kubernetes", "GigaAM"]
snippets = { "мой имейл" = "me@example.com" }
```

## Development

```bash
uv run pytest && uv run ruff check && uv run ruff format --check
```
