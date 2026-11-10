import logging
import threading

import numpy as np
import torch
from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

QWEN_MODEL_ID = "Qwen/Qwen3-ASR-1.7B-hf"
QWEN_REVISION = "bcd2b5b7f32b480ab5790554cfa8347f246a14f3"
QWEN_MAX_NEW_TOKENS = 1024

MAX_SPAN_SEC = 20.0


class QwenTranscriber:
    """Native Qwen3-ASR transcription for dictation."""

    def __init__(self, device: str, lock: "threading.Lock | None" = None):
        self.device = device
        self.dtype = torch.float16 if torch.device(device).type in ("mps", "cuda") else torch.float32
        self.lock = lock or threading.Lock()
        logger.info("Loading Qwen3-ASR on %s …", device)
        self.processor = AutoProcessor.from_pretrained(QWEN_MODEL_ID, revision=QWEN_REVISION, local_files_only=True)
        self.model = (
            Qwen3ASRForConditionalGeneration.from_pretrained(
                QWEN_MODEL_ID, revision=QWEN_REVISION, dtype=self.dtype, local_files_only=True
            )
            .to(device)
            .eval()
        )

    def transcribe(self, audio: np.ndarray, language: str | None = None, *, terms: tuple[str, ...] = ()) -> str:
        return self.transcribe_spans(audio, [(0.0, len(audio) / SAMPLE_RATE)], language=language, terms=terms)[0]

    def transcribe_spans(
        self,
        audio: np.ndarray,
        spans: list[tuple[float, float]],
        language: str | None = None,
        *,
        terms: tuple[str, ...] = (),
    ) -> list[str]:
        texts = []
        for start, end in spans:
            clip = audio[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
            if not len(clip):
                texts.append("")
                continue
            with self.lock:
                texts.append(self._recognize(clip, language, terms))
        return texts

    @torch.inference_mode()
    def _recognize(self, clip: np.ndarray, language: str | None, terms: tuple[str, ...]) -> str:
        kwargs = {"prompt": ", ".join(terms)} if terms else {}
        inputs = self.processor.apply_transcription_request(audio=clip, language=language, **kwargs).to(
            self.device, self.dtype
        )
        output_ids = self.model.generate(**inputs, max_new_tokens=QWEN_MAX_NEW_TOKENS, do_sample=False)
        generated_ids = output_ids[0, inputs["input_ids"].shape[1] :]
        eos_ids = self.model.generation_config.eos_token_id
        if isinstance(eos_ids, int):
            eos_ids = [eos_ids]
        if len(generated_ids) >= QWEN_MAX_NEW_TOKENS and int(generated_ids[-1]) not in (eos_ids or []):
            raise RuntimeError("Qwen3-ASR transcription truncated at the generation token limit")
        return self.processor.decode(generated_ids, return_format="transcription_only").strip()
