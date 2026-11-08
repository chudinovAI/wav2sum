"""Opt-in cloud providers; neither credentials nor request/response contents are logged.

API references:
https://developers.deepgram.com/reference/speech-to-text/listen-pre-recorded
https://developers.deepgram.com/docs/utterances
https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
"""

import contextlib
import http.client
import io
import json
import math
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import deque

import numpy as np

from wav2sum.audio import SAMPLE_RATE
from wav2sum.conversation import Utterance
from wav2sum.credentials import credential

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-6.1-terra"
DICTATION_MODEL = "gpt-6-luna"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(url: str, body, headers: dict[str, str], timeout: float, provider: str, *, opener=None):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if opener is None:
        opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        # Do not include the response body, URL, or exception text in the error.
        raise RuntimeError(f"{provider} request failed (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, http.client.HTTPException):
        raise RuntimeError(f"{provider} request failed or timed out") from None
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise RuntimeError(f"{provider} returned malformed JSON") from None


class _OpenAITransport:
    """One serialized direct connection; configured proxies use urllib's fresh path."""

    def __init__(self):
        self._lock = threading.Lock()
        self._connection = None
        self._origin = None
        self._preconnecting = None
        self._generation = 0

    def _close(self):
        self._generation += 1
        if self._preconnecting is not None:
            self._preconnecting.close()
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._origin = None

    def close(self) -> None:
        with self._lock:
            self._close()

    def preconnect(self, url: str, timeout: float) -> threading.Thread | None:
        try:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
                return None
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        proxies = urllib.request.getproxies()
        if proxies.get(parsed.scheme) and not urllib.request.proxy_bypass(parsed.netloc):
            return None
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._preconnecting is not None:
                return None
            origin = (parsed.scheme, parsed.hostname, port)
            if self._connection is not None:
                sock = self._connection.sock
                if self._origin == origin and sock is not None and not select.select([sock], [], [], 0)[0]:
                    return None
                self._close()
            connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
            connection = connection_type(parsed.hostname, port, timeout=timeout)
            self._preconnecting = connection
            worker = threading.Thread(
                target=self._connect,
                args=(connection, origin, self._generation),
                name="openai-preconnect",
                daemon=True,
            )
        finally:
            self._lock.release()
        worker.start()
        return worker

    def _connect(self, connection, origin, generation):
        connected = False
        retained = False
        try:
            with self._lock:
                current = generation == self._generation
            if current:
                connection.connect()
                connected = True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            with self._lock:
                self._preconnecting = None
                if connected and generation == self._generation and self._connection is None:
                    self._connection = connection
                    self._origin = origin
                    retained = True
            if not retained:
                connection.close()

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float, provider: str):
        with self._lock:
            try:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
                    raise ValueError("invalid URL")
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                origin = (parsed.scheme, parsed.hostname, port)
                proxies = urllib.request.getproxies()
                if proxies.get(parsed.scheme) and not urllib.request.proxy_bypass(parsed.netloc):
                    self._close()
                    return _post(url, body, headers, timeout, provider)
                if self._origin != origin:
                    self._close()
                if self._connection is not None:
                    sock = self._connection.sock
                    if sock is None or select.select([sock], [], [], 0)[0]:
                        self._close()
                if self._connection is None:
                    connection_type = (
                        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
                    )
                    self._connection = connection_type(parsed.hostname, port, timeout=timeout)
                    self._origin = origin
                connection = self._connection
                connection.timeout = timeout
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request("POST", path, body=body, headers=headers)
                with connection.getresponse() as response:
                    if not 200 <= response.status < 300:
                        self._close()
                        raise RuntimeError(f"{provider} request failed (HTTP {response.status})")
                    data = response.read()
                    if response.will_close:
                        self._close()
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError):
                    self._close()
                    raise RuntimeError(f"{provider} returned malformed JSON") from None
            except (TimeoutError, OSError, ValueError, http.client.HTTPException):
                self._close()
                raise RuntimeError(f"{provider} request failed or timed out") from None


