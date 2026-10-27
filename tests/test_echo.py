import numpy as np

from wav2sum.audio import SAMPLE_RATE
from wav2sum.echo import suppress_echo

SR = SAMPLE_RATE


def _burst(rng, seconds, amp):
    n = int(seconds * SR)
    env = 0.5 + 0.5 * np.sin(np.linspace(0, seconds * 2 * np.pi * 4, n)) ** 2
    return (rng.normal(0, amp, n) * env).astype(np.float32)


def _speech(rng, seconds, amp):
    syllables = np.repeat(rng.random(int(seconds * 10)) > 0.3, SR // 10)
    return (rng.normal(0, amp, len(syllables)) * syllables).astype(np.float32)


def _rms_db(x):
    return 10 * np.log10(np.mean(x.astype(np.float64) ** 2) + 1e-12)


def _scene(leak: float):
    rng = np.random.default_rng(0)
    n = 10 * SR
    system = np.zeros(n, np.float32)
    mine = np.zeros(n, np.float32)
    system[0 : 4 * SR] = _burst(rng, 4, 0.1)
    mine[5 * SR : 9 * SR] = _burst(rng, 4, 0.1)
    delay = int(0.06 * SR)
    mic = mine.copy()
    mic[delay:] += leak * system[:-delay]
    mic += rng.normal(0, 1e-4, n).astype(np.float32)
    return mic, system


def test_echo_is_silenced_and_my_speech_kept():
    mic, system = _scene(leak=0.2)
    out = suppress_echo(mic, system)
    their_part = slice(int(0.3 * SR), int(3.7 * SR))
    my_part = slice(5 * SR, 9 * SR)
    assert _rms_db(out[their_part]) < _rms_db(mic[their_part]) - 30
    np.testing.assert_allclose(out[my_part], mic[my_part])


def test_no_leak_leaves_mic_untouched():
    mic, system = _scene(leak=0.0)
    np.testing.assert_array_equal(suppress_echo(mic, system), mic)


def test_echo_is_cancelled_under_my_speech():
    rng = np.random.default_rng(1)
    n = 10 * SR
    system = np.zeros(n, np.float32)
    mine = np.zeros(n, np.float32)
    system[0 : 8 * SR] = _speech(rng, 8, 0.1)
    mine[5 * SR : 9 * SR] = _speech(rng, 4, 0.1)
    room = rng.normal(0, 1, int(0.05 * SR)) * np.exp(-np.arange(int(0.05 * SR)) / (0.01 * SR))
    room *= 0.2 / np.sqrt(np.sum(room**2))
    echo = np.convolve(np.concatenate([np.zeros(int(0.06 * SR)), system]), room)[:n].astype(np.float32)
    mic = mine + echo + rng.normal(0, 1e-4, n).astype(np.float32)
    out = suppress_echo(mic, system)
    both = slice(int(5.5 * SR), int(7.5 * SR))
    assert _rms_db(out[both] - mine[both]) < _rms_db(echo[both]) - 20
    assert abs(_rms_db(out[both]) - _rms_db(mine[both])) < 0.5
    np.testing.assert_allclose(out[int(8.5 * SR) : 9 * SR], mic[int(8.5 * SR) : 9 * SR])
