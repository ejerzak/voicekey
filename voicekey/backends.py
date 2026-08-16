"""Transcription backends. One interface — transcribe(wav_path) -> str —
selected by [backend].type. Backends load once at daemon start and stay warm."""

from __future__ import annotations

import glob
import logging
import os

from .config import BackendConfig

log = logging.getLogger("voicekey.backends")


class BackendUnavailable(Exception):
    """Backend can't run on this machine. Message must be human-actionable —
    it goes straight into a notification."""


# --- faster-whisper (desktop, CUDA) -----------------------------------------

def _preload_cuda_libs() -> None:
    """ctranslate2 needs cuBLAS/cuDNN at import time. The pip nvidia-* wheels
    put them in site-packages/nvidia/*/lib, which isn't on the loader path;
    dlopen them RTLD_GLOBAL before importing so a plain venv works without a
    system CUDA install."""
    import ctypes
    import site

    dirs = []
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        dirs += glob.glob(os.path.join(sp, "nvidia", "*", "lib"))
    for d in dirs:
        for lib in sorted(glob.glob(os.path.join(d, "lib*.so.*"))):
            base = os.path.basename(lib)
            if not ("cublas" in base or "cudnn" in base):
                continue
            try:
                ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError as e:
                log.debug("preload skipped %s: %s", base, e)


class FasterWhisperBackend:
    def __init__(self, cfg: BackendConfig, language: str) -> None:
        _preload_cuda_libs()
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError:
            raise BackendUnavailable(
                "faster-whisper not installed in the voicekey venv — "
                "run install step 07-voicekey (backend.type = 'faster-whisper')"
            )
        self.language = language or None
        log.info("loading faster-whisper %s (device=%s, compute=%s)...",
                 cfg.model, cfg.device, cfg.compute_type)
        try:
            model_path = download_model(cfg.model, local_files_only=True)
        except Exception as e:
            raise BackendUnavailable(
                "faster-whisper model is unavailable in the local cache: "
                f"{e} — rerun install step 07-voicekey to repair the cache"
            )
        try:
            self.model = WhisperModel(model_path, device=cfg.device,
                                      compute_type=cfg.compute_type)
        except Exception as e:
            raise BackendUnavailable(
                f"faster-whisper model failed to initialize: {e}"
            )
        log.info("model loaded")

    def transcribe(self, wav_path: str) -> str:
        # vad_filter drops silence so an empty recording yields an empty
        # transcript instead of a hallucinated "Thank you."
        segments, _info = self.model.transcribe(
            wav_path, language=self.language, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()


# --- parakeet (laptop, CPU via sherpa-onnx) ----------------------------------
# UNTESTED until the laptop arrives — the config seam and graceful-failure
# paths are what land now.

class ParakeetBackend:
    def __init__(self, cfg: BackendConfig, language: str) -> None:
        try:
            import sherpa_onnx
        except ImportError:
            raise BackendUnavailable(
                "sherpa-onnx not installed in the voicekey venv — "
                "run install step 07-voicekey (backend.type = 'parakeet')"
            )
        model_dir = os.path.expanduser(cfg.model_dir)
        if not model_dir or not os.path.isdir(model_dir):
            raise BackendUnavailable(
                f"parakeet model_dir not found: {model_dir or '(unset)'} — download a "
                "sherpa-onnx Parakeet model (e.g. sherpa-onnx-nemo-parakeet-tdt-0.6b-v3) "
                "and set [backend] model_dir in config.toml"
            )

        def one(pattern: str) -> str:
            matches = glob.glob(os.path.join(model_dir, pattern))
            if not matches:
                raise BackendUnavailable(f"parakeet: no {pattern} in {model_dir}")
            return sorted(matches)[0]

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=one("encoder*.onnx"),
            decoder=one("decoder*.onnx"),
            joiner=one("joiner*.onnx"),
            tokens=one("tokens.txt"),
            num_threads=4,
            model_type="nemo_transducer",
        )

    def transcribe(self, wav_path: str) -> str:
        import wave

        import numpy as np
        with wave.open(wav_path) as f:
            samples = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
            rate = f.getframerate()
        stream = self.recognizer.create_stream()
        stream.accept_waveform(rate, samples.astype(np.float32) / 32768.0)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()


# --- remote (stub in v1) -----------------------------------------------------

class RemoteBackend:
    def __init__(self, cfg: BackendConfig, language: str) -> None:
        raise BackendUnavailable(
            "remote backend is a stub in v1 — config shape is "
            "[backend] type = 'remote', url = 'http://desktop:9188/transcribe'"
        )

    def transcribe(self, wav_path: str) -> str:
        raise NotImplementedError


REGISTRY = {
    "faster-whisper": FasterWhisperBackend,
    "parakeet": ParakeetBackend,
    "remote": RemoteBackend,
}


def create_backend(cfg: BackendConfig, language: str):
    try:
        factory = REGISTRY[cfg.type]
    except KeyError:
        raise BackendUnavailable(
            f"unknown backend.type {cfg.type!r} — one of: {', '.join(REGISTRY)}")
    return factory(cfg, language)


def predownload(cfg: BackendConfig) -> None:
    """Fetch model weights without loading them (install step, not first keypress)."""
    if cfg.type == "faster-whisper":
        from faster_whisper.utils import download_model
        print(f"downloading faster-whisper model {cfg.model} ...")
        path = download_model(cfg.model)
        print(f"model ready at {path}")
    elif cfg.type == "parakeet":
        print("parakeet: download a model archive from "
              "https://github.com/k2-fsa/sherpa-onnx/releases (e.g. "
              "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8), unpack it, and set "
              "[backend] model_dir in config.toml")
    else:
        print(f"backend {cfg.type}: nothing to download")