def _pcm(audio: np.ndarray, channels: int) -> bytes:
    samples = np.asarray(audio)
    if channels == 1 and samples.ndim == 1:
        samples = samples[:, None]
    if samples.ndim != 2 or samples.shape[1] != channels:
        raise ValueError(f"Expected audio with {channels} channel(s)")
    if not np.isfinite(samples).all():
        raise ValueError("Audio contains non-finite samples")
    return np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()


def _wav(audio: np.ndarray, channels: int) -> bytes:
    pcm = _pcm(audio, channels)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm)
    return buffer.getvalue()


def _channel_transcripts(response, count: int) -> tuple[dict, list[str]]:
    try:
        results = response["results"]
        channels = results["channels"]
        if not isinstance(channels, list) or len(channels) != count:
            raise ValueError
        texts = []
        for channel in channels:
            alternatives = channel["alternatives"]
            if not isinstance(alternatives, list) or not alternatives:
                raise ValueError
            text = alternatives[0]["transcript"]
            if not isinstance(text, str):
                raise ValueError
            texts.append(text.strip())
        return results, texts
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Deepgram returned malformed transcription") from None


def _decode_utterance(item, channels: int, diarize: bool) -> tuple[Utterance, int]:
    start, end = item["start"], item["end"]
    channel, text = item["channel"], item["transcript"]
    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(start)
        or isinstance(end, bool)
        or not isinstance(end, (int, float))
        or not math.isfinite(end)
        or start < 0
        or end < start
        or isinstance(channel, bool)
        or not isinstance(channel, int)
        or not 0 <= channel < channels
        or not isinstance(text, str)
        or not text.strip()
    ):
        raise ValueError
    if diarize:
        speaker = item["speaker"]
        if isinstance(speaker, bool) or not isinstance(speaker, int) or speaker < 0:
            raise ValueError
    else:
        speaker = 0
    if channels == 1:
        key = f"spk:{speaker}"
    elif channel == 0:
        key = "me"
    else:
        key = f"them:{speaker}" if diarize else "them"
    return Utterance(start, end, text.strip(), key), channel


def _language(language: str | None) -> str:
    if language in (None, "auto"):
        return "multi"
    if language not in ("ru", "en"):
        raise ValueError("Language must be auto, ru, or en")
    return language


