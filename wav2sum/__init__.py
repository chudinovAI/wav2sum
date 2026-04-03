from __future__ import annotations

import torch

__version__ = "0.2.0"


def detect_device(requested: str | None = None) -> str:
    """Pick the best available compute device."""
    if requested:
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"
