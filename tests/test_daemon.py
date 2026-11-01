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
from wav2sum.calls import Speakers
from wav2sum.capture import CaptureError
from wav2sum.client import DaemonClient, DaemonError
from wav2sum.config import Config
from wav2sum.dictation import Dictation
from wav2sum.history import append_dictation, load_dictations

ZOOM = "us.zoom.xos"


class FakeRecorder:
    def __init__(self, path, mic, on_level, app=None):
        self.path, self.app, self.seconds = path, app, 0.0

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
    monkeypatch.setattr(daemon, "HISTORY_PATH", tmp_path / "dictations.jsonl")
    monkeypatch.setattr(daemon, "EDITS_PATH", tmp_path / "edits.jsonl")
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
        assert d.recorder.app == ZOOM

        await inputs.put([])
        await asyncio.sleep(0.4)
        assert d.recorder is None
        assert d.jobs == []
        assert d.queue.empty()
        assert len(list(tmp_path.glob("*.wav"))) == 1
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
        assert d.recorder.app is None
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


def test_edits_are_saved_and_become_cleanup_examples_after_a_restart(tmp_path, monkeypatch):
    dictated = {"raw": "ну залей на гитхаб", "text": "залей на гитхаб", "style": "chat", "language": "ru"}
    append_dictation({"time": "t", **dictated, "command": False}, tmp_path / "dictations.jsonl")

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        await d._handle({"cmd": "correction", "original": "залей на гитхаб", "edited": " залей на GitHub"}, None)
        await d._handle({"cmd": "correction", "original": "не из диктовки", "edited": "не из диктовки!"}, None)
        assert d.dictator.examples == {}
        [edit] = load_dictations(tmp_path / "edits.jsonl")
        assert edit == {"time": edit["time"], **dictated, "edited": "залей на GitHub"}
        restarted = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        assert restarted.dictator.examples == {("chat", "ru"): [("ну залей на гитхаб", "залей на GitHub")]}

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
            assert set(stopped) == {"path"}
            assert Path(stopped["path"]).parent == tmp_path
            assert Path(stopped["path"]).exists()
            assert (await connected.request("status"))["jobs"] == []

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
            reply = await dictating.dictate(audio, app="ru.keepcoder.Telegram", command=False, before="Я думаю")
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
            "before": "Я думаю",
            "after": None,
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


def test_recordings_without_a_result_are_not_queued_at_start(tmp_path, monkeypatch, socket_path):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.auto_record = False
        audio = tmp_path / "call-2.wav"
        audio.write_bytes(b"saved audio")
        output = tmp_path / "output" / "call-1"
        output.mkdir(parents=True)
        sentinel = output / "summary.md"
        sentinel.write_text("existing result")

        async def not_running():
            return False

        async def warm():
            d.ready.set()

        monkeypatch.setattr(daemon, "is_running", not_running)
        monkeypatch.setattr(d, "_warm_up", warm)
        monkeypatch.setattr(daemon, "SOCKET_PATH", socket_path)
        monkeypatch.setattr(d.dictator.llm, "unload", lambda: None)
        running = asyncio.create_task(d.run())
        while not hasattr(d, "stopping"):
            await asyncio.sleep(0)
        while not socket_path.exists():
            await asyncio.sleep(0.01)
        assert d.jobs == [] and d.queue.empty()
        assert audio.read_bytes() == b"saved audio"
        assert sentinel.read_text() == "existing result"
        d.stopping.set()
        await running

    asyncio.run(scenario())


