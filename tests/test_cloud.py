import io
import json
import queue
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest

from wav2sum import cloud
from wav2sum.audio import SAMPLE_RATE


@pytest.fixture
def server(monkeypatch):
    requests = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, dict(self.headers), body))
            status, headers, payload, delay = replies.pop(0)
            if delay:
                time.sleep(delay)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

        def do_GET(self):
            requests.append((self.path, dict(self.headers), b""))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}"
    monkeypatch.setattr(cloud, "DEEPGRAM_URL", url + "/v1/listen")
    monkeypatch.setattr(cloud, "OPENAI_URL", url + "/v1/chat/completions")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-secret")
    try:
        yield requests, replies, url
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def completion(text="done", finish="stop", refusal=None):
    return {
        "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text, "refusal": refusal}}]
    }


@pytest.fixture
def persistent_server(monkeypatch):
    requests = []
    replies = queue.Queue()
    closed = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.client_address, self.path, dict(self.headers), body))
            status, headers, payload, close_peer = replies.get(timeout=2)
            if headers.pop("X-Drop", False):
                self.close_connection = True
                return
            self.send_response(status)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(payload)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)
                self.wfile.flush()
            if close_peer:
                self.close_connection = True

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                closed.put(client_address)

    http = Server(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}"
    monkeypatch.setattr(cloud, "OPENAI_URL", url + "/chat")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-secret")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield requests, replies, closed, url
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def persistent_reply(replies, obj, status=200, headers=None, close_peer=False):
    payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    replies.put((status, headers or {}, payload, close_peer))


def test_openai_reuses_connection_and_unload_closes_it(persistent_server, monkeypatch):
    requests, replies, closed, _ = persistent_server
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    llm = cloud.OpenAILLM()
    llm.validate_credentials()
    assert not requests
    persistent_reply(replies, completion("first"))
    assert llm.chat("system", "one") == "first"
    monkeypatch.setenv("OPENAI_API_KEY", "new-secret")
    persistent_reply(replies, completion("second"))
    assert llm.chat("system", "two") == "second"
    assert requests[0][0] == requests[1][0]
    assert requests[0][2]["Authorization"] == "Bearer oa-secret"
    assert requests[1][2]["Authorization"] == "Bearer new-secret"
    llm.unload()
    assert closed.get(timeout=2) == requests[0][0]


def test_openai_connection_close_and_client_isolation(persistent_server):
    requests, replies, closed, _ = persistent_server
    first, second = cloud.OpenAILLM(), cloud.OpenAILLM()
    persistent_reply(replies, completion("one"), headers={"Connection": "close"}, close_peer=True)
    assert first.chat("system", "one") == "one"
    assert closed.get(timeout=2) == requests[0][0]
    persistent_reply(replies, completion("two"))
    assert first.chat("system", "two") == "two"
    persistent_reply(replies, completion("three"))
    assert second.chat("system", "three") == "three"
    assert len({request[0] for request in requests}) == 3
    first.unload()
    second.unload()


def test_openai_origin_change_closes_old_connection(persistent_server, monkeypatch):
    requests, replies, closed, url = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, completion())
    llm.chat("system", "one")
    monkeypatch.setattr(cloud, "OPENAI_URL", url.replace("127.0.0.1", "localhost") + "/chat")
    persistent_reply(replies, completion())
    llm.chat("system", "two")
    assert requests[0][0] != requests[1][0]
    assert closed.get(timeout=2) == requests[0][0]
    llm.unload()


def test_openai_stale_peer_never_replays_post(persistent_server):
    requests, replies, closed, _ = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, completion("one"), close_peer=True)
    assert llm.chat("system", "one") == "one"
    assert closed.get(timeout=2) == requests[0][0]
    persistent_reply(replies, completion("two"))
    assert llm.chat("system", "two") == "two"
    assert len(requests) == 2 and requests[0][0] != requests[1][0]
    assert [json.loads(request[3])["messages"][-1]["content"] for request in requests].count("two") == 1
    llm.unload()


