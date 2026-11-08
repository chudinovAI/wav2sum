import copy
import logging
import threading
import warnings

import numpy as np
import torch
from huggingface_hub import hf_hub_download
from transformers import AutoConfig, AutoModel, AutoProcessor, Qwen3ASRForConditionalGeneration

from wav2sum.audio import SAMPLE_RATE

logger = logging.getLogger(__name__)

MODEL_ID = "ai-sage/GigaAM-v3"
REVISION = "e2e_rnnt"
QWEN_MODEL_ID = "Qwen/Qwen3-ASR-1.7B-hf"
QWEN_REVISION = "bcd2b5b7f32b480ab5790554cfa8347f246a14f3"
QWEN_MAX_NEW_TOKENS = 1024

MAX_SPAN_SEC = 20.0

MAX_BATCH_SECONDS = 160.0
MAX_BATCH_ITEMS = 16


class Transcriber:
    def __init__(self, device: str, lock: "threading.Lock | None" = None):
        self.device = device
        self.lock = lock or threading.Lock()
        logger.info("Loading GigaAM on %s …", device)
        # transformers 5 builds a from_pretrained model on the meta device, and GigaAM's remote code makes real tensors
        # in __init__; building from the config and loading the weights ourselves keeps it on the CPU.
        config = AutoConfig.from_pretrained(MODEL_ID, revision=REVISION, trust_remote_code=True)
        model = AutoModel.from_config(config, trust_remote_code=True)
        weights = hf_hub_download(MODEL_ID, "pytorch_model.bin", revision=REVISION)
        model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
        self.model = model.model
        if device != "cpu":
            # GigaAM already runs the encoder under fp16 autocast off the CPU; fp16 weights just save ~0.6 GB.
            self.model.encoder.half()
        self.model.to(device).eval()
        self.cpu_head = copy.deepcopy(self.model.head).cpu().float()

    def transcribe(self, audio: np.ndarray) -> str:
        return self.transcribe_spans(audio, [(0.0, len(audio) / SAMPLE_RATE)])[0]

    def transcribe_spans(self, audio: np.ndarray, spans: list[tuple[float, float]]) -> list[str]:
        texts = [""] * len(spans)
        for batch in _batches(spans):
            clips = [audio[int(spans[i][0] * SAMPLE_RATE) : int(spans[i][1] * SAMPLE_RATE)] for i in batch]
            with self.lock:
                for i, text in zip(batch, self._recognize(clips), strict=True):
                    texts[i] = text.strip()
        return texts

    @torch.inference_mode()
    def _recognize(self, clips: list[np.ndarray]) -> list[str]:
        lengths = torch.tensor([len(c) for c in clips])
        wav = torch.zeros(len(clips), int(lengths.max()))
        for row, clip in enumerate(clips):
            wav[row, : len(clip)] = torch.from_numpy(clip)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message=r".*resized since it had shape.*")
            encoded, enc_len = self.model.forward(wav.to(self.device), lengths.to(self.device))
        return self.model.decoding.decode(self.cpu_head, encoded.float().cpu(), enc_len.cpu())


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


def _batches(spans: list[tuple[float, float]]):
    order = sorted(range(len(spans)), key=lambda i: spans[i][0] - spans[i][1])
    batch: list[int] = []
    seconds = 0.0
    for i in order:
        duration = spans[i][1] - spans[i][0]
        if batch and (seconds + duration > MAX_BATCH_SECONDS or len(batch) >= MAX_BATCH_ITEMS):
            yield batch
            batch, seconds = [], 0.0
        batch.append(i)
        seconds += duration
    if batch:
        yield batch
