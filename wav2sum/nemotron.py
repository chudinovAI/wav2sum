"""On-demand speech.cpp worker for local Nemotron call transcription."""

import base64
import json
import os
import selectors
import subprocess
import threading
import time
from contextlib import suppress

import numpy as np

from wav2sum.audio import SAMPLE_RATE
from wav2sum.local_models import speech_paths

START_TIMEOUT = 600.0
REQUEST_TIMEOUT = 600.0
CLOSE_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class NemotronTranscriber:
    """Keep one worker for a call; no process is started until nonempty audio arrives."""

    def __init__(self, device: str = "mps"):
        if device not in ("mps", "cpu"):
            raise ValueError(f"Unsupported Nemotron device: {device}")
        self.device = "gpu" if device == "mps" else "cpu"
        self._process: subprocess.Popen | None = None
        self._buffer = bytearray()
        self._sequence = 0
        self._lock = threading.RLock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        """Close the protocol and always reap the child, even if it is hung."""
        with self._lock:
            process, self._process = self._process, None
            self._buffer.clear()
            if process is None:
                return
            try:
                if process.stdin is not None:
                    with suppress(OSError):
                        process.stdin.close()
                try:
                    process.wait(timeout=CLOSE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        process.wait(timeout=CLOSE_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            process.kill()
                        process.wait()
            finally:
                if process.poll() is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    process.wait()
                if process.stdout is not None:
                    process.stdout.close()

    def transcribe(self, audio: np.ndarray, language: str | None = None, *, terms: tuple[str, ...] = ()) -> str:
        return self.transcribe_spans(audio, [(0.0, len(audio) / SAMPLE_RATE)], language=language, terms=terms)[0]

    def transcribe_spans(
        self,
        audio: np.ndarray,
        spans: list[tuple[float, float]],
        language: str | None = None,
        *,
        terms: tuple[str, ...] = (),
    ) -> list[str]:
        texts = []
        with self._lock:
            try:
                for start, end in spans:
                    clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
                    if len(clip) == 0:
                        texts.append("")
                        continue
                    if self._process is None:
                        self._start()
                    texts.append(self._transcribe_clip(clip, language, terms))
            except BaseException:
                self.close()
                raise
        return texts

    def _start(self):
        binary, model = (path.resolve() for path in speech_paths())
        if not binary.is_file():
            raise RuntimeError(f"Nemotron speech executable not found: {binary}")
        if not model.is_file():
            raise RuntimeError(f"Nemotron model file not found: {model}")
        try:
            self._process = subprocess.Popen(
                [str(binary), "worker", str(model), "--device", self.device],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError("Could not start Nemotron speech worker") from exc
        assert self._process.stdin is not None and self._process.stdout is not None
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)
        message = self._receive(time.monotonic() + START_TIMEOUT)
        if message.get("type") != "ready" or message.get("protocol") != 3:
            raise RuntimeError("Nemotron worker failed to start with protocol 3")

    def _transcribe_clip(self, clip: np.ndarray, language: str | None, terms: tuple[str, ...]) -> str:
        self._sequence += 1
        request_id = str(self._sequence)
        pcm = np.rint(np.clip(clip, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        chunk = {"type": "chunk", "id": request_id, "seq": 0, "pcm": base64.b64encode(pcm).decode("ascii")}
        request = {"type": "transcribe", "id": request_id, "sample_rate": SAMPLE_RATE}
        if language:
            request["language"] = language
        if terms:
            request["prompt"] = ", ".join(terms)
        deadline = time.monotonic() + REQUEST_TIMEOUT
        self._send(chunk, deadline)
        self._send(request, deadline)
        while True:
            message = self._receive(deadline)
            kind = message.get("type")
            if kind == "fatal":
                raise RuntimeError("Nemotron worker reported a fatal error")
            if message.get("id") != request_id:
                raise RuntimeError("Nemotron worker returned an unexpected request ID")
            if kind == "progress":
                continue
            if kind == "end":
                if message.get("stop") != "complete" or not isinstance(message.get("text"), str):
                    raise RuntimeError("Nemotron transcription was incomplete or malformed")
                return message["text"].strip()
            if kind in ("error", "cancelled"):
                raise RuntimeError(f"Nemotron transcription {kind}")
            raise RuntimeError("Nemotron worker returned an unexpected response")

    @staticmethod
    def _wait(fd: int, event: int, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Nemotron worker timed out")
        with selectors.DefaultSelector() as selector:
            selector.register(fd, event)
            if not selector.select(remaining):
                raise RuntimeError("Nemotron worker timed out")

    def _send(self, message: dict, deadline: float):
        assert self._process is not None and self._process.stdin is not None
        fd = self._process.stdin.fileno()
        data = memoryview((json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8"))
        while data:
            self._wait(fd, selectors.EVENT_WRITE, deadline)
            try:
                count = os.write(fd, data)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise RuntimeError("Nemotron worker input closed") from exc
            if count == 0:
                raise RuntimeError("Nemotron worker input closed")
            data = data[count:]

    def _receive(self, deadline: float) -> dict:
        assert self._process is not None and self._process.stdout is not None
        fd = self._process.stdout.fileno()
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > MAX_RESPONSE_BYTES:
                    raise RuntimeError("Nemotron worker response is too large")
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                try:
                    message = json.loads(line)
                except ValueError:
                    raise RuntimeError("Nemotron worker returned malformed JSON") from None
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    raise RuntimeError("Nemotron worker returned a malformed response")
                return message
            if len(self._buffer) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Nemotron worker response is too large")
            self._wait(fd, selectors.EVENT_READ, deadline)
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise RuntimeError("Nemotron worker output closed") from exc
            if not data:
                raise RuntimeError("Nemotron worker output closed unexpectedly")
            self._buffer.extend(data)
