import asyncio

import pytest

from wav2sum import daemon
from wav2sum.config import Config


class Recorder:
    def __init__(self, path, fail=False):
        self.path = path
        self.fail = fail

    def stop(self):
        if self.fail:
            raise daemon.CaptureError("synthetic capture failure")
        self.path.write_bytes(b"synthetic")
        return self.path


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("automatic", [False, True])
def test_auto_processing_is_explicit_and_uses_selected_mode(tmp_path, cloud, automatic):
    async def scenario():
        cfg = Config(recordings_dir=tmp_path, output_dir=tmp_path / "out", auto_process=automatic)
        cfg.cloud = cloud
        cfg.auto_cloud_consent = cloud and automatic
        d = daemon.Daemon(cfg)
        d.queue = asyncio.Queue()
        d.recorder = Recorder(tmp_path / "meeting.wav")
        d.auto_app = "synthetic-call"
        reply = await d._record_stop()
        assert reply["path"] == str(tmp_path / "meeting.wav")
        assert d.recorder is None
        assert len(d.jobs) == int(automatic)
        if automatic:
            assert d.jobs[0].audio == reply["path"]
            assert d.jobs[0].state == "queued"
            assert d.cfg.cloud is cloud
            assert (d.summary_llm is not None) is cloud

    asyncio.run(scenario())


def test_cloud_auto_processing_does_not_reuse_local_only_consent(tmp_path):
    async def scenario():
        cfg = Config(recordings_dir=tmp_path, auto_process=True)
        cfg.cloud = True
        d = daemon.Daemon(cfg)
        d.queue = asyncio.Queue()
        d.recorder = Recorder(tmp_path / "meeting.wav")
        await d._record_stop()
        assert d.jobs == []
        assert d.queue.empty()

    asyncio.run(scenario())


def test_failed_capture_does_not_enqueue_or_leave_recording_active(tmp_path):
    async def scenario():
        d = daemon.Daemon(Config(recordings_dir=tmp_path, auto_process=True))
        d.queue = asyncio.Queue()
        d.recorder = Recorder(tmp_path / "meeting.wav", fail=True)
        with pytest.raises(daemon.CaptureError):
            await d._record_stop()
        assert not d.jobs and d.recorder is None
        assert not (tmp_path / "meeting.wav").exists()

    asyncio.run(scenario())
