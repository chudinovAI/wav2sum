"""Real HTTP coverage for streaming dictation uploads."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from wav2sum import cloud
from wav2sum.config import DictationConfig
from wav2sum.dictation import DictationStream, Dictator
from wav2sum.vocabulary import Vocabulary


class UploadServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), UploadHandler)
        self.first = threading.Event()
        self.eof = threading.Event()
        self.respond = threading.Event()
        self.read_body = threading.Event()
        self.read_body.set()
        self.requests = []
        self.chunks = []
        self.headers = {}
        self.status = 200
        self.body = json.dumps({"results": {"channels": [{"alternatives": [{"transcript": " full tail "}]}]}}).encode()


class UploadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        server = self.server
        server.requests.append((self.path, self.headers))
        server.read_body.wait(5)
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    return
                size = int(line.split(b";", 1)[0], 16)
                if not size:
                    self.rfile.readline()
                    server.eof.set()
                    break
                chunk = self.rfile.read(size)
                self.rfile.read(2)
                server.chunks.append(chunk)
                server.first.set()
            if not server.respond.wait(5):
                return
            self.send_response(server.status)
            for key, value in server.headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(server.body)))
            self.end_headers()
            self.wfile.write(server.body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    server = UploadServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(cloud, "DEEPGRAM_URL", f"http://127.0.0.1:{server.server_port}/v1/listen")
    monkeypatch.setattr(cloud, "credential", lambda name: "test-key")
    try:
        yield server
    finally:
        server.read_body.set()
        server.respond.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def upload(**kwargs):
    return cloud.DeepgramTranscriber().start_dictation(**kwargs)


def test_streams_owned_ordered_audio_then_waits_for_eof_and_response(server):
    stream = upload(language="en", terms=("widget",))
    first = np.array([0.25, -0.5], dtype=np.float32)
    stream.add(first)
    assert server.first.wait(3), "first audio must arrive before finish"
    first[:] = 0
    last = np.array([0.75, -1], dtype=np.float32)
    stream.add(last)
    last[:] = 0
    assert not server.eof.is_set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(stream.finish)
        two = pool.submit(stream.finish)
        assert server.eof.wait(3)
        assert not one.done()
        assert not two.done()
        server.respond.set()
        assert one.result(3) == two.result(3) == "full tail"
    assert stream.finish() == "full tail"
    assert stream.sent_samples == 4
    assert b"".join(server.chunks) == np.array([8192, -16384, 24576, -32768], dtype="<i2").tobytes()
    assert len(server.requests) == 1
    path, headers = server.requests[0]
    query = parse_qs(urlsplit(path).query)
    assert query == {
        "model": ["nova-3"],
        "language": ["en"],
        "smart_format": ["true"],
        "punctuate": ["true"],
        "keyterm": ["widget"],
        "encoding": ["linear16"],
        "sample_rate": ["16000"],
        "channels": ["1"],
    }
    assert headers["Transfer-Encoding"].lower() == "chunked"
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["Authorization"] == "Token test-key"


def test_empty_and_closed_uploads_do_not_request_or_read_credentials(server, monkeypatch):
    def no_credentials(name):
        raise AssertionError("credentials requested for empty upload")

    monkeypatch.setattr(cloud, "credential", no_credentials)
    empty = upload()
    empty.add(np.array([], dtype=np.float32))
    assert empty.finish() == ""
    cancelled = upload()
    cancelled.close()
    with pytest.raises(RuntimeError):
        cancelled.add(np.ones(1, dtype=np.float32))
    assert not server.requests


@pytest.mark.parametrize("waiting_response", [False, True])
def test_close_wakes_finish_and_discards_late_response(server, waiting_response):
    stream = upload()
    stream.add(np.ones(8, dtype=np.float32))
    assert server.first.wait(3)
    if waiting_response:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(stream.finish)
            assert server.eof.wait(3)
            stream.close()
            with pytest.raises(RuntimeError):
                result.result(3)
    else:
        stream.close()
        with pytest.raises(RuntimeError):
            stream.finish()
    server.respond.set()
    with pytest.raises(RuntimeError):
        stream.finish()
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "status,body",
    [
        (503, b"secret provider details"),
        (200, b"not json"),
        (200, b'{"results":{"channels":[]}}'),
    ],
)
def test_provider_failures_never_replay(server, status, body):
    server.status, server.body = status, body
    server.respond.set()
    stream = upload()
    stream.add(np.ones(8, dtype=np.float32))
    with pytest.raises(RuntimeError) as first:
        stream.finish()
    assert "secret provider details" not in str(first.value)
    with pytest.raises(RuntimeError):
        stream.finish()
    assert len(server.requests) == 1


def test_response_deadline_no_replay(server):
    stream = upload(timeout=0.2)
    stream.add(np.ones(8, dtype=np.float32))
    assert server.first.wait(3)
    with pytest.raises(RuntimeError):
        stream.finish()
    assert server.eof.wait(3)
    server.respond.set()
    with pytest.raises(RuntimeError):
        stream.finish()
    assert len(server.requests) == 1


def test_overflow_fails_entire_session_without_replay(server):
    server.read_body.clear()
    stream = upload()
    chunk = np.ones(524288, dtype=np.float32)
    try:
        with pytest.raises(RuntimeError):
            for _ in range(128):
                stream.add(chunk)
        with pytest.raises(RuntimeError):
            stream.finish()
        assert len(server.requests) <= 1
    finally:
        stream.close()


def test_default_proxy_is_used_and_redirect_is_not_replayed(server, monkeypatch):
    monkeypatch.setattr(cloud, "DEEPGRAM_URL", "http://asr.example.invalid/v1/listen")
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    server.status = 307
    server.headers["Location"] = f"http://127.0.0.1:{server.server_port}/redirected"
    server.respond.set()
    stream = upload()
    stream.add(np.ones(1, dtype=np.float32))
    with pytest.raises(RuntimeError, match="HTTP 307"):
        stream.finish()
    assert len(server.requests) == 1
    assert server.requests[0][0].startswith("http://asr.example.invalid/v1/listen?")


def test_dictation_stream_real_http_includes_final_audio(server):
    models = SimpleNamespace(cloud=True, dictation_asr=cloud.DeepgramTranscriber())
    dictator = Dictator(models, DictationConfig(cleanup=False), vocabulary=Vocabulary(), language="en")
    stream = DictationStream(dictator)
    stream.add(np.array([0.25, -0.5], dtype=np.float32))
    assert server.first.wait(3)
    assert stream.step(preview=True) is None
    stream.add(np.array([0.75], dtype=np.float32))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(stream.finish)
        assert server.eof.wait(3)
        assert not result.done()
        server.respond.set()
        assert result.result(3) == ("full tail", "en")
    assert len(server.requests) == 1
    assert b"".join(server.chunks) == np.array([8192, -16384, 24576], dtype="<i2").tobytes()
    assert stream.sent_seconds == 3 / 16000


def test_direct_full_recording_is_not_rejected_as_pending_backlog(server):
    models = SimpleNamespace(cloud=True, dictation_asr=cloud.DeepgramTranscriber())
    dictator = Dictator(models, DictationConfig(cleanup=False), vocabulary=Vocabulary(), language="en")
    audio = np.zeros(4_194_305, dtype=np.float32)
    audio[-2:] = [0.75, -1]
    server.respond.set()

    assert dictator.recognize(audio) == ("full tail", "en")
    pcm = b"".join(server.chunks)
    assert pcm == b"\0" * (2 * len(audio) - 4) + np.array([24576, -32768], dtype="<i2").tobytes()
    assert len(server.requests) == 1
