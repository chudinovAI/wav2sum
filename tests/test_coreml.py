import pytest
import torch

from wav2sum import coreml


class Subsampling(torch.nn.Module):
    def forward(self, x, lengths):
        return x[:, ::4], torch.div(lengths + 3, 4, rounding_mode="floor")


class Positions(torch.nn.Module):
    def forward(self, x):
        return x, torch.arange(x.size(1)).float()


class Layer(torch.nn.Module):
    """Attention that leaks padding unless `att_mask` hides it, as in GigaAM's Conformer layers."""

    def forward(self, x, pos_emb, att_mask, pad_mask):
        scores = x @ x.transpose(1, 2)
        if att_mask is not None:
            scores = scores.masked_fill(att_mask, -1e4)
        return (scores.softmax(-1) @ x + pos_emb[None, :, None]).masked_fill(pad_mask.unsqueeze(-1), 0.0)


class Encoder(torch.nn.Module):
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


def test_pad_takes_the_smallest_shape_that_fits_and_fills_with_zeros():
    features = torch.ones(1, 2, 50)

    padded = coreml.pad(features, [40, 80, 160])

    assert padded.shape == (1, 2, 80)
    assert padded[..., 50:].abs().sum() == 0
    with pytest.raises(ValueError, match="longer than 160 frames"):
        coreml.pad(torch.ones(1, 2, 161), [40, 80, 160])