@pytest.mark.parametrize(
    "payload,status", [(b"bad json", 200), (completion(None, refusal="no"), 200), (b"secret", 500)]
)
def test_openai_failure_discards_connection_and_next_request_recovers(persistent_server, payload, status):
    requests, replies, closed, _ = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, payload, status=status)
    with pytest.raises(RuntimeError) as exc:
        llm.chat("system", "private")
    assert "secret" not in str(exc.value) and "private" not in str(exc.value)
    assert closed.get(timeout=2) == requests[0][0]
    persistent_reply(replies, completion("recovered"))
    assert llm.chat("system", "next") == "recovered"
    assert len(requests) == 2 and requests[0][0] != requests[1][0]
    llm.unload()


def test_openai_incomplete_response_is_not_replayed(persistent_server):
    requests, replies, closed, _ = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, b'{"partial":', headers={"Content-Length": "100"}, close_peer=True)
    with pytest.raises(RuntimeError, match="OpenAI"):
        llm.chat("system", "first")
    assert closed.get(timeout=2) == requests[0][0]
    persistent_reply(replies, completion("recovered"))
    assert llm.chat("system", "second") == "recovered"
    assert len(requests) == 2 and requests[0][0] != requests[1][0]
    llm.unload()


def test_openai_dropped_response_is_not_replayed(persistent_server):
    requests, replies, _, _ = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, b"", headers={"X-Drop": True})
    with pytest.raises(RuntimeError, match="OpenAI"):
        llm.chat("system", "first")
    persistent_reply(replies, completion("next"))
    assert llm.chat("system", "second") == "next"
    assert [json.loads(item[3])["messages"][-1]["content"] for item in requests] == ["first", "second"]
    llm.unload()


