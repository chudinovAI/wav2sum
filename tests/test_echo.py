import numpy as np

from wav2sum.audio import SAMPLE_RATE
from wav2sum.echo import suppress_echo

SR = SAMPLE_RATE


def _burst(rng, seconds, amp):
    n = int(seconds * SR)
    env = 0.5 + 0.5 * np.sin(np.linspace(0, seconds * 2 * np.pi * 4, n)) ** 2
    return (rng.normal(0, amp, n) * env).astype(np.float32)


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
