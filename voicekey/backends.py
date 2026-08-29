"""Speech recognition backends.

Final pass: ``transcribe(samples) -> str`` from ``[backend]`` — Parakeet through
sherpa-onnx on the CPU, or faster-whisper on CUDA. Live preview: a cache-aware
Nemotron transducer through sherpa-onnx from ``[streaming]``, whose partial text
only ever grows. Models load once at daemon start and stay warm. Audio is
float32 mono at 16 kHz throughout."""

from __future__ import annotations

import ctypes
import glob
import logging
import os
import shutil
import site
import tarfile
import tempfile
import urllib.request
from pathlib import Path

import numpy as np

from .config import BackendConfig, StreamingConfig

log = logging.getLogger("voicekey.backends")

SAMPLE_RATE = 16000
NUM_THREADS = 4  # 2-4 is the sweet spot on a desktop CPU; more is slower
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
# sherpa-onnx model archives. Each unpacks to <name>/ holding MODEL_FILES.
MODELS = {
    name: f"{RELEASES}{name}.tar.bz2"
    for name in (
        "sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming",
        "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
        "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25",
    )
}
MODEL_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")


class BackendUnavailable(Exception):
    """The backend can't run here. The message goes straight into a
    notification, so it says what to do."""


def _sherpa():
    try:
        import sherpa_onnx
    except ImportError:
        raise BackendUnavailable(
            "sherpa-onnx is not installed in the voicekey venv — run install.sh"
        )
    return sherpa_onnx


def _model_files(model_dir: str) -> dict[str, str]:
    """Paths of a sherpa-onnx transducer model, keyed for from_transducer()."""
    model_dir = os.path.expanduser(model_dir)
    if not model_dir:
        raise BackendUnavailable("model_dir is empty")
    missing = [
        name for name in MODEL_FILES
        if not os.path.isfile(os.path.join(model_dir, name))
    ]
    if missing:
        raise BackendUnavailable(
            f"{model_dir} is missing {', '.join(missing)} — "
            "run install.sh to download the model"
        )
    return {name.split(".")[0]: os.path.join(model_dir, name) for name in MODEL_FILES}


# --- final pass -------------------------------------------------------------

class ParakeetBackend:
    def __init__(self, cfg: BackendConfig, language: str) -> None:
        self.recognizer = _sherpa().OfflineRecognizer.from_transducer(
            num_threads=NUM_THREADS,
            model_type="nemo_transducer",
            **_model_files(cfg.model_dir),
        )

    def transcribe(self, samples: np.ndarray) -> str:
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text.strip()


def _preload_cuda_libs() -> None:
    """ctranslate2 needs cuBLAS/cuDNN at import time. The pip nvidia-* wheels
    put them in site-packages/nvidia/*/lib, which isn't on the loader path;
    dlopen them RTLD_GLOBAL first so a plain venv works without system CUDA."""
    for prefix in site.getsitepackages() + [site.getusersitepackages()]:
        for lib in sorted(glob.glob(os.path.join(prefix, "nvidia", "*", "lib", "lib*.so.*"))):
            base = os.path.basename(lib)
            if "cublas" in base or "cudnn" in base:
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError as exc:
                    log.debug("preload skipped %s: %s", base, exc)


