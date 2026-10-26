import torch

from wav2sum import asr


class GigaAM(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.model = torch.nn.Module()
        self.model.encoder = torch.nn.Linear(2, 2)
        self.model.head = torch.nn.Linear(2, 2)


def test_gigaam_is_built_from_its_config_and_gets_the_downloaded_weights(tmp_path, monkeypatch):
    saved = GigaAM()
    torch.save(saved.state_dict(), tmp_path / "pytorch_model.bin")
    downloads = []

    class AutoModel:  # no from_pretrained: under transformers 5 it builds the model on the meta device
        @staticmethod
        def from_config(config, trust_remote_code):
            assert (config, trust_remote_code) == ("gigaam config", True)
            return GigaAM()

    monkeypatch.setattr(asr.AutoConfig, "from_pretrained", lambda *args, **kwargs: "gigaam config")
    monkeypatch.setattr(asr, "AutoModel", AutoModel)
    monkeypatch.setattr(
        asr,
        "hf_hub_download",
        lambda *args, **kwargs: downloads.append((args, kwargs)) or tmp_path / "pytorch_model.bin",
    )

    transcriber = asr.Transcriber("cpu")

    assert downloads == [((asr.MODEL_ID, "pytorch_model.bin"), {"revision": asr.REVISION})]
    assert torch.equal(transcriber.model.encoder.weight, saved.model.encoder.weight)
    assert torch.equal(transcriber.cpu_head.weight, saved.model.head.weight)
