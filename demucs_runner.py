from __future__ import annotations

import sys


def patch_demucs_padding() -> None:
    import torch
    import torch.nn.functional as F
    import demucs.hdemucs as hdemucs_module
    import demucs.htdemucs as htdemucs_module

    def safe_pad1d(
        x: torch.Tensor,
        paddings: tuple[int, int],
        mode: str = "constant",
        value: float = 0.0,
    ) -> torch.Tensor:
        length = x.shape[-1]
        padding_left, padding_right = paddings
        if mode == "reflect":
            max_pad = max(padding_left, padding_right)
            if length <= max_pad:
                extra_pad = max_pad - length + 1
                extra_pad_right = min(padding_right, extra_pad)
                extra_pad_left = extra_pad - extra_pad_right
                paddings = (padding_left - extra_pad_left, padding_right - extra_pad_right)
                x = F.pad(x, (extra_pad_left, extra_pad_right))
        out = F.pad(x, paddings, mode, value)
        if out.shape[-1] != length + padding_left + padding_right:
            raise RuntimeError("Demucs pad1d produced an unexpected output length")
        return out

    hdemucs_module.pad1d = safe_pad1d
    htdemucs_module.pad1d = safe_pad1d


def patch_demucs_audio_save() -> None:
    from pathlib import Path

    import numpy as np
    import soundfile as sf
    import demucs.audio as audio_module
    import demucs.separate as separate_module

    original_prevent_clip = audio_module.prevent_clip

    def save_audio_compat(
        wav,
        path,
        samplerate,
        bitrate=320,
        clip="rescale",
        bits_per_sample=16,
        as_float=False,
        preset=2,
    ):
        wav = original_prevent_clip(wav, mode=clip)
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix != ".wav":
            raise ValueError(f"Compatibility saver only supports wav output, got: {suffix}")

        if hasattr(wav, "detach"):
            wav = wav.detach().cpu().numpy()
        wav = np.asarray(wav)
        if wav.ndim == 2:
            wav = wav.transpose(1, 0)

        subtype = "FLOAT" if as_float else "PCM_16"
        sf.write(str(target), wav, samplerate, subtype=subtype)

    audio_module.save_audio = save_audio_compat
    separate_module.save_audio = save_audio_compat


def main() -> None:
    patch_demucs_padding()
    patch_demucs_audio_save()
    from demucs.separate import main as demucs_main

    demucs_main()


if __name__ == "__main__":
    main()
