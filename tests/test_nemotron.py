import json
import os

import numpy as np
import pytest

from wav2sum import nemotron

WORKER = r"""#!/usr/bin/env python3
import base64
import json
import os
import sys
import time

record = os.environ["FAKE_RECORD"]
mode = os.environ.get("FAKE_MODE", "ok")
def save(value):
    with open(record, "a") as f:
        f.write(json.dumps(value) + "\n")
def send(value):
    print(json.dumps(value), flush=True)
save({"pid": os.getpid(), "args": sys.argv[1:]})
if mode == "startup_timeout":
    time.sleep(60)
if mode == "startup_fatal":
    send({"type": "fatal", "error": {"message": "private startup detail"}})
    sys.exit(1)
if mode == "startup_eof":
    sys.exit(1)
if mode == "bad_protocol":
    send({"type": "ready", "protocol": 2})
    time.sleep(60)
send({"type": "ready", "protocol": 3})
if mode == "write_timeout":
    time.sleep(60)
chunks = []
for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "chunk":
        pcm = base64.b64decode(message["pcm"])
        chunks.append(len(pcm))
        save({"type": "chunk", "id": message["id"], "seq": message["seq"], "bytes": len(pcm), "first": list(pcm[:2])})
    elif message["type"] == "transcribe":
        save(message)
        allowed = {"type", "id", "sample_rate", "language", "prompt"}
        if set(message) - allowed or ("prompt" in message and not isinstance(message["prompt"], str)):
            send({"type": "error", "id": message["id"], "error": {"message": "invalid option"}})
            continue
        if mode == "contextual":
            prompt = message.get("prompt", "")
            text = "recognized " + prompt.split(", ")[0] if prompt else "no context"
            send({"type": "end", "id": message["id"], "stop": "complete", "text": text})
            continue
        if mode == "timeout":
            time.sleep(60)
        if mode == "eof":
            sys.exit(0)
        if mode == "bad_json":
            print("not json", flush=True)
            continue
        if mode == "partial_line":
            sys.stdout.write('{"type":"end"')
            sys.stdout.flush()
            time.sleep(60)
        if mode == "error":
            send({"type": "error", "id": message["id"], "error": {"message": "private audio text"}})
            continue
        if mode == "fatal":
            send({"type": "fatal", "error": {"message": "private audio text"}})
            continue
        if mode == "missing_text":
            send({"type": "end", "id": message["id"], "stop": "complete"})
            continue
        if mode == "empty":
            send({"type": "end", "id": message["id"], "stop": "complete", "text": ""})
            continue
        if mode == "cancelled":
            send({"type": "cancelled", "id": message["id"]})
            continue
        if mode == "wrong_id":
            send({"type": "end", "id": "other", "stop": "complete", "text": "wrong"})
            continue
        send({"type": "progress", "id": message["id"], "done": 0.5})
        send({
            "type": "end", "id": message["id"],
            "stop": "model_limit" if mode == "partial" else "complete",
            "text": f"  clip {len(chunks)}  ",
        })
"""


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    worker = tmp_path / "speech"
    worker.write_text(WORKER)
    worker.chmod(0o755)
    model = tmp_path / "model.gguf"
    model.touch()
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("WAV2SUM_SPEECH_BIN", str(worker))
    monkeypatch.setenv("WAV2SUM_NEMOTRON_MODEL", str(model))
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setattr(nemotron, "START_TIMEOUT", 1.0)
    monkeypatch.setattr(nemotron, "REQUEST_TIMEOUT", 0.4)
    monkeypatch.setattr(nemotron, "CLOSE_TIMEOUT", 0.3)
    return record, worker, model


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_lazy_empty_audio_and_context_do_not_spawn(fake_worker):
    record, _, _ = fake_worker
    with nemotron.NemotronTranscriber() as transcriber:
        assert transcriber.transcribe(np.zeros(0, dtype=np.float32)) == ""
        assert transcriber.transcribe_spans(np.ones(10, dtype=np.float32), [(0, 0), (1, 2)]) == ["", ""]
        assert transcriber.transcribe_spans(np.ones(10, dtype=np.float32), []) == []
    assert not record.exists()