def test_openai_concurrent_requests_are_serialized(persistent_server):
    requests, replies, _, _ = persistent_server
    llm = cloud.OpenAILLM()
    persistent_reply(replies, completion("one"))
    persistent_reply(replies, completion("two"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(llm.chat, "system", prompt) for prompt in ("first", "second")]
        assert {future.result(timeout=5) for future in futures} == {"one", "two"}
    assert len(requests) == 2 and requests[0][0] == requests[1][0]
    assert {json.loads(item[3])["messages"][-1]["content"] for item in requests} == {"first", "second"}
    llm.unload()


def test_openai_uses_configured_proxy(persistent_server, monkeypatch):
    requests, replies, _, url = persistent_server
    monkeypatch.setattr(cloud.urllib.request, "getproxies", lambda: {"http": url})
    monkeypatch.setattr(cloud.urllib.request, "proxy_bypass", lambda host: False)
    monkeypatch.setattr(cloud, "OPENAI_URL", "http://example.invalid/chat")
    persistent_reply(replies, completion("proxied"))
    llm = cloud.OpenAILLM()
    assert llm.chat("system", "prompt") == "proxied"
    assert requests[0][1] == "http://example.invalid/chat"
    llm.unload()


def reply(replies, obj, status=200, headers=None, delay=0):
    payload = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    replies.append((status, headers or {}, payload, delay))


def result(*texts, utterances=None):
    results = {"channels": [{"alternatives": [{"transcript": text}]} for text in texts]}
    if utterances is not None:
        results["utterances"] = utterances
    return {"results": results}


def wave_data(body):
    with wave.open(io.BytesIO(body), "rb") as wav:
        return (
            wav.getnchannels(),
            wav.getsampwidth(),
            wav.getframerate(),
            np.frombuffer(wav.readframes(wav.getnframes()), "<i2"),
        )


def test_dictation_wav_query_and_span_slices(server):
    requests, replies, _ = server
    reply(replies, result("  hello  "))
    reply(replies, result("привет"))
    audio = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
    asr = cloud.DeepgramTranscriber()
    assert asr.transcribe_spans(audio, [(0, 2 / SAMPLE_RATE), (2 / SAMPLE_RATE, 4 / SAMPLE_RATE)], "ru") == [
        "hello",
        "привет",
    ]
    path, headers, body = requests[0]
    query = parse_qs(urlsplit(path).query)
    assert query == {"model": ["nova-3"], "language": ["ru"], "smart_format": ["true"], "punctuate": ["true"]}
    assert headers["Authorization"] == "Token dg-secret"
    assert headers["Content-Type"] == "audio/wav"
    channels, width, rate, samples = wave_data(body)
    assert (channels, width, rate) == (1, 2, SAMPLE_RATE)
    np.testing.assert_allclose(samples / 32768, [0, 0.5], atol=1 / 32768)
    _, _, _, samples = wave_data(requests[1][2])
    np.testing.assert_allclose(samples / 32768, [-0.5, 1], atol=1 / 32768)


@pytest.mark.parametrize("language,expected", [(None, "multi"), ("auto", "multi"), ("en", "en")])
def test_language_and_legitimate_empty_transcript(server, language, expected):
    requests, replies, _ = server
    reply(replies, result(""))
    assert cloud.DeepgramTranscriber().transcribe(np.zeros(32, dtype=np.float32), language) == ""
    query = parse_qs(urlsplit(requests[0][0]).query)
    assert query["language"] == [expected]
    assert query.get("language_hint", []) == (["ru", "en"] if expected == "multi" else [])


def test_keyterms_are_repeated_for_batch_spans_and_calls(server):
    requests, replies, _ = server
    for payload in (result("hello"), result("one"), result("two"), result("", utterances=[])):
        reply(replies, payload)
    asr = cloud.DeepgramTranscriber()
    terms = ("Acme Inc", "привет & мир")
    audio = np.ones(8, dtype=np.float32)
    asr.transcribe(audio, "en", terms=terms)
    asr.transcribe_spans(audio, [(0, 4 / SAMPLE_RATE), (4 / SAMPLE_RATE, 8 / SAMPLE_RATE)], terms=terms)
    asr.transcribe_call(audio[:, None], layout="mono", terms=terms)
    assert len(requests) == 4
    for path, _, _ in requests:
        assert parse_qs(urlsplit(path).query)["keyterm"] == list(terms)


def test_stereo_call_channel_speakers_and_timing(server):
    requests, replies, _ = server
    utterances = [
        {"start": 1.25, "end": 2.5, "channel": 1, "speaker": 0, "transcript": "first"},
        {"start": 0.1, "end": 0.8, "channel": 0, "speaker": 3, "transcript": "mine"},
        {"start": 3.0, "end": 4.0, "channel": 1, "speaker": 1, "transcript": "second"},
    ]
    reply(replies, result("mine", "first second", utterances=utterances))
    audio = np.array([[0.25, -0.25], [0.5, -0.5]], dtype=np.float32)
    out = cloud.DeepgramTranscriber().transcribe_call(audio, layout="call", language="en")
    assert [(u.start, u.end, u.text, u.speaker) for u in out] == [
        (0.1, 0.8, "mine", "me"),
        (1.25, 2.5, "first", "them:0"),
        (3.0, 4.0, "second", "them:1"),
    ]
    query = parse_qs(urlsplit(requests[0][0]).query)
    assert query["utterances"] == ["true"] and query["diarize"] == ["true"] and query["multichannel"] == ["true"]
    channels, width, rate, samples = wave_data(requests[0][2])
    assert (channels, width, rate) == (2, 2, SAMPLE_RATE)
    np.testing.assert_allclose(samples / 32768, [0.25, -0.25, 0.5, -0.5], atol=1 / 32768)


def test_call_without_diarization_and_mono_diarization(server):
    requests, replies, _ = server
    reply(replies, result("", "hello", utterances=[{"start": 0, "end": 1, "channel": 1, "transcript": "hello"}]))
    out = cloud.DeepgramTranscriber().transcribe_call(np.zeros((4, 2)), layout="call", diarize=False)
    assert out[0].speaker == "them"
    assert "diarize" not in parse_qs(urlsplit(requests[0][0]).query)
    reply(
        replies,
        result("hello", utterances=[{"start": 0, "end": 1, "channel": 0, "speaker": 2, "transcript": "hello"}]),
    )
    out = cloud.DeepgramTranscriber().transcribe_call(np.zeros((4, 1)), layout="mono")
    assert out[0].speaker == "spk:2"
    assert "multichannel" not in parse_qs(urlsplit(requests[1][0]).query)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": {}},
        {"results": {"channels": []}},
        {"results": {"channels": [{"alternatives": []}]}},
        {"results": {"channels": [{"alternatives": [{"transcript": None}]}]}},
        b"not json",
    ],
)
def test_malformed_asr_response_is_not_silence(server, payload):
    _, replies, _ = server
    reply(replies, payload)
    with pytest.raises(RuntimeError, match="Deepgram"):
        cloud.DeepgramTranscriber().transcribe(np.zeros(8))