class DeepgramUpload:
    """A single chunked HTTP upload, completed only by the final response."""

    _MAX_PENDING_BYTES = 8 * 1024 * 1024
    _MAX_PENDING_CHUNKS = 256

    def __init__(self, *, language=None, terms=(), timeout=20):
        self._language = _language(language)
        self._terms = tuple(terms)
        self._timeout = timeout
        self._condition = threading.Condition()
        self._pending = deque()
        self._pending_bytes = 0
        self._started = False
        self._finishing = False
        self._deadline = None
        self._text = None
        self._error = None
        self._connection = None
        self._socket = None
        self._sent_samples = 0

    @property
    def sent_samples(self):
        with self._condition:
            return self._sent_samples

    def _fail_locked(self, message):
        if self._error is None and self._text is None:
            self._error = message
            self._pending.clear()
            self._pending_bytes = 0
            self._condition.notify_all()
        return self._socket

    @staticmethod
    def _shutdown(sock):
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    def add(self, audio):
        pcm = _pcm(audio, 1)
        sock = None
        overflow = False
        with self._condition:
            if self._error is not None:
                raise RuntimeError(self._error)
            if self._finishing or self._text is not None:
                raise RuntimeError("Deepgram upload is already finished")
            if not pcm:
                return
            # A whole recording can be one chunk; bound the backlog, not that input's duration.
            if (self._pending and self._pending_bytes + len(pcm) > self._MAX_PENDING_BYTES) or len(
                self._pending
            ) >= self._MAX_PENDING_CHUNKS:
                overflow = True
                sock = self._fail_locked("Deepgram upload buffer overflow")
            else:
                self._pending.append(pcm)
                self._pending_bytes += len(pcm)
                if not self._started:
                    self._started = True
                    threading.Thread(target=self._run, daemon=True, name="deepgram-upload").start()
                self._condition.notify_all()
        if overflow:
            self._shutdown(sock)
            raise RuntimeError("Deepgram upload buffer overflow")

    def _body(self):
        # urllib creates and connects the connection before consuming this iterator.
        with self._condition:
            self._socket = self._connection.sock
            sock = self._socket if self._error is not None else None
        self._shutdown(sock)
        while True:
            with self._condition:
                while not self._pending and not self._finishing and self._error is None:
                    self._condition.wait()
                if self._error is not None:
                    return
                if not self._pending:
                    return
                chunk = self._pending.popleft()
                self._pending_bytes -= len(chunk)
            yield chunk
            with self._condition:
                self._sent_samples += len(chunk) // 2

    def _run(self):
        upload = self

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, request):
                def connection(host, **kwargs):
                    conn = http.client.HTTPConnection(host, **kwargs)
                    with upload._condition:
                        upload._connection = conn
                    return conn

                return self.do_open(connection, request)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(self, request):
                def connection(host, **kwargs):
                    conn = http.client.HTTPSConnection(host, **kwargs)
                    with upload._condition:
                        upload._connection = conn
                    return conn

                return self.do_open(connection, request, context=self._context)

        try:
            with self._condition:
                if self._error is not None:
                    return
            key = credential("DEEPGRAM_API_KEY")
            with self._condition:
                if self._error is not None:
                    return
            query = {
                "model": DEEPGRAM_MODEL,
                "language": self._language,
                "smart_format": "true",
                "punctuate": "true",
                "keyterm": self._terms,
                "encoding": "linear16",
                "sample_rate": str(SAMPLE_RATE),
                "channels": "1",
            }
            if self._language == "multi":
                query["language_hint"] = ("ru", "en")
            opener = urllib.request.build_opener(HTTPHandler(), HTTPSHandler(), _NoRedirect())
            response = _post(
                DEEPGRAM_URL + "?" + urllib.parse.urlencode(query, doseq=True),
                self._body(),
                {"Authorization": f"Token {key}", "Content-Type": "application/octet-stream"},
                self._timeout,
                "Deepgram",
                opener=opener,
            )
            _, texts = _channel_transcripts(response, 1)
            with self._condition:
                if self._error is None:
                    if self._deadline is not None and time.monotonic() >= self._deadline:
                        self._fail_locked("Deepgram request failed or timed out")
                    else:
                        self._text = texts[0]
                        self._condition.notify_all()
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else "Deepgram request failed or timed out"
            with self._condition:
                self._fail_locked(message)
        finally:
            with self._condition:
                self._connection = None
                self._socket = None

    def finish(self):
        sock = None
        with self._condition:
            if not self._started and self._error is None:
                self._finishing = True
                self._text = ""
            elif not self._finishing and self._error is None:
                self._finishing = True
                self._deadline = time.monotonic() + self._timeout
                self._condition.notify_all()
            while self._text is None and self._error is None:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    sock = self._fail_locked("Deepgram request failed or timed out")
                    break
                self._condition.wait(remaining)
            text, error = self._text, self._error
        self._shutdown(sock)
        if error is not None:
            raise RuntimeError(error)
        return text

    def close(self):
        with self._condition:
            sock = self._fail_locked("Deepgram upload cancelled")
        self._shutdown(sock)


