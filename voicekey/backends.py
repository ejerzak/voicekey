"""Transcription backends. One interface — transcribe(wav_path) -> str —
selected by [backend].type. Backends load once at daemon start and stay warm."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from .config import BackendConfig

log = logging.getLogger("voicekey.backends")

PARAKEET_MODELS = {
    "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming.tar.bz2"
    ),
    "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2"
    ),
}
PARAKEET_REQUIRED_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "joiner.int8.onnx",
    "tokens.txt",
)


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


# --- parakeet (CPU via sherpa-onnx) -----------------------------------------

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
                "sherpa-onnx Parakeet model "
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


def _parakeet_ready(model_dir: Path) -> bool:
    return model_dir.is_dir() and all(
        (model_dir / name).is_file() for name in PARAKEET_REQUIRED_FILES
    )


def _download(url: str, destination: Path) -> None:
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=30) as response:
        try:
            total = int(response.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            total = 0
        received = 0
        next_report = 10
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                if total:
                    percent = received * 100 // total
                    if percent >= next_report:
                        print(f"  {min(percent, 100)}%", flush=True)
                        next_report = (percent // 10 + 1) * 10


def _extract_model(archive_path: Path, destination: Path) -> None:
    """Extract a model archive after rejecting links and path traversal."""
    root = destination.resolve()
    # Stream the bzip2 archive: opening it in random-access mode would
    # decompress the large encoder once to enumerate members and again to
    # extract them.
    with tarfile.open(archive_path, "r|bz2") as archive:
        for member in archive:
            member_path = (destination / member.name).resolve()
            if not member_path.is_relative_to(root):
                raise BackendUnavailable(
                    f"parakeet archive contains an unsafe path: {member.name}"
                )
            if not (member.isfile() or member.isdir()):
                raise BackendUnavailable(
                    f"parakeet archive contains an unsupported entry: {member.name}"
                )
            # We have rejected traversal, links, devices, and other special
            # entries, so retaining the model files' ordinary mode bits is safe.
            archive.extract(member, destination, filter="fully_trusted")


def _predownload_parakeet(cfg: BackendConfig) -> None:
    model_dir = Path(os.path.expanduser(cfg.model_dir))
    if not cfg.model_dir:
        raise BackendUnavailable("parakeet backend.model_dir is empty")
    if _parakeet_ready(model_dir):
        print(f"model ready at {model_dir}")
        return
    if model_dir.exists():
        raise BackendUnavailable(
            f"parakeet model directory exists but is incomplete: {model_dir}"
        )

    model_name = model_dir.name
    try:
        url = PARAKEET_MODELS[model_name]
    except KeyError:
        raise BackendUnavailable(
            f"no automatic download is defined for parakeet model {model_name!r}"
        )

    model_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{model_name}-", dir=model_dir.parent
    ) as temporary:
        staging = Path(temporary)
        archive_path = staging / f"{model_name}.tar.bz2"
        extracted = staging / "extracted"
        extracted.mkdir()
        _download(url, archive_path)
        print("extracting model ...", flush=True)
        _extract_model(archive_path, extracted)
        candidate = extracted / model_name
        if not _parakeet_ready(candidate):
            missing = [
                name for name in PARAKEET_REQUIRED_FILES
                if not (candidate / name).is_file()
            ]
            raise BackendUnavailable(
                f"downloaded parakeet archive is missing: {', '.join(missing)}"
            )
        shutil.move(str(candidate), str(model_dir))
    print(f"model ready at {model_dir}")


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
        _predownload_parakeet(cfg)
    else:
        print(f"backend {cfg.type}: nothing to download")
