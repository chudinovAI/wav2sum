# wav2sum

## Установка

```bash
uv sync
uv tool install -e .
ollama pull gemma4:26b-a4b-it-q4_K_M
export HF_TOKEN="hf_..."
```

## Использование

```bash
wav2sum meeting.wav
wav2sum meeting.wav --speaker "0=Алексей" --speaker "1=Мария"
wav2sum meeting.wav --model llama3.1:8b --no-timestamps -v
```

