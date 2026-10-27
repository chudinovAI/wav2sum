import asyncio
import base64
import contextlib
import functools
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from wav2sum import daemon
from wav2sum.autorecord import CallDetector
from wav2sum.client import DaemonClient, DaemonError
from wav2sum.config import Config
from wav2sum.dictation import Dictation

ZOOM = "us.zoom.xos"


class FakeRecorder:
    def __init__(self, path, mic, on_level):
        self.path, self.seconds = path, 0.0

    def start(self):
        pass

    def stop(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        return self.path


def _daemon(tmp_path, monkeypatch, inputs: asyncio.Queue):
    async def watch_inputs():
        while (apps := await inputs.get()) is not None:
            yield apps

    monkeypatch.setattr(daemon, "CallRecorder", FakeRecorder)
    monkeypatch.setattr(daemon, "watch_inputs", watch_inputs)
    monkeypatch.setattr(daemon, "CallDetector", functools.partial(CallDetector, start_after=0.05, stop_after=0.1))
    monkeypatch.setattr(daemon, "AUTO_RECORD_TICK", 0.01)
    monkeypatch.setattr(daemon, "VOCABULARY_PATH", tmp_path / "vocabulary.json")
    monkeypatch.setattr(daemon, "_memory_mb", lambda: 512)
    monkeypatch.setattr(daemon, "memory_mb", lambda: 0)
    d = daemon.Daemon(Config(recordings_dir=tmp_path, output_dir=tmp_path / "output"))
    d.loop, d.queue, d.ready = asyncio.get_running_loop(), asyncio.Queue(), asyncio.Event()
    return d


def test_call_is_recorded_from_start_to_end(tmp_path, monkeypatch):
    async def scenario():
        inputs = asyncio.Queue()
        d = _daemon(tmp_path, monkeypatch, inputs)
        watching = asyncio.create_task(d._watch_calls())
        await inputs.put([ZOOM])
        await asyncio.sleep(0.3)
        assert d.status()["recording"]["auto"] == ZOOM

        await inputs.put([])
        await asyncio.sleep(0.4)
        assert d.recorder is None
        assert [j.state for j in d.jobs] == ["queued"]
        watching.cancel()

    asyncio.run(scenario())


def test_manual_stop_mid_call_is_respected(tmp_path, monkeypatch):
    async def scenario():
        inputs = asyncio.Queue()
        d = _daemon(tmp_path, monkeypatch, inputs)
        watching = asyncio.create_task(d._watch_calls())
        await inputs.put([ZOOM])
        await asyncio.sleep(0.3)
        await d._handle({"cmd": "record_stop"}, writer=None)
        await asyncio.sleep(0.3)
        assert d.recorder is None

        await d._handle({"cmd": "record_start"}, writer=None)
        await inputs.put([])
        await asyncio.sleep(0.4)
        assert d.status()["recording"]["auto"] is None
        watching.cancel()

    asyncio.run(scenario())


def test_corrections_are_learned(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        reply = await d._handle({"cmd": "correction", "original": "Залей на гитхаб", "edited": "Залей на GitHub"}, None)
        assert reply == {"learned": [("гитхаб", "GitHub")]}
        assert d.dictator.vocabulary.apply("гитхаб") == "GitHub"

    asyncio.run(scenario())


def test_status_counts_the_in_process_llm_once(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(daemon, "memory_mb", lambda: 200)
        status = d.status()
        assert (status["memory_mb"], status["llm_memory_mb"]) == (312, 200)

    asyncio.run(scenario())


def test_summary_model_stays_as_long_as_dictation_when_shared_else_a_minute():
    assert daemon.Daemon(Config()).summary_llm.keep_alive == 60
    cfg = Config()
    cfg.dictation.model = cfg.model
    assert daemon.Daemon(cfg).summary_llm.keep_alive == 600


def test_only_dictation_caches_its_prompt_prefix():
    d = daemon.Daemon(Config())
    assert (d.dictator.llm.cache_prefix, d.summary_llm.cache_prefix) == (True, False)


@contextlib.asynccontextmanager
async def _serving(d: daemon.Daemon, socket_path: Path):
    server = await asyncio.start_unix_server(d._serve, path=str(socket_path))
    connected = await DaemonClient.connect()
    try:
        yield connected
    finally:
        await connected.close()
        server.close()


def test_client_talks_to_daemon_over_the_socket(tmp_path, monkeypatch, socket_path):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        async with _serving(d, socket_path) as connected:
            status = await connected.request("status")
            assert (status["state"], status["recording"], status["jobs"]) == ("loading", None, [])

            with pytest.raises(DaemonError, match="unknown command 'nope'"):
                await connected.request("nope")
            with pytest.raises(DaemonError, match="not recording"):
                await connected.request("record_stop")

            await connected.request("record_start")
            with pytest.raises(DaemonError, match="already recording"):
                await connected.request("record_start")
            stopped = await connected.request("record_stop")
            assert stopped["job"]["state"] == "queued"
            assert Path(stopped["path"]).parent == tmp_path

    asyncio.run(scenario())


def test_dictation_reaches_history_and_subscribers(tmp_path, monkeypatch, socket_path):
    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        heard = []

        def dictate(audio, **context):
            heard.append((audio, context))
            return Dictation("привет", "Привет", "chat", "ru", False, {"asr": 0.1, "llm": 0.2})

        monkeypatch.setattr(d.dictator, "dictate", dictate)
        async with _serving(d, socket_path) as listener, _serving(d, socket_path) as dictating:
            next_event = asyncio.ensure_future(anext(listener.events()))
            await asyncio.sleep(0.05)

            audio = np.linspace(-1, 1, 16_000, dtype=np.float32)
            reply = await dictating.dictate(audio, app="ru.keepcoder.Telegram", command=False)
            assert (reply["text"], reply["seconds"], reply["app"]) == ("Привет", 1.0, "ru.keepcoder.Telegram")

            event = await asyncio.wait_for(next_event, timeout=5)
            assert (event["event"], event["text"]) == ("dictation", "Привет")

        [(received, context)] = heard
        np.testing.assert_allclose(received, audio, atol=1e-4)
        assert context == {
            "app": "ru.keepcoder.Telegram",
            "title": None,
            "selection": None,
            "command": False,
            "translate": False,
        }
        assert [entry["raw"] for entry in history] == ["привет"]

    asyncio.run(scenario())


def test_silence_is_not_saved_to_history(tmp_path, monkeypatch):
    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        monkeypatch.setattr(
            d.dictator, "dictate", lambda audio, **_: Dictation("", "", "default", "ru", False, {"asr": 0, "llm": 0})
        )
        audio = base64.b64encode(np.zeros(1600, "<i2").tobytes()).decode()
        reply = await d._handle({"cmd": "dictate", "audio": audio}, None)
        assert reply["text"] == ""

    asyncio.run(scenario())
    assert history == []


def test_translate_reaches_the_dictator(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "append_dictation", lambda entry: None)
    heard = []

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()

        def dictate(audio, **context):
            heard.append(context["translate"])
            return Dictation("привет", "Hi", "chat", "ru", False, {"asr": 0, "llm": 0}, True)

        monkeypatch.setattr(d.dictator, "dictate", dictate)
        audio = base64.b64encode(np.zeros(1600, "<i2").tobytes()).decode()
        reply = await d._handle({"cmd": "dictate", "audio": audio, "translate": True}, None)
        assert (reply["text"], reply["translate"]) == ("Hi", True)

    asyncio.run(scenario())
    assert heard == [True]


def test_processing_a_missing_file_fails_and_an_existing_one_is_queued(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        with pytest.raises(FileNotFoundError):
            await d._handle({"cmd": "process", "path": str(tmp_path / "missing.wav")}, None)
        audio = tmp_path / "meeting.mp3"
        audio.write_bytes(b"")
        reply = await d._handle({"cmd": "process", "path": str(audio)}, None)
        assert reply["job"] == {
            "id": 1,
            "audio": str(audio),
            "state": "queued",
            "stage": "",
            "out_dir": None,
            "error": None,
        }
        assert d.queue.qsize() == 1

    asyncio.run(scenario())


def test_jobs_run_after_models_load_and_report_failures(tmp_path, monkeypatch):
    def fake_process(audio, models, out_root, *, on_stage, **_):
        on_stage("Transcribing")
        if audio.name == "broken.wav":
            raise RuntimeError("ffmpeg failed")
        return type("Result", (), {"out_dir": out_root / audio.stem})()

    monkeypatch.setattr(daemon, "process", fake_process)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        for name in ("good.wav", "broken.wav"):
            (tmp_path / name).write_bytes(b"")
            d._enqueue(tmp_path / name)
        working = asyncio.create_task(d._work())
        await asyncio.sleep(0.05)
        assert [j.state for j in d.jobs] == ["queued", "queued"]

        d.ready.set()
        await asyncio.sleep(0.2)
        good, broken = d.jobs
        assert (good.state, good.out_dir) == ("done", str(tmp_path / "output" / "good"))
        assert (broken.state, broken.error) == ("failed", "ffmpeg failed")
        working.cancel()

    asyncio.run(scenario())


def test_pyannote_is_unloaded_once_the_queue_is_empty(tmp_path, monkeypatch):
    unloaded = []

    def fake_process(audio, *_, **__):
        unloaded.append(audio.name)
        if audio.name == "broken.wav":
            raise RuntimeError("ffmpeg failed")
        return type("Result", (), {"out_dir": tmp_path})()

    monkeypatch.setattr(daemon, "process", fake_process)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.models, "unload", unloaded.append)
        for name in ("good.wav", "broken.wav"):
            (tmp_path / name).write_bytes(b"")
            d._enqueue(tmp_path / name)
        d.ready.set()
        working = asyncio.create_task(d._work())
        await asyncio.sleep(0.2)
        assert unloaded == ["good.wav", "broken.wav", "diarizer"]
        working.cancel()

    asyncio.run(scenario())


def test_a_failed_warm_up_is_logged(tmp_path, monkeypatch, caplog):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())

        def broken():
            raise RuntimeError("Tensor on device cpu is not on the expected device meta!")

        monkeypatch.setattr(d.dictator, "warm_up", broken)
        await d._warm_up()
        assert not d.ready.is_set()

    asyncio.run(scenario())
    assert "Loading models failed" in caplog.text
    assert "expected device meta" in caplog.text


def test_learning_can_be_turned_off(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.dictation.learn = False
        reply = await d._handle({"cmd": "correction", "original": "Залей на гитхаб", "edited": "Залей на GitHub"}, None)
        assert reply == {"learned": []}
        assert not (tmp_path / "vocabulary.json").exists()

    asyncio.run(scenario())


def test_client_reports_a_missing_or_lost_daemon(monkeypatch, socket_path):
    async def hang_up(reader, writer):
        await reader.readline()
        writer.close()

    async def scenario():
        with pytest.raises(DaemonError, match="not running"):
            await DaemonClient.connect()

        server = await asyncio.start_unix_server(hang_up, path=str(socket_path))
        connected = await DaemonClient.connect()
        with pytest.raises(DaemonError, match="lost connection"):
            await connected.request("status")
        await connected.close()
        server.close()

    asyncio.run(scenario())


def _loud_models():
    """Hears every non-zero sample and names each span by its start."""

    def speech_spans(audio):
        loud = np.flatnonzero(np.diff(np.concatenate([[0], audio != 0, [0]]).astype(int)))
        return [(start / 16_000, end / 16_000) for start, end in zip(loud[::2], loud[1::2], strict=True)]

    asr = SimpleNamespace(transcribe_spans=lambda audio, spans: [f"{start:g}" for start, _ in spans])
    return SimpleNamespace(vad=SimpleNamespace(speech_spans=speech_spans), asr_for=lambda _: asr)


def _pcm(audio):
    return base64.b64encode((audio * 32767).astype("<i2").tobytes()).decode()


def test_streamed_dictation_matches_a_whole_one(tmp_path, monkeypatch, socket_path):
    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)
    # Under the 64 KB line limit of the test server: speech 0.3 s, silence 0.7 s, speech 0.3 s.
    audio = np.concatenate(
        [np.full(int(s * 16_000), loud, np.float32) for s, loud in ((0.3, 0.5), (0.7, 0), (0.3, 0.5))]
    )

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        d.dictator.models = _loud_models()
        d.dictator.cfg.cleanup = False
        d.dictator.language = "ru"
        async with _serving(d, socket_path) as connected:
            whole = await connected.dictate(audio, app="ru.keepcoder.Telegram")
            await connected.request("dictate_audio", audio=_pcm(np.ones(8_000, np.float32)), first=True)
            await connected.request("dictate_audio", audio=_pcm(audio[:9_600]), first=True)
            await connected.request("dictate_audio", audio=_pcm(audio[9_600:17_600]))
            await asyncio.sleep(0.1)
            streamed = await connected.request(
                "dictate", audio=_pcm(audio[17_600:]), streamed=True, app="ru.keepcoder.Telegram"
            )
            with pytest.raises(DaemonError, match="no dictation"):
                await connected.request("dictate", audio=_pcm(audio), streamed=True)
            await connected.request("dictate_audio", audio=_pcm(audio), first=True)
        await asyncio.sleep(0.1)
        assert d.streams == {}
        return whole, streamed

    whole, streamed = asyncio.run(scenario())
    assert streamed["raw"] == whole["raw"] == "0 1"
    assert "asr_live" in streamed["timings"]
    del whole["time"], streamed["time"], whole["timings"], streamed["timings"]
    assert streamed == whole
    assert [entry["raw"] for entry in history] == ["0 1", "0 1"]


def test_the_first_chunk_loads_an_unloaded_dictation_llm_once(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        d.dictator.models = _loud_models()
        warmed, loading = [], threading.Event()
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: warmed.append(loading.wait(1)))
        chunk = {"cmd": "dictate_audio", "audio": _pcm(np.zeros(16_000, np.float32))}
        await d._handle({**chunk, "first": True}, None)
        await d._handle(chunk, None)
        await d._handle({**chunk, "first": True}, None)  # the next dictation while the model still loads
        loading.set()
        await asyncio.sleep(0.1)
        assert warmed == [True]

        await d._handle({**chunk, "first": True}, None)  # unloaded again after the idle time
        await asyncio.sleep(0.1)
        assert warmed == [True, True]

        monkeypatch.setattr(type(d.dictator.llm), "loaded", True)
        await d._handle({**chunk, "first": True}, None)
        await asyncio.sleep(0.1)
        assert warmed == [True, True]

    asyncio.run(scenario())