@pytest.mark.parametrize(
    "utterances",
    [
        None,
        [],
        [{"start": 0, "end": 1, "channel": 0, "transcript": "hello"}],
        [{"start": 1, "end": 0, "channel": 0, "speaker": 0, "transcript": "hello"}],
        [{"start": 0, "end": 1, "channel": 4, "speaker": 0, "transcript": "hello"}],
        [{"start": 0, "end": 1, "channel": 0, "speaker": -1, "transcript": "hello"}],
        [{"start": float("nan"), "end": 1, "channel": 0, "speaker": 0, "transcript": "hello"}],
    ],
)
def test_malformed_call_utterances_are_rejected(server, utterances):
    _, replies, _ = server
    reply(replies, result("hello", utterances=utterances))
    with pytest.raises(RuntimeError, match="Deepgram"):
        cloud.DeepgramTranscriber().transcribe_call(np.zeros((8, 1)), layout="mono")


def test_call_rejects_lost_nonempty_channel(server):
    _, replies, _ = server
    reply(
        replies,
        result(
            "mine",
            "theirs",
            utterances=[{"start": 0, "end": 1, "channel": 0, "speaker": 0, "transcript": "mine"}],
        ),
    )
    with pytest.raises(RuntimeError, match="Deepgram"):
        cloud.DeepgramTranscriber().transcribe_call(np.zeros((8, 2)), layout="call")


def test_empty_call_is_legitimate(server):
    _, replies, _ = server
    reply(replies, result("", utterances=[]))
    assert cloud.DeepgramTranscriber().transcribe_call(np.zeros((8, 1)), layout="mono") == []


@pytest.mark.parametrize("options", [{}, {"model": "gpt-6-luna", "service_tier": "fast"}])
def test_chat_messages_limits_and_local_token_count(server, options):
    requests, replies, _ = server
    reply(replies, completion("  result  "))
    llm = cloud.OpenAILLM(num_ctx=200, temperature=0.2, **options)
    assert llm.loaded and llm.keep_alive is None and llm.count_tokens("Привет") >= len("Привет".encode())
    assert llm.chat("system", "prompt", [("example", "answer")], max_growth=2) == "result"
    path, headers, body = requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer oa-secret"
    data = json.loads(body)
    assert data["temperature"] == 0.2
    assert data["reasoning_effort"] == "none"
    if options:
        assert data["service_tier"] == "fast"
    else:
        assert "service_tier" not in data
    assert data["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "example"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "prompt"},
    ]
    assert data["max_completion_tokens"] == 76
    llm.unload()


@pytest.mark.parametrize(
    "payload",
    [
        completion("partial", "length"),
        completion("content", "content_filter"),
        completion(None, refusal="no"),
        completion(" ", "stop"),
        completion(None),
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": 42}]},
        {"choices": [{"finish_reason": "stop", "message": {"role": "user", "content": "wrong role"}}]},
        b"not json",
    ],
)
def test_chat_rejects_truncated_refused_and_malformed(server, payload):
    _, replies, _ = server
    reply(replies, payload)
    with pytest.raises(RuntimeError, match="OpenAI"):
        cloud.OpenAILLM().chat("system", "prompt")


@pytest.mark.parametrize("provider,key", [("deepgram", "DEEPGRAM_API_KEY"), ("openai", "OPENAI_API_KEY")])
def test_missing_credentials_make_no_request(server, monkeypatch, provider, key):
    requests, _, _ = server
    monkeypatch.delenv(key)
    obj = cloud.DeepgramTranscriber() if provider == "deepgram" else cloud.OpenAILLM()
    with pytest.raises(RuntimeError, match=key):
        obj.validate_credentials()
    with pytest.raises(RuntimeError, match=key):
        if provider == "deepgram":
            obj.transcribe(np.zeros(8))
        else:
            obj.chat("system", "prompt")
    assert requests == []