def test_order_pcm_language_and_worker_reused_and_reaped(fake_worker):
    record, _, model = fake_worker
    audio = np.concatenate([np.full(16000, -1.0), np.full(16000, 0.5)]).astype(np.float32)
    with nemotron.NemotronTranscriber(device="mps") as transcriber:
        assert transcriber.transcribe_spans(audio, [(1, 2), (0, 0), (0, 1)], language="ru") == [
            "clip 1",
            "",
            "clip 2",
        ]
        assert transcriber.transcribe(audio[:10], language="en") == "clip 3"
        entries = records(record)
        assert entries[0]["args"] == ["worker", str(model), "--device", "gpu"]
        chunks = [row for row in entries if row.get("type") == "chunk"]
        assert [row["bytes"] for row in chunks] == [32000, 32000, 20]
        assert chunks[0]["first"] == [0, 64]
        assert chunks[1]["first"] == [1, 128]
        assert all(row["seq"] == 0 for row in chunks)
        requests = [row for row in entries if row.get("type") == "transcribe"]
        assert [row.get("language") for row in requests] == ["ru", "ru", "en"]
        assert all(row["sample_rate"] == 16000 for row in requests)
        assert [row["id"] for row in chunks] == [row["id"] for row in requests]
    assert_reaped(entries[0]["pid"])
    transcriber.close()


def test_cpu_and_large_chunk(fake_worker):
    record, _, _ = fake_worker
    with nemotron.NemotronTranscriber(device="cpu") as transcriber:
        assert transcriber.transcribe(np.ones(320000, dtype=np.float32)) == "clip 1"
    entries = records(record)
    assert entries[0]["args"][-2:] == ["--device", "cpu"]
    assert entries[1]["bytes"] == 640000
    assert_reaped(entries[0]["pid"])


def test_context_is_sent_as_the_worker_prompt_and_does_not_leak_into_text(fake_worker, monkeypatch):
    record, _, _ = fake_worker
    monkeypatch.setenv("FAKE_MODE", "contextual")
    with nemotron.NemotronTranscriber() as transcriber:
        assert transcriber.transcribe(np.ones(10, dtype=np.float32), terms=("GitHub", "Qwen")) == "recognized GitHub"
        assert transcriber.transcribe(np.ones(10, dtype=np.float32)) == "no context"
    requests = [row for row in records(record) if row.get("type") == "transcribe"]
    assert requests[0]["prompt"] == "GitHub, Qwen"
    assert "prompt" not in requests[1]


def test_empty_speech_is_a_valid_response(fake_worker, monkeypatch):
    record, _, _ = fake_worker
    monkeypatch.setenv("FAKE_MODE", "empty")
    with nemotron.NemotronTranscriber() as transcriber:
        assert transcriber.transcribe(np.ones(10, dtype=np.float32)) == ""
    assert_reaped(records(record)[0]["pid"])


def test_context_closes_worker_on_caller_error(fake_worker):
    record, _, _ = fake_worker
    with pytest.raises(ValueError, match="caller"), nemotron.NemotronTranscriber() as transcriber:
        assert transcriber.transcribe(np.ones(10, dtype=np.float32)) == "clip 1"
        raise ValueError("caller")
    assert_reaped(records(record)[0]["pid"])


@pytest.mark.parametrize(
    "mode",
    [
        "partial",
        "error",
        "cancelled",
        "eof",
        "bad_json",
        "wrong_id",
        "timeout",
        "partial_line",
        "startup_timeout",
        "startup_fatal",
        "startup_eof",
        "bad_protocol",
        "write_timeout",
        "fatal",
        "missing_text",
    ],
)
def test_failure_terminates_and_reaps(fake_worker, monkeypatch, mode):
    record, _, _ = fake_worker
    monkeypatch.setenv("FAKE_MODE", mode)
    transcriber = nemotron.NemotronTranscriber()
    audio = np.zeros(320000 if mode == "write_timeout" else 10, dtype=np.float32)
    with pytest.raises(RuntimeError) as error:
        transcriber.transcribe(audio)
    assert "private" not in str(error.value)
    assert_reaped(records(record)[0]["pid"])
    transcriber.close()


def test_missing_files_and_unsupported_device_are_explicit(fake_worker, monkeypatch, tmp_path):
    record, worker, model = fake_worker
    with pytest.raises(ValueError, match="device"):
        nemotron.NemotronTranscriber("cuda")
    monkeypatch.setenv("WAV2SUM_SPEECH_BIN", str(tmp_path / "missing-speech"))
    with pytest.raises(RuntimeError, match=r"(?i)speech"):
        nemotron.NemotronTranscriber().transcribe(np.ones(1, dtype=np.float32))
    monkeypatch.setenv("WAV2SUM_SPEECH_BIN", str(worker))
    monkeypatch.setenv("WAV2SUM_NEMOTRON_MODEL", str(tmp_path / "missing.gguf"))
    with pytest.raises(RuntimeError, match=r"(?i)model"):
        nemotron.NemotronTranscriber().transcribe(np.ones(1, dtype=np.float32))
    assert model.exists()
    assert not record.exists()