def test_process_rejects_duplicate_jobs_and_occupied_output(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        audio = tmp_path / "meeting.wav"
        audio.write_bytes(b"audio")
        job = (await d._handle({"cmd": "process", "path": str(audio)}, None))["job"]
        assert job["state"] == "queued"
        with pytest.raises(ValueError, match=r"(?i)(already|queued|processing)"):
            await d._handle({"cmd": "process", "path": str(audio)}, None)
        d.jobs[0].state = "running"
        with pytest.raises(ValueError, match=r"(?i)(already|processing)"):
            await d._handle({"cmd": "process", "path": str(audio)}, None)
        alias = tmp_path / "alias.wav"
        alias.symlink_to(audio)
        with pytest.raises(ValueError, match=r"(?i)(already|ambiguous)"):
            await d._handle({"cmd": "process", "path": str(alias)}, None)
        other = tmp_path / "elsewhere" / "meeting.wav"
        other.parent.mkdir()
        other.write_bytes(b"other")
        with pytest.raises(ValueError, match=r"(?i)(stem|already|processing)"):
            await d._handle({"cmd": "process", "path": str(other)}, None)
        d.jobs[0].state = "failed"
        output = tmp_path / "output" / "meeting"
        output.mkdir(parents=True)
        sentinel = output / "partial.txt"
        sentinel.write_text("keep")
        with pytest.raises(ValueError, match=r"(?i)(output|result|exist)"):
            await d._handle({"cmd": "process", "path": str(audio)}, None)
        assert sentinel.read_text() == "keep"
        assert len(d.jobs) == 1

    asyncio.run(scenario())


def test_process_rejects_another_recording_with_the_same_stem(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        (tmp_path / "meeting.wav").write_bytes(b"recording")
        other = tmp_path / "elsewhere" / "meeting.mp3"
        other.parent.mkdir()
        other.write_bytes(b"different recording")
        with pytest.raises(ValueError, match=r"(?i)(stem|ambiguous)"):
            await d._handle({"cmd": "process", "path": str(other)}, None)
        assert d.jobs == []

    asyncio.run(scenario())


def test_process_rejects_active_and_finalizing_recording(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class SlowRecorder(FakeRecorder):
        def stop(self):
            entered.set()
            assert release.wait(5)
            return super().stop()

    monkeypatch.setattr(daemon, "CallRecorder", SlowRecorder)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(daemon, "CallRecorder", SlowRecorder)
        path = Path((await d._record_start())["path"])
        path.write_bytes(b"partial")
        with pytest.raises(ValueError, match=r"(?i)record"):
            await d._handle({"cmd": "process", "path": str(path)}, None)
        stopping = asyncio.create_task(d._record_stop())
        while not entered.is_set():
            await asyncio.sleep(0.01)
        try:
            with pytest.raises(ValueError, match=r"(?i)record"):
                await d._handle({"cmd": "process", "path": str(path)}, None)
        finally:
            release.set()
            await stopping
        assert d.jobs == []
        assert path.exists()

    asyncio.run(scenario())


def test_failed_stop_clears_recording_and_preserves_audio(tmp_path, monkeypatch):
    class FailedRecorder(FakeRecorder):
        def stop(self):
            self.path.write_bytes(b"partial audio")
            raise CaptureError("capture failed")

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(daemon, "CallRecorder", FailedRecorder)
        events = []
        monkeypatch.setattr(d, "_broadcast", events.append)
        path = Path((await d._record_start(auto=ZOOM))["path"])

        with pytest.raises(CaptureError, match="capture failed"):
            await d._record_stop()

        assert d.status()["recording"] is None
        assert events[-1] == {"event": "recording", "active": False, "path": str(path)}
        assert path.read_bytes() == b"partial audio"
        assert d.queue.empty()
        await d._record_start()
        assert d.status()["recording"] is not None

    asyncio.run(scenario())


def test_worker_rechecks_output_before_processing(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(daemon, "process", lambda *a, **kw: called.append(a))

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.models, "unload", lambda *_: None)
        audio = tmp_path / "meeting.wav"
        audio.write_bytes(b"audio")
        d._enqueue(audio)
        output = tmp_path / "output" / audio.stem
        output.mkdir(parents=True)
        sentinel = output / "summary.md"
        sentinel.write_text("preserve me")
        d.ready.set()
        worker = asyncio.create_task(d._work())
        for _ in range(100):
            if d.jobs[0].state == "failed":
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        assert d.jobs[0].state == "failed"
        assert called == []
        assert sentinel.read_text() == "preserve me"

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
            await d._handle({"cmd": "process", "path": str(tmp_path / name)}, None)
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


def test_recordings_are_diarized_with_a_range_of_people_on_the_system_channel(tmp_path, monkeypatch):
    passed = []

    def fake_process(audio, models, out_root, **kwargs):
        passed.append(kwargs["speakers"])
        return type("Result", (), {"out_dir": out_root / audio.stem})()

    monkeypatch.setattr(daemon, "process", fake_process)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        (tmp_path / "call.wav").write_bytes(b"")
        d._enqueue(tmp_path / "call.wav")
        d.ready.set()
        working = asyncio.create_task(d._work())
        await asyncio.sleep(0.2)
        assert [s.without_me() for s in passed] == [Speakers(None, 1, 6)]
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
        append_dictation({"raw": "залей на гитхаб", "text": "Залей на гитхаб"}, tmp_path / "dictations.jsonl")
        reply = await d._handle({"cmd": "correction", "original": "Залей на гитхаб", "edited": "Залей на GitHub"}, None)
        assert reply == {"learned": []}
        assert not (tmp_path / "vocabulary.json").exists()
        assert not (tmp_path / "edits.jsonl").exists()

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
