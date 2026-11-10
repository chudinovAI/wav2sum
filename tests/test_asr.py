import threading
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from transformers.models.qwen3_asr.processing_qwen3_asr import Qwen3ASRProcessor

from wav2sum import asr


class FakeQwenInputs(dict):
    def to(self, device, dtype):
        self.device = device
        self.dtype = dtype
        return self


class FakeQwenProcessor:
    # Exercise the actual Transformers output parser with a tiny deterministic tokenizer.
    decode = Qwen3ASRProcessor.decode
    extract_transcription = Qwen3ASRProcessor.extract_transcription

    def __init__(self):
        self.requests = []
        self.tokenizer = SimpleNamespace(decode=self._decode)

    def apply_transcription_request(self, audio, language, **kwargs):
        self.requests.append((audio.copy(), language, kwargs))
        return FakeQwenInputs(input_ids=torch.tensor([[90, 91, 92]], dtype=torch.long))

    @staticmethod
    def _decode(tokens, skip_special_tokens):
        assert skip_special_tokens
        vocabulary = {
            1: "language Russian",
            2: "<asr_text>",
            3: " Привет ",
            4: " Hello ",
            7: "",
            8: "",
            90: "PROMPT",
            91: " AUDIO",
            92: " CONTENT",
        }
        return "".join(vocabulary[int(token)] for token in tokens)


class FakeQwenModel:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.generation_config = SimpleNamespace(eos_token_id=[7, 8])
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        suffix = torch.tensor([next(self.outputs)], dtype=torch.long)
        return torch.cat((kwargs["input_ids"], suffix), dim=1)


def test_qwen_initialization_uses_only_prepared_files(monkeypatch):
    calls = []

    class Model:
        def to(self, device):
            return self

        def eval(self):
            return self

    def processor(*args, **kwargs):
        calls.append(kwargs)
        return object()

    def model(*args, **kwargs):
        calls.append(kwargs)
        return Model()

    monkeypatch.setattr(asr.AutoProcessor, "from_pretrained", processor)
    monkeypatch.setattr(asr.Qwen3ASRForConditionalGeneration, "from_pretrained", model)
    asr.QwenTranscriber("cpu")
    assert all(call["local_files_only"] is True for call in calls)


def qwen_transcriber(outputs):
    transcriber = asr.QwenTranscriber.__new__(asr.QwenTranscriber)
    transcriber.device = "cpu"
    transcriber.dtype = torch.float32
    transcriber.lock = threading.Lock()
    transcriber.processor = FakeQwenProcessor()
    transcriber.model = FakeQwenModel(outputs)
    return transcriber


def test_qwen_transcription_decodes_only_generated_tokens_and_strips_language_tags():
    transcriber = qwen_transcriber([[1, 2, 3, 7], [4, 8], [4, 7]])
    audio = np.arange(3 * asr.SAMPLE_RATE, dtype=np.float32)

    assert transcriber.transcribe_spans(audio, [(2, 3), (0, 1), (1, 1), (1, 2)]) == [
        "Привет",
        "Hello",
        "",
        "Hello",
    ]
    requests = transcriber.processor.requests
    np.testing.assert_array_equal(requests[0][0], audio[2 * asr.SAMPLE_RATE :])
    np.testing.assert_array_equal(requests[1][0], audio[: asr.SAMPLE_RATE])
    np.testing.assert_array_equal(requests[2][0], audio[asr.SAMPLE_RATE : 2 * asr.SAMPLE_RATE])
    assert [request[1] for request in requests] == [None, None, None]
    assert all(call["do_sample"] is False for call in transcriber.model.calls)


@pytest.mark.parametrize("language", ["ru", "en"])
def test_qwen_forces_requested_language_and_excludes_prefilled_prompt(language):
    transcriber = qwen_transcriber([[4, 7]])
    assert transcriber.transcribe(np.ones(10, dtype=np.float32), language=language) == "Hello"
    assert transcriber.processor.requests[0][1] == language


def test_qwen_empty_audio_and_empty_spans_do_not_generate():
    transcriber = qwen_transcriber([])
    assert transcriber.transcribe(np.array([], dtype=np.float32)) == ""
    assert transcriber.transcribe_spans(np.ones(10, dtype=np.float32), []) == []
    assert transcriber.transcribe_spans(np.ones(10, dtype=np.float32), [(0, 0), (1, 2)]) == ["", ""]
    assert transcriber.processor.requests == []
    assert transcriber.model.calls == []


def test_qwen_raises_instead_of_returning_a_truncated_transcript():
    transcriber = qwen_transcriber([[4] * asr.QWEN_MAX_NEW_TOKENS])
    with pytest.raises(RuntimeError, match=r"(?i)(truncat|token limit)"):
        transcriber.transcribe(np.ones(10, dtype=np.float32))


def test_qwen_accepts_eos_on_last_allowed_token(monkeypatch):
    monkeypatch.setattr(asr, "QWEN_MAX_NEW_TOKENS", 2)
    transcriber = qwen_transcriber([[4, 8]])
    assert transcriber.transcribe(np.ones(10, dtype=np.float32)) == "Hello"


def test_qwen_context_uses_processor_prompt_without_leaking_into_generated_text():
    transcriber = qwen_transcriber([[1, 2, 3, 7], [4, 7]])
    audio = np.ones(10, dtype=np.float32)
    assert transcriber.transcribe(audio, terms=("GitHub", "Qwen")) == "Привет"
    assert transcriber.processor.requests[0][2]["prompt"] == "GitHub, Qwen"
    assert transcriber.transcribe(audio) == "Hello"
    assert "prompt" not in transcriber.processor.requests[1][2]
