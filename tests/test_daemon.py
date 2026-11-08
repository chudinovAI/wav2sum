import asyncio
import base64
import contextlib
import functools
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from audio_fakes import FakeVAD

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
        assert status["cloud"] is False

    asyncio.run(scenario())


def test_summary_model_is_cloud_only():
    local = daemon.Daemon(Config())
    assert local.summary_llm is None
    assert local.dictator.llm.cache_prefix
    cfg = Config()
    cfg.cloud = True
    cloud = daemon.Daemon(cfg)
    assert cloud.models.cloud
    assert cloud.summary_llm.model == cfg.summary_model


@pytest.mark.parametrize("cmd", ["subscribe", "dictate_audio", "dictate", "process"])
@pytest.mark.parametrize("cloud", [False, True])
def test_sensitive_mode_mismatch_rejected_before_processing(tmp_path, monkeypatch, cmd, cloud):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = cloud
        request = {"cmd": cmd, "cloud": not cloud, "audio": "not base64", "path": "missing"}
        with pytest.raises(ValueError, match="mode"):
            await d._handle(request, None)
        if cloud:
            with pytest.raises(ValueError, match="mode"):
                await d._handle({"cmd": cmd}, None)
        assert d.queue.empty()
        assert not d.streams

    asyncio.run(scenario())


def test_cloud_missing_credentials_rejected_before_decoding(tmp_path, monkeypatch):
    async def scenario():
        _daemon(tmp_path, monkeypatch, asyncio.Queue())
        cfg = Config(recordings_dir=tmp_path, output_dir=tmp_path / "output")
        cfg.cloud = True
        d = daemon.Daemon(cfg)
        d.queue = asyncio.Queue()
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY"):
            await d._handle({"cmd": "dictate_audio", "cloud": True, "audio": "not base64"}, None)
        assert d.queue.empty() and not d.streams

    asyncio.run(scenario())


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


@pytest.mark.parametrize("during_loading", [False, True])
def test_failed_startup_unblocks_dictation_without_decoding_or_running_models(tmp_path, monkeypatch, during_loading):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_up", lambda: (_ for _ in ()).throw(RuntimeError("missing model")))
        monkeypatch.setattr(d.dictator, "dictate", lambda *a, **k: pytest.fail("failed models must not run"))
        monkeypatch.setattr(daemon, "append_dictation", lambda *a, **k: pytest.fail("no history on startup failure"))
        request = {"cmd": "dictate", "audio": "not valid PCM"}
        waiting = asyncio.create_task(d._handle(request, None)) if during_loading else None
        if waiting is not None:
            await asyncio.sleep(0)
            assert not waiting.done()
        await d._warm_up()
        if waiting is None:
            waiting = asyncio.create_task(d._handle(request, None))
        with pytest.raises(RuntimeError, match="Selected models"):
            await asyncio.wait_for(waiting, 0.2)
        assert d.status()["state"] == "failed" and not d.ready.is_set()
        with pytest.raises(RuntimeError, match="Selected models"):
            d._dictate_audio({"first": True, "audio": "not valid PCM"}, None)
        assert not d.streams

    asyncio.run(scenario())


@pytest.mark.parametrize("during_loading", [False, True])
def test_failed_startup_marks_waiting_jobs_failed_without_processing(tmp_path, monkeypatch, during_loading):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_up", lambda: (_ for _ in ()).throw(RuntimeError("missing model")))
        monkeypatch.setattr(daemon, "process", lambda *a, **k: pytest.fail("failed models must not process a job"))
        monkeypatch.setattr(d.models, "unload", lambda *a: None)
        job = daemon.Job(1, str(tmp_path / "never-read.wav"))
        d.jobs.append(job)
        d.queue.put_nowait(job)
        failed = asyncio.Event()
        update = d._update

        def updated(*args, **kwargs):
            update(*args, **kwargs)
            failed.set()

        monkeypatch.setattr(d, "_update", updated)
        worker = asyncio.create_task(d._work()) if during_loading else None
        try:
            if worker is not None:
                await asyncio.sleep(0)
                assert job.state == "queued"
            await d._warm_up()
            if worker is None:
                worker = asyncio.create_task(d._work())
            await asyncio.wait_for(failed.wait(), 0.2)
            assert job.state == "failed" and "Selected models" in job.error
            assert job.out_dir is None
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


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

    asr = SimpleNamespace(transcribe_spans=lambda audio, spans, language=None: [f"{start:g}" for start, _ in spans])
    return SimpleNamespace(vad=FakeVAD(speech_spans), dictation_asr=asr)


def _pcm(audio):
    return base64.b64encode((audio * 32767).astype("<i2").tobytes()).decode()


