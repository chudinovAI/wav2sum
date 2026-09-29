import asyncio
import functools

from wav2sum import daemon
from wav2sum.autorecord import CallDetector
from wav2sum.config import Config

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
    d = daemon.Daemon(Config(recordings_dir=tmp_path))
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