@pytest.mark.parametrize("provider", ["deepgram", "openai"])
@pytest.mark.parametrize("status", [302, 307, 401, 500])
def test_errors_and_redirects_do_not_leak_credentials_or_body(server, provider, status):
    requests, replies, url = server
    reply(replies, b"secret response dg-secret oa-secret", status=status, headers={"Location": url + "/stolen"})
    with pytest.raises(RuntimeError) as exc:
        if provider == "deepgram":
            cloud.DeepgramTranscriber().transcribe(np.zeros(8))
        else:
            cloud.OpenAILLM().chat("private system", "private prompt")
    message = str(exc.value)
    assert str(status) in message and "secret" not in message and "private" not in message
    assert len(requests) == 1


@pytest.mark.parametrize("provider", ["deepgram", "openai"])
def test_timeout_is_sanitized(server, provider):
    _, replies, _ = server
    reply(replies, {}, delay=0.2)
    with pytest.raises(RuntimeError) as exc:
        if provider == "deepgram":
            cloud.DeepgramTranscriber(timeout=0.02).transcribe(np.zeros(8))
        else:
            cloud.OpenAILLM(timeout=0.02).chat("system", "prompt")
    assert ("Deepgram" if provider == "deepgram" else "OpenAI") in str(exc.value)
    assert "secret" not in str(exc.value)


def test_saved_keychain_keys_authenticate_without_shell_exports(server, monkeypatch):
    from wav2sum import credentials

    requests, replies, _ = server
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY")
    values = {"DEEPGRAM_API_KEY": "saved-deepgram", "OPENAI_API_KEY": "saved-openai"}

    class Stored:
        def get(self, name):
            return values[name]

    monkeypatch.setattr(credentials, "Keychain", Stored)
    reply(replies, result("recognized"))
    reply(replies, completion("cleaned"))
    assert cloud.DeepgramTranscriber().transcribe(np.zeros(8)) == "recognized"
    assert cloud.OpenAILLM().chat("clean", "recognized") == "cleaned"
    assert requests[0][1]["Authorization"] == "Token saved-deepgram"
    assert requests[1][1]["Authorization"] == "Bearer saved-openai"


def test_dictation_deadline_does_not_shorten_call_deadline(server):
    requests, replies, _ = server
    asr = cloud.DeepgramTranscriber(timeout=1)
    reply(replies, result("late"), delay=0.2)
    with pytest.raises(RuntimeError, match="Deepgram"):
        asr.transcribe(np.ones(8), timeout=0.02)
    reply(
        replies,
        result("finished", utterances=[{"start": 0, "end": 1, "channel": 0, "speaker": 0, "transcript": "finished"}]),
        delay=0.05,
    )
    assert [item.text for item in asr.transcribe_call(np.ones((8, 1)), layout="mono")] == ["finished"]
    assert len(requests) == 2


def test_preconnect_sends_no_http_or_credentials_and_chat_reuses_socket(persistent_server, monkeypatch):
    requests, replies, _, _ = persistent_server
    llm = cloud.OpenAILLM()
    credential = cloud.credential

    def no_credentials(name):
        pytest.fail("TLS preconnect must not read credentials")

    monkeypatch.setattr(cloud, "credential", no_credentials)
    warm = llm.preconnect()
    try:
        warm.join(3)
        assert not warm.is_alive()
        assert not requests
        address = llm._http._connection.sock.getsockname()
        monkeypatch.setattr(cloud, "credential", credential)
        persistent_reply(replies, completion("cleaned"))
        assert llm.chat("system", "spoken text") == "cleaned"
        assert len(requests) == 1
        assert requests[0][0] == address
        assert llm.preconnect() is None
    finally:
        llm.unload()


def test_preconnect_does_not_bypass_configured_proxy(persistent_server, monkeypatch):
    requests, _, _, _ = persistent_server
    monkeypatch.setattr(cloud.urllib.request, "getproxies", lambda: {"http": "http://127.0.0.1:1"})
    monkeypatch.setattr(cloud.urllib.request, "proxy_bypass", lambda host: False)

    def forbidden(*args, **kwargs):
        pytest.fail("preconnect must not open a direct connection when a proxy applies")

    monkeypatch.setattr(cloud.http.client, "HTTPConnection", forbidden)
    monkeypatch.setattr(cloud.http.client, "HTTPSConnection", forbidden)
    llm = cloud.OpenAILLM()
    try:
        assert llm.preconnect() is None
        assert not requests
    finally:
        llm.unload()


