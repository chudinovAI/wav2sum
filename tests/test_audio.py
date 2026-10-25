import shutil

import numpy as np
import pytest
from scipy.io import wavfile

from wav2sum.asr import MAX_BATCH_ITEMS, _batches
from wav2sum.audio import SAMPLE_RATE, file_hash, load_audio, pcm16_to_float


def test_pcm16_maps_to_unit_range():
    pcm = np.array([-32768, 0, 16384, 32767], "<i2").tobytes()
    np.testing.assert_allclose(pcm16_to_float(pcm), [-1.0, 0.0, 0.5, 32767 / 32768])


def test_file_hash_follows_content(tmp_path):
    a, b, c = tmp_path / "a.wav", tmp_path / "b.wav", tmp_path / "c.wav"
    a.write_bytes(b"x" * (3 << 20))
    b.write_bytes(b"x" * (3 << 20))
    c.write_bytes(b"x" * (3 << 20) + b"y")
    assert file_hash(a) == file_hash(b) != file_hash(c)


def test_missing_audio_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_audio(tmp_path / "nope.wav", channels=1)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_stereo_recording_is_resampled_and_keeps_channels(tmp_path):
    rate = 48_000
    t = np.arange(rate) / rate
    stereo = np.stack([0.5 * np.sin(2 * np.pi * 440 * t), np.zeros(rate)], axis=1)
    path = tmp_path / "call.wav"
    wavfile.write(path, rate, (stereo * 32767).astype(np.int16))

    audio = load_audio(path, channels=2)

    assert audio.shape == (SAMPLE_RATE, 2)
    assert 0.3 < np.abs(audio[:, 0]).max() < 0.55
    assert np.abs(audio[:, 1]).max() < 1e-3


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_unreadable_audio_names_the_file(tmp_path):
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"not audio")
    with pytest.raises(RuntimeError, match=r"broken\.mp3"):
        load_audio(path, channels=1)


def test_asr_batches_cover_every_span_within_limits():
    spans = [(0.0, 15.0)] * 12 + [(0.0, 2.0)] * 30
    batches = list(_batches(spans))
    assert sorted(i for batch in batches for i in batch) == list(range(len(spans)))
    for batch in batches:
        assert len(batch) <= MAX_BATCH_ITEMS
        assert sum(spans[i][1] - spans[i][0] for i in batch) <= 160
    assert set(batches[0]) <= set(range(12))