def test_preview_is_raw_origin_only_deduplicated_and_not_persisted(tmp_path, monkeypatch, socket_path):
    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        d.ready.set()
        d.dictator.models = _loud_models()
        d.dictator.language = "ru"
        d.dictator.cfg.cleanup = False
        async with _serving(d, socket_path) as origin, _serving(d, socket_path) as other:
            await other.request("subscribe")
            audio = _pcm(np.full(8_000, 0.5, np.float32))
            await origin.request("dictate_audio", first=True, session="one", audio=audio)
            assert await asyncio.wait_for(origin._events.get(), 2) == {
                "event": "dictation_preview",
                "session": "one",
                "text": "0",
            }
            await origin.request("dictate_audio", session="one", audio=audio)
            await asyncio.sleep(0.1)
            assert origin._events.empty() and other._events.empty()
            assert history == []
            reply = await origin.request("dictate", streamed=True, session="one", audio="")
            assert reply["raw"] == "0"
            assert [entry["raw"] for entry in history] == ["0"]

    asyncio.run(scenario())


def test_sessionless_clients_do_not_recognize_tentative_audio_or_receive_preview(tmp_path, monkeypatch, socket_path):
    monkeypatch.setattr(daemon, "append_dictation", lambda entry: None)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        d.ready.set()
        d.dictator.models = _loud_models()
        d.dictator.language = "ru"
        d.dictator.cfg.cleanup = False
        async with _serving(d, socket_path) as origin:
            audio = _pcm(np.full(8_000, 0.5, np.float32))
            await origin.request("dictate_audio", first=True, audio=audio)
            reply = await origin.request("dictate", streamed=True, audio="")
            assert reply["raw"] == "0"
            assert origin._events.empty()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["cancel", "replace", "disconnect"])
def test_inflight_preview_is_discarded_on_cancel_replacement_or_disconnect(tmp_path, monkeypatch, socket_path, action):
    history, entered, release = [], threading.Event(), threading.Event()
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    def blocked_step(self, *, preview=False):
        entered.set()
        assert release.wait(2)
        return "stale"

    monkeypatch.setattr(daemon.DictationStream, "step", blocked_step)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        async with _serving(d, socket_path) as origin:
            audio = _pcm(np.full(8_000, 0.5, np.float32))
            await origin.request("dictate_audio", first=True, session="old", audio=audio)
            assert await asyncio.to_thread(entered.wait, 2)
            if action == "cancel":
                await origin.request("dictate_cancel", session="old")
            elif action == "replace":
                # Keep the replacement from starting a new worker until after this assertion.
                d.ready.clear()
                await origin.request("dictate_audio", first=True, session="new", audio=audio)
            else:
                await origin.close()
                await asyncio.sleep(0.05)
            release.set()
            await asyncio.sleep(0.1)
            assert origin._events.empty() or action == "disconnect"
            assert history == []

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_sessions_cannot_cancel_append_or_finish_another_stream(tmp_path, monkeypatch, socket_path):
    monkeypatch.setattr(daemon, "append_dictation", lambda entry: None)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        d.ready.set()
        d.dictator.models = _loud_models()
        d.dictator.language = "ru"
        d.dictator.cfg.cleanup = False
        async with _serving(d, socket_path) as origin:
            audio = _pcm(np.full(8_000, 0.5, np.float32))
            await origin.request("dictate_audio", first=True, session="current", audio=audio)
            await origin.request("dictate_cancel", session="old")
            with pytest.raises(DaemonError, match="session"):
                await origin.request("dictate_audio", session="old", audio=audio)
            with pytest.raises(DaemonError, match="session"):
                await origin.request("dictate", streamed=True, session="old", audio="")
            reply = await origin.request("dictate", streamed=True, session="current", audio="")
            assert reply["raw"] == "0" and reply["seconds"] == 0.5

    asyncio.run(scenario())


def test_audio_arriving_during_recognition_gets_a_later_preview(tmp_path, monkeypatch, socket_path):
    entered, release = threading.Event(), threading.Event()

    def slow_step(self, *, preview=False):
        samples = self.samples
        if not entered.is_set():
            entered.set()
            assert release.wait(2)
        return str(samples)

    monkeypatch.setattr(daemon.DictationStream, "step", slow_step)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.ready.set()
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        async with _serving(d, socket_path) as origin:
            audio = _pcm(np.full(8_000, 0.5, np.float32))
            await origin.request("dictate_audio", first=True, session="current", audio=audio)
            assert await asyncio.to_thread(entered.wait, 2)
            await origin.request("dictate_audio", session="current", audio=audio)
            release.set()
            assert (await asyncio.wait_for(origin._events.get(), 2))["text"] == "8000"
            assert (await asyncio.wait_for(origin._events.get(), 2))["text"] == "16000"
            await origin.request("dictate_cancel", session="current")

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_preview_worker_failure_is_reported_by_final_request(tmp_path, monkeypatch, socket_path):
    def broken_step(self, *, preview=False):
        raise ValueError("asr failed")

    monkeypatch.setattr(daemon.DictationStream, "step", broken_step)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        monkeypatch.setattr(d.dictator, "warm_llm", lambda: None)
        d.ready.set()
        async with _serving(d, socket_path) as origin:
            await origin.request("dictate_audio", first=True, session="current", audio="")
            with pytest.raises(DaemonError, match="asr failed"):
                await origin.request("dictate", streamed=True, session="current", audio="")

    asyncio.run(scenario())


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
            await connected.request("dictate_audio", audio=_pcm(audio[:9_600]), first=True, app="ru.keepcoder.Telegram")
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