@pytest.mark.parametrize("action", ["foreground", "unload"])
def test_stalled_preconnect_never_blocks_or_resurrects_connection(persistent_server, monkeypatch, action):
    requests, replies, _, _ = persistent_server
    entered = threading.Event()
    release = threading.Event()
    connections = []
    base = cloud.http.client.HTTPConnection

    class SlowConnection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            connections.append(self)

        def connect(self):
            if self is connections[0]:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test connect deadline")
            super().connect()

    monkeypatch.setattr(cloud.http.client, "HTTPConnection", SlowConnection)
    llm = cloud.OpenAILLM()
    warm = llm.preconnect()
    try:
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(lambda _: llm.preconnect(), range(16))) == [None] * 16
            assert len(connections) == 1
            try:
                if action == "foreground":
                    persistent_reply(replies, completion("foreground"))
                    assert pool.submit(llm.chat, "system", "spoken text").result(3) == "foreground"
                    foreground = llm._http._connection
                else:
                    pool.submit(llm.unload).result(3)
                    foreground = None
                assert warm.is_alive()
            finally:
                release.set()
        warm.join(3)
        assert not warm.is_alive()
        assert llm._http._connection is foreground
        assert connections[0].sock is None
        if action == "unload":
            persistent_reply(replies, completion("after unload"))
            assert llm.chat("system", "next text") == "after unload"
        assert len(requests) == 1
    finally:
        release.set()
        warm.join(3)
        llm.unload()


def test_failed_preconnect_leaves_normal_chat_available(persistent_server, monkeypatch):
    requests, replies, _, _ = persistent_server
    base = cloud.http.client.HTTPConnection
    attempts = []

    class FirstConnectFails(base):
        def connect(self):
            attempts.append(self)
            if len(attempts) == 1:
                raise TimeoutError("test TLS/connect failure")
            super().connect()

    monkeypatch.setattr(cloud.http.client, "HTTPConnection", FirstConnectFails)
    llm = cloud.OpenAILLM()
    warm = llm.preconnect()
    try:
        warm.join(3)
        assert not warm.is_alive()
        assert not requests
        persistent_reply(replies, completion("normal request"))
        assert llm.chat("system", "spoken text") == "normal request"
        assert len(requests) == 1
    finally:
        llm.unload()


def test_dictation_renews_closed_preconnect_while_audio_continues(persistent_server):
    from types import SimpleNamespace

    from wav2sum.config import DictationConfig
    from wav2sum.dictation import DictationStream, Dictator
    from wav2sum.vocabulary import Vocabulary

    class Upload:
        sent_samples = 0
        closed = False

        def add(self, audio):
            self.sent_samples += len(audio)

        def finish(self):
            return "spoken text"

        def close(self):
            self.closed = True

    requests, replies, _, _ = persistent_server
    models = SimpleNamespace(cloud=True, dictation_asr=SimpleNamespace(start_dictation=lambda **kwargs: Upload()))
    dictator = Dictator(models, DictationConfig(), vocabulary=Vocabulary(), language="en")
    workers = []
    preconnect = dictator.llm.preconnect

    def observe():
        worker = preconnect()
        if worker is not None:
            workers.append(worker)
        return worker

    dictator.llm.preconnect = observe
    stream = DictationStream(dictator)
    try:
        stream.add(np.ones(8, dtype=np.float32))
        workers[-1].join(3)
        dictator.llm._http._connection.close()
        stream.add(np.ones(8, dtype=np.float32))
        workers[-1].join(3)
        prepared = dictator.llm._http._connection
        assert prepared.sock is not None, "continued speech must replace the closed preconnect before finalization"
        address = prepared.sock.getsockname()
        assert not requests
        persistent_reply(replies, completion("cleaned"))
        assert dictator.dictate(np.empty(0, dtype=np.float32), stream=stream).text == "cleaned"
        assert len(requests) == 1
        assert requests[0][0] == address
    finally:
        stream.close()
        dictator.llm.unload()
        for worker in workers:
            worker.join(3)
