import pytest
import torch

from wav2sum import coreml


class Subsampling(torch.nn.Module):
    def forward(self, x, lengths):
        return x[:, ::4], torch.div(lengths + 3, 4, rounding_mode="floor")


class Positions(torch.nn.Module):
    def extend_pe(self, length, device):
        self.pe = torch.arange(2 * length, dtype=torch.float32, device=device)


class Attention(torch.nn.Module):
    torch_sdpa_attn = True


class Layer(torch.nn.Module):
    """Attention that leaks padding unless `att_mask` hides it, as in GigaAM's Conformer layers."""

    def __init__(self):
        super().__init__()
        self.self_attn = Attention()

    def forward(self, x, pos_emb, att_mask, pad_mask):
        cos, sin = pos_emb
        scores = x @ x.transpose(1, 2)
        if att_mask is not None:
            scores = scores.masked_fill(att_mask, -1e4)
        x = scores.softmax(-1) @ x + cos[None, :, None] - sin[None, :, None]
        return x.masked_fill(pad_mask.unsqueeze(-1), 0.0)


class Encoder(torch.nn.Module):
    pos_emb_max_len = 50

    def __init__(self):
        super().__init__()
        self.pre_encode = Subsampling()
        self.pos_enc = Positions()
        self.layers = torch.nn.ModuleList([Layer(), Layer()])


def test_padding_to_a_fixed_shape_does_not_change_the_frames_of_the_clip():
    encoder = coreml.MaskedEncoder(Encoder()).eval()
    features = torch.randn(1, 4, 21)

    encoded, length = encoder(features, torch.tensor([21]))
    padded, padded_length = encoder(coreml.pad(features, [40, 80]), torch.tensor([21]))

    assert padded.shape == (1, 4, 10)
    assert length.tolist() == padded_length.tolist() == [6]
    torch.testing.assert_close(padded[..., :6], encoded)


def test_attention_skips_sdpa_which_leaks_padding_on_the_neural_engine():
    encoder = coreml.MaskedEncoder(Encoder())

    assert [layer.self_attn.torch_sdpa_attn for layer in encoder.encoder.layers] == [False, False]


def test_pad_takes_the_smallest_shape_that_fits_and_fills_with_zeros():
    features = torch.ones(1, 2, 50)

    padded = coreml.pad(features, [40, 80, 160])

    assert padded.shape == (1, 2, 80)
    assert padded[..., 50:].abs().sum() == 0
    with pytest.raises(ValueError, match="longer than 160 frames"):
        coreml.pad(torch.ones(1, 2, 161), [40, 80, 160])