class DeepgramTranscriber:
    def __init__(self, timeout: float = 300):
        self.timeout = timeout

    def start_dictation(self, *, language=None, terms=(), timeout=20) -> DeepgramUpload:
        return DeepgramUpload(language=language, terms=terms, timeout=timeout)

    def validate_credentials(self) -> None:
        credential("DEEPGRAM_API_KEY")

    def _listen(
        self,
        audio: np.ndarray,
        channels: int,
        language: str | None,
        *,
        terms=(),
        call=False,
        diarize=True,
        timeout=None,
    ):
        key = credential("DEEPGRAM_API_KEY")
        query = {
            "model": DEEPGRAM_MODEL,
            "language": _language(language),
            "smart_format": "true",
            "punctuate": "true",
            "keyterm": terms,
        }
        if query["language"] == "multi":
            query["language_hint"] = ("ru", "en")
        if call:
            query["utterances"] = "true"
            if diarize:
                query["diarize"] = "true"
            if channels == 2:
                query["multichannel"] = "true"
        return _post(
            DEEPGRAM_URL + "?" + urllib.parse.urlencode(query, doseq=True),
            _wav(audio, channels),
            {"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            self.timeout if timeout is None else timeout,
            "Deepgram",
        )

    def transcribe(
        self,
        audio: np.ndarray,
        language: str | None = None,
        *,
        terms: tuple[str, ...] = (),
        timeout: float | None = None,
    ) -> str:
        if not len(audio):
            return ""
        _, texts = _channel_transcripts(self._listen(audio, 1, language, terms=terms, timeout=timeout), 1)
        return texts[0]

    def transcribe_spans(
        self,
        audio: np.ndarray,
        spans: list[tuple[float, float]],
        language: str | None = None,
        *,
        terms: tuple[str, ...] = (),
    ) -> list[str]:
        return [
            self.transcribe(audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)], language, terms=terms)
            for start, end in spans
        ]

    def transcribe_call(
        self,
        audio_2d: np.ndarray,
        *,
        layout: str,
        language: str | None = None,
        diarize: bool = True,
        terms: tuple[str, ...] = (),
    ) -> list[Utterance]:
        if layout not in ("mono", "call"):
            raise ValueError("Call layout must be mono or call")
        channels = 2 if layout == "call" else 1
        if not len(audio_2d):
            return []
        response = self._listen(audio_2d, channels, language, terms=terms, call=True, diarize=diarize)
        results, transcripts = _channel_transcripts(response, channels)
        items = results.get("utterances")
        if items is None and not any(transcripts):
            return []
        if not isinstance(items, list):
            raise RuntimeError("Deepgram returned malformed utterances")
        utterances = []
        represented = set()
        try:
            for item in items:
                utterance, channel = _decode_utterance(item, channels, diarize)
                utterances.append(utterance)
                represented.add(channel)
            if any(text and channel not in represented for channel, text in enumerate(transcripts)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Deepgram returned malformed utterances") from None
        return sorted(utterances, key=lambda utterance: utterance.start)


class OpenAILLM:
    def __init__(
        self,
        model: str = OPENAI_MODEL,
        num_ctx: int = 32768,
        temperature: float = 0.2,
        timeout: float = 30,
        service_tier: str | None = None,
        **metadata,
    ):
        self.model = model
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.timeout = timeout
        self.service_tier = service_tier
        self.keep_alive = None
        self._http = _OpenAITransport()

    @property
    def loaded(self) -> bool:
        return True

    def validate_credentials(self) -> None:
        credential("OPENAI_API_KEY")

    def preconnect(self) -> threading.Thread | None:
        """Start a best-effort connection only; no credentials or HTTP request."""
        return self._http.preconnect(OPENAI_URL, self.timeout)

    def count_tokens(self, text: str) -> int:
        return len(text.encode("utf-8"))

    def unload(self) -> None:
        self._http.close()

    def chat(
        self,
        system: str,
        user: str,
        examples: list[tuple[str, str]] = (),
        *,
        max_growth: float | None = None,
        lookup: bool = False,
        think: bool | None = None,
    ) -> str:
        key = credential("OPENAI_API_KEY")
        messages = [{"role": "system", "content": system}]
        for example, answer in examples:
            messages.extend([{"role": "user", "content": example}, {"role": "assistant", "content": answer}])
        messages.append({"role": "user", "content": user})
        limit = self.num_ctx
        if max_growth is not None:
            limit = min(limit, int(max_growth * self.count_tokens(user)) + 64)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": limit,
        }
        if self.model in (OPENAI_MODEL, DICTATION_MODEL):
            payload["reasoning_effort"] = "none"
        if self.service_tier is not None:
            payload["service_tier"] = self.service_tier
        data = json.dumps(payload).encode("utf-8")
        response = self._http.post(
            OPENAI_URL,
            data,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            self.timeout,
            "OpenAI",
        )
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice["finish_reason"] != "stop":
                raise ValueError
            message = choice["message"]
            if (
                not isinstance(message, dict)
                or message.get("role") != "assistant"
                or message.get("refusal") is not None
            ):
                raise ValueError
            content = message["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            return content.strip()
        except (KeyError, TypeError, ValueError):
            self._http.close()
            raise RuntimeError("OpenAI returned a malformed, refused, or incomplete completion") from None
