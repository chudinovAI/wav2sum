import json
import sys
import textwrap

import pytest

from wav2sum import capture
from wav2sum.capture import CallRecorder, CaptureError, new_recording_path

HELPER = """\
#!{python}
import json, signal, sys, time

def say(**event):
    print(json.dumps(event), file=sys.stderr, flush=True)

args = sys.argv[1:]
open(args[0] + ".args", "w").write(json.dumps(args[1:]))
if "--mic" in args and args[args.index("--mic") + 1] == "missing":
    say(event="error", message="no microphone named missing")
    sys.exit(1)

stopping = False
def stop(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGINT, stop)

print("capture: some diagnostic line", file=sys.stderr, flush=True)
say(event="started", mic="MacBook Pro Microphone")
say(event="level", t=6.0, mic_db={mic_db}, sys_db=-20.0)
while not stopping:
    time.sleep(0.01)
open(args[0], "wb").write(b"RIFF")
say(event="stopped", seconds=6.5)
"""


@pytest.fixture
def helper(tmp_path, monkeypatch):
    def install(mic_db=-30.0):
        path = tmp_path / "wav2sum-capture"
        path.write_text(textwrap.dedent(HELPER).format(python=sys.executable, mic_db=mic_db))
        path.chmod(0o755)
        monkeypatch.setattr(capture, "capture_helper", lambda: path)

    return install


def test_recording_writes_audio_and_sidecar(tmp_path, helper):
    helper()
    levels = []
    recorder = CallRecorder(tmp_path / "recordings" / "call.wav", on_level=levels.append)

    recorder.start()
    started = json.loads(recorder.path.with_suffix(".json").read_text())
    wav = recorder.stop()

    assert started["layout"] == "call" and "duration_sec" not in started
    assert wav.read_bytes() == b"RIFF"
    sidecar = json.loads(wav.with_suffix(".json").read_text())
    assert sidecar | {"started_at": None} == {
        "layout": "call",
        "channels": ["mic", "system"],
        "mic": "MacBook Pro Microphone",
        "started_at": None,
        "duration_sec": 6.5,
    }
    assert [(lv.seconds, lv.mic_db, lv.sys_db) for lv in levels] == [(6.0, -30.0, -20.0)]


def test_call_app_is_passed_to_the_helper(tmp_path, helper):
    helper()
    recorder = CallRecorder(tmp_path / "call.wav", app="us.zoom.xos")
    recorder.start()
    recorder.stop()
    assert json.loads((tmp_path / "call.wav.args").read_text()) == ["--app", "us.zoom.xos"]


def test_helper_error_is_raised_on_start(tmp_path, helper):
    helper()
    with pytest.raises(CaptureError, match="no microphone named missing"):
        CallRecorder(tmp_path / "call.wav", mic="missing").start()


def test_silent_microphone_is_reported(tmp_path, helper, caplog):
    helper(mic_db=-120.0)
    recorder = CallRecorder(tmp_path / "call.wav")
    recorder.start()
    recorder.stop()
    assert "microphone was silent" in caplog.text
    assert "System audio was silent" not in caplog.text


def test_recording_names_sort_by_time(tmp_path):
    path = new_recording_path(tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("call-20") and path.suffix == ".wav"
