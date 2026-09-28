import logging

import numpy as np
from scipy.ndimage import maximum_filter1d, uniform_filter1d

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

FRAME = SAMPLE_RATE // 50
MAX_LAG_SEC = 0.5
SYSTEM_ACTIVE_DB = -55.0
MIN_COUPLING_DB = -40.0


def suppress_echo(mic: np.ndarray, system: np.ndarray, margin_db: float = 6.0) -> np.ndarray:
    n_frames = min(len(mic), len(system)) // FRAME
    if n_frames < 50:
        return mic

    em = _frame_energy(mic, n_frames)
    es = _frame_energy(system, n_frames)
    active = es > _db_to_power(SYSTEM_ACTIVE_DB)
    if active.sum() < 50:
        return mic

    lag = _estimate_lag(em, es, int(MAX_LAG_SEC * 50))
    es_lagged = np.zeros_like(es)
    es_lagged[lag:] = es[: n_frames - lag]
    active = es_lagged > _db_to_power(SYSTEM_ACTIVE_DB)

    coupling = np.median(em[active] / es_lagged[active])
    coupling_db = 10 * np.log10(coupling + 1e-20)
    if coupling_db < MIN_COUPLING_DB:
        logger.info("Echo: no speaker leak detected (%.0f dB) — mic left as is.", coupling_db)
        return mic

    es_lagged = maximum_filter1d(es_lagged, size=5)
    active = es_lagged > _db_to_power(SYSTEM_ACTIVE_DB)
    predicted_echo = coupling * es_lagged
    echo_only = active & (em < predicted_echo * _db_to_power(margin_db))
    keep = ~echo_only
    keep = maximum_filter1d(keep.astype(np.uint8), size=7).astype(bool)
    gain = uniform_filter1d(keep.astype(np.float32), size=3)

    out = mic.copy()
    out[: n_frames * FRAME] *= np.repeat(gain, FRAME)
    logger.info(
        "Echo: delay %d ms, leak %.0f dB → silenced %.0f%% of mic frames.",
        lag * 20,
        coupling_db,
        100 * (1 - keep.mean()),
    )
    return out


def _frame_energy(x: np.ndarray, n_frames: int) -> np.ndarray:
    frames = x[: n_frames * FRAME].reshape(n_frames, FRAME)
    return np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12


def _estimate_lag(em: np.ndarray, es: np.ndarray, max_lag: int) -> int:
    a = np.log(em)
    b = np.log(es)
    a = a - a.mean()
    b = b - b.mean()
    scores = [float(np.dot(a[lag:], b[: len(b) - lag])) / (len(b) - lag) for lag in range(max_lag + 1)]
    return int(np.argmax(scores))


def _db_to_power(db: float) -> float:
    return 10 ** (db / 10)