def test_cancel_during_final_cleanup_is_responsive_and_never_persists(tmp_path, monkeypatch, socket_path):
    entered, release = threading.Event(), threading.Event()
    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    def clean(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return "0"

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.dictator.models = _loud_models()
        d.dictator.llm = SimpleNamespace(chat=clean, loaded=True)
        d.ready.set()
        async with _serving(d, socket_path) as connected:
            await connected.request("dictate_audio", first=True, session="old", audio=_pcm(np.ones(8000)))
            pending = asyncio.create_task(connected.request("dictate", streamed=True, session="old", audio=""))
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(connected.request("dictate_cancel", session="old"), 0.5)
            await connected.request("dictate_audio", first=True, session="new", audio=_pcm(np.ones(4000)))
            release.set()
            with pytest.raises(DaemonError, match="cancel"):
                await pending
            assert next(iter(d.streams.values())).session == "new"
            await connected.request("dictate_cancel", session="new")
            assert history == []

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_pipelined_sessionless_final_cannot_capture_replacement(tmp_path, monkeypatch, socket_path):
    import json

    history = []
    monkeypatch.setattr(daemon, "append_dictation", history.append)

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.dictator.models = _loud_models()
        d.dictator.cfg.cleanup = False
        d.ready.set()
        async with _serving(d, socket_path):
            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            requests = [
                {"id": 1, "cmd": "dictate_audio", "first": True, "audio": _pcm(np.ones(8000))},
                {"id": 2, "cmd": "dictate", "streamed": True, "audio": _pcm(np.ones(2000))},
                {"id": 3, "cmd": "dictate_audio", "first": True, "audio": _pcm(np.ones(4000))},
                {"id": 4, "cmd": "dictate", "streamed": True, "audio": ""},
            ]
            writer.write("".join(json.dumps(r) + "\n" for r in requests).encode())
            await writer.drain()
            replies = {}
            for _ in requests:
                reply = json.loads(await asyncio.wait_for(reader.readline(), 3))
                replies[reply["id"]] = reply
            writer.close()
            await writer.wait_closed()
            assert replies[2]["ok"] is False
            assert replies[4]["ok"] is True
            assert replies[4]["seconds"] == 0.2
            assert [entry["seconds"] for entry in history] == [0.2]

    asyncio.run(scenario())


def test_cancelled_model_work_finishes_before_cleanup_and_remains_cancelled():
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release, finished = threading.Event(), threading.Event()

        def work():
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(2)
                raise ValueError("synthetic model failure after cancellation")
            finally:
                finished.set()

        task = asyncio.create_task(daemon._finish_thread(work))
        await entered.wait()
        task.cancel()
        try:
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def _saved_meeting(d, name, started_at, *, transcript=None, summary=None):
    import json

    audio = d.cfg.recordings_dir / name
    audio.write_bytes(b"synthetic recording")
    audio.with_suffix(".json").write_text(json.dumps({"started_at": started_at, "duration_sec": 5, "layout": "call"}))
    folder = d.cfg.output_dir / audio.stem
    if transcript is not None:
        folder.mkdir(parents=True)
        (folder / "transcript.txt").write_text(transcript)
        (folder / "meta.json").write_text(json.dumps({"audio": str(audio.resolve()), "language": "en"}))
    if summary is not None:
        (folder / "summary.md").write_text(summary)
    return audio, folder


def test_last_meeting_summary_uses_recording_chronology_and_deduplicates(tmp_path, monkeypatch):
    import os

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = True
        d.summary_llm = SimpleNamespace(validate_credentials=lambda: None)
        old, old_folder = _saved_meeting(
            d, "old.wav", "2026-11-01T12:00:00Z", transcript="[00:00 – 00:05] Me: old", summary="keep old"
        )
        newest, folder = _saved_meeting(d, "new.wav", "2026-11-02T12:00:00Z", transcript="[00:00 – 00:05] Me: new")
        os.utime(old, (2_000_000_000, 2_000_000_000))
        os.utime(newest, (1_000_000_000, 1_000_000_000))
        first = await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        second = await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert first["job"]["audio"] == str(newest.resolve())
        assert first["job"]["out_dir"] == str(folder)
        assert second["job"]["id"] == first["job"]["id"]
        assert d.queue.qsize() == 1
        assert (old_folder / "summary.md").read_text() == "keep old"

    asyncio.run(scenario())


def test_last_meeting_existing_summary_opens_without_credentials_or_new_job(tmp_path, monkeypatch):
    def no_keys():
        pytest.fail("opening an existing summary must not require API credentials")

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = True
        d.summary_llm = SimpleNamespace(validate_credentials=no_keys)
        _, folder = _saved_meeting(d, "last.wav", "2026-11-02T12:00:00Z", transcript="saved", summary="existing")
        result = await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert result == {"out_dir": str(folder), "already_done": True}
        assert d.queue.empty()
        assert (folder / "summary.md").read_text() == "existing"

    asyncio.run(scenario())


def test_last_meeting_raw_audio_queues_normal_processing(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = True
        d.summary_llm = SimpleNamespace(validate_credentials=lambda: None)
        audio, _ = _saved_meeting(d, "raw.wav", "2026-11-02T12:00:00Z")
        result = await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert result["job"]["audio"] == str(audio.resolve())
        assert result["job"]["out_dir"] is None
        assert d.queue.qsize() == 1

    asyncio.run(scenario())


def test_last_meeting_rejects_local_active_empty_and_partial_results(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        with pytest.raises(ValueError, match="cloud"):
            await d._handle({"cmd": "summarize_last_meeting", "cloud": False}, None)
        d.cfg.cloud = True
        d.summary_llm = SimpleNamespace(validate_credentials=lambda: None)
        with pytest.raises(FileNotFoundError):
            await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        audio, folder = _saved_meeting(d, "unfinished.wav", "2026-11-02T12:00:00Z")
        d.recorder = FakeRecorder(audio, None, lambda _: None)
        with pytest.raises(ValueError, match="Recording"):
            await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        d.recorder = None
        folder.mkdir(parents=True)
        (folder / "transcript.txt").write_text("partial")
        with pytest.raises(ValueError, match=r"[Ii]ncomplete|already exists"):
            await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert d.queue.empty()
        assert (folder / "transcript.txt").read_text() == "partial"

    asyncio.run(scenario())


def test_last_meeting_mode_mismatch_is_rejected_before_selection(tmp_path, monkeypatch):
    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        with pytest.raises(ValueError, match="mode mismatch"):
            await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert d.queue.empty()

    asyncio.run(scenario())


def test_retention_runs_off_dictation_path_and_refreshes_subscribers(tmp_path, monkeypatch):
    import json

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        path = tmp_path / "dictations.jsonl"
        kept = {"time": "2099-01-01T00:00:00Z", "text": "keep"}
        append_dictation({"time": "2020-01-01T00:00:00Z", "text": "old"}, path)
        append_dictation(kept, path)
        edits = tmp_path / "edits.jsonl"
        edits.write_text('{"text":"learned example"}\n')
        notified = asyncio.Event()
        messages = []

        def broadcast(event):
            messages.append(event)
            notified.set()

        d._broadcast = broadcast
        task = asyncio.create_task(d._maintain_history())
        try:
            await asyncio.wait_for(notified.wait(), 3)
            assert [json.loads(line) for line in path.read_text().splitlines()] == [kept]
            assert messages == [{"event": "history_pruned", "deleted": 1}]
            assert edits.read_text() == '{"text":"learned example"}\n'
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_last_meeting_skips_unfinalized_capture_sidecar(tmp_path, monkeypatch):
    import json

    async def scenario():
        d = _daemon(tmp_path, monkeypatch, asyncio.Queue())
        d.cfg.cloud = True
        d.summary_llm = SimpleNamespace(validate_credentials=lambda: None)
        _, completed = _saved_meeting(
            d, "completed.wav", "2026-11-01T12:00:00Z", transcript="saved", summary="completed summary"
        )
        unfinished = tmp_path / "unfinished.wav"
        unfinished.write_bytes(b"partial capture")
        unfinished.with_suffix(".json").write_text(json.dumps({"layout": "call", "started_at": "2026-11-02T12:00:00Z"}))
        result = await d._handle({"cmd": "summarize_last_meeting", "cloud": True}, None)
        assert result == {"out_dir": str(completed), "already_done": True}
        assert d.queue.empty()
        assert unfinished.read_bytes() == b"partial capture"

    asyncio.run(scenario())