class FasterWhisperBackend:
    def __init__(self, cfg: BackendConfig, language: str) -> None:
        _preload_cuda_libs()
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
        except ImportError:
            raise BackendUnavailable(
                "faster-whisper is not installed in the voicekey venv — "
                "run install.sh with backend.type = 'faster-whisper'"
            )
        self.language = language or None
        log.info("loading faster-whisper %s (device=%s, compute=%s)...",
                 cfg.model, cfg.device, cfg.compute_type)
        try:
            model_path = download_model(cfg.model, local_files_only=True)
        except Exception as exc:
            raise BackendUnavailable(
                f"faster-whisper model is not in the local cache: {exc} — "
                "run install.sh to download it"
            )
        try:
            self.model = WhisperModel(model_path, device=cfg.device,
                                      compute_type=cfg.compute_type)
        except Exception as exc:
            raise BackendUnavailable(f"faster-whisper failed to initialize: {exc}")
        log.info("model loaded")

    def transcribe(self, samples: np.ndarray) -> str:
        # vad_filter drops silence so an empty recording yields an empty
        # transcript instead of a hallucinated "Thank you."
        segments, _info = self.model.transcribe(
            samples, language=self.language, beam_size=1, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()


# --- live preview -----------------------------------------------------------

class StreamingBackend:
    """Cache-aware streaming transducer; one StreamSession per utterance."""

    def __init__(self, cfg: StreamingConfig) -> None:
        self.recognizer = _sherpa().OnlineRecognizer.from_transducer(
            num_threads=NUM_THREADS,
            model_type="nemotron",
            **_model_files(cfg.model_dir),
        )

    def session(self) -> StreamSession:
        return StreamSession(self.recognizer)


class StreamSession:
    """Feed audio as it arrives; the returned text only ever grows (greedy
    decoding never retracts), so it is safe to show and append to live."""

    def __init__(self, recognizer) -> None:
        self.recognizer = recognizer
        self.stream = recognizer.create_stream()

    def feed(self, samples: np.ndarray) -> str:
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        return self._decode()

    def finish(self) -> str:
        self.stream.input_finished()
        return self._decode()

    def _decode(self) -> str:
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        return self.recognizer.get_result(self.stream).strip()


# --- construction and model download ----------------------------------------

def create_backend(cfg: BackendConfig, language: str):
    factory = {"faster-whisper": FasterWhisperBackend, "parakeet": ParakeetBackend}
    return factory[cfg.type](cfg, language)


def create_streaming(cfg: StreamingConfig) -> StreamingBackend | None:
    return StreamingBackend(cfg) if cfg.model_dir else None


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
                if total and received * 100 // total >= next_report:
                    print(f"  {min(received * 100 // total, 100)}%", flush=True)
                    next_report = (received * 100 // total // 10 + 1) * 10


def _extract(archive_path: Path, destination: Path) -> None:
    """Extract a model archive after rejecting links and path traversal."""
    root = destination.resolve()
    # Stream the bzip2 archive: random-access mode would decompress the large
    # encoder once to enumerate members and again to extract them.
    with tarfile.open(archive_path, "r|bz2") as archive:
        for member in archive:
            if not (destination / member.name).resolve().is_relative_to(root):
                raise BackendUnavailable(
                    f"model archive contains an unsafe path: {member.name}"
                )
            if not (member.isfile() or member.isdir()):
                raise BackendUnavailable(
                    f"model archive contains an unsupported entry: {member.name}"
                )
            # Traversal, links and special entries are rejected above, so the
            # ordinary mode bits of the model files are safe to keep.
            archive.extract(member, destination, filter="fully_trusted")


def ensure_model(model_dir: str) -> None:
    """Download and unpack a known sherpa-onnx model unless it is complete."""
    model_dir = Path(os.path.expanduser(model_dir))
    if all((model_dir / name).is_file() for name in MODEL_FILES):
        print(f"model ready at {model_dir}")
        return
    if model_dir.exists():
        raise BackendUnavailable(
            f"{model_dir} exists but is incomplete — remove it and retry"
        )
    url = MODELS.get(model_dir.name)
    if url is None:
        raise BackendUnavailable(
            f"no download is known for model {model_dir.name!r}; "
            f"known models: {', '.join(MODELS)}"
        )
    model_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{model_dir.name}-", dir=model_dir.parent
    ) as temporary:
        staging = Path(temporary)
        archive_path = staging / f"{model_dir.name}.tar.bz2"
        extracted = staging / "extracted"
        extracted.mkdir()
        _download(url, archive_path)
        print("extracting model ...", flush=True)
        _extract(archive_path, extracted)
        candidate = extracted / model_dir.name
        missing = [name for name in MODEL_FILES if not (candidate / name).is_file()]
        if missing:
            raise BackendUnavailable(
                f"downloaded archive is missing: {', '.join(missing)}"
            )
        shutil.move(str(candidate), str(model_dir))
    print(f"model ready at {model_dir}")


def predownload(backend: BackendConfig, streaming: StreamingConfig) -> None:
    """Fetch model weights without loading them (install step, not first keypress)."""
    if backend.type == "faster-whisper":
        from faster_whisper.utils import download_model
        print(f"downloading faster-whisper model {backend.model} ...")
        print(f"model ready at {download_model(backend.model)}")
    else:
        ensure_model(backend.model_dir)
    if streaming.model_dir:
        ensure_model(streaming.model_dir)
