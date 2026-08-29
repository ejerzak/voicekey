from __future__ import annotations

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from voicekey.backends import (
    MODEL_FILES,
    MODELS,
    BackendUnavailable,
    FasterWhisperBackend,
    StreamSession,
    _model_files,
    ensure_model,
)
from voicekey.config import BackendConfig


class FakeStream:
    def __init__(self):
        self.pending = 0
        self.text = ""
        self.finished = False

    def accept_waveform(self, rate, samples):
        self.pending += 1

    def input_finished(self):
        self.finished = True
        self.pending += 1


class FakeRecognizer:
    """Emits one word per decode; is_ready while waveform is pending."""

    def create_stream(self):
        return FakeStream()

    def is_ready(self, stream):
        return stream.pending > 0

    def decode_stream(self, stream):
        stream.pending -= 1
        stream.text += " word"

    def get_result(self, stream):
        return stream.text


class StreamSessionTests(unittest.TestCase):
    def test_text_grows_with_each_frame_and_finish_flushes(self):
        session = StreamSession(FakeRecognizer())
        frame = np.zeros(1600, dtype=np.float32)
        self.assertEqual(session.feed(frame), "word")
        self.assertEqual(session.feed(frame), "word word")
        self.assertEqual(session.finish(), "word word word")
        self.assertTrue(session.stream.finished)


class ModelFilesTests(unittest.TestCase):
    def test_missing_files_are_named_with_a_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "tokens.txt").touch()
            with self.assertRaisesRegex(BackendUnavailable, "encoder.int8.onnx.*install.sh"):
                _model_files(directory)

    def test_complete_model_maps_to_keyword_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in MODEL_FILES:
                Path(directory, name).touch()
            files = _model_files(directory)
        self.assertEqual(set(files), {"encoder", "decoder", "joiner", "tokens"})
        self.assertTrue(files["tokens"].endswith("/tokens.txt"))


class FasterWhisperBackendTests(unittest.TestCase):
    def test_runtime_load_is_cache_only(self):
        whisper_model = Mock()
        module = types.ModuleType("faster_whisper")
        module.__path__ = []
        module.WhisperModel = whisper_model
        download_model = Mock(return_value="/cached/model")
        utils = types.ModuleType("faster_whisper.utils")
        utils.download_model = download_model

        with patch("voicekey.backends._preload_cuda_libs"):
            with patch.dict(sys.modules, {
                "faster_whisper": module,
                "faster_whisper.utils": utils,
            }):
                FasterWhisperBackend(BackendConfig(type="faster-whisper"), "en")

        download_model.assert_called_once_with("large-v3-turbo", local_files_only=True)
        whisper_model.assert_called_once_with(
            "/cached/model", device="auto", compute_type="default"
        )


class ModelDownloadTests(unittest.TestCase):
    @staticmethod
    def _archive(model_name: str) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:bz2") as archive:
            for name in MODEL_FILES:
                content = name.encode()
                info = tarfile.TarInfo(f"{model_name}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    MODEL = "sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25"
    FAKE_DIGESTS = {MODEL: {name: hashlib.sha256(name.encode()).hexdigest() for name in MODEL_FILES}}

    def test_downloads_verifies_extracts_and_then_skips_complete_model(self):
        self.assertIn(self.MODEL, MODELS)
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory) / self.MODEL
            response = io.BytesIO(self._archive(self.MODEL))
            response.headers = {"Content-Length": str(len(response.getvalue()))}
            with patch("voicekey.backends.urllib.request.urlopen", return_value=response), \
                    patch("voicekey.backends.MODELS", self.FAKE_DIGESTS):
                ensure_model(str(model_dir))
            for name in MODEL_FILES:
                self.assertTrue((model_dir / name).is_file())
            with patch(
                "voicekey.backends.urllib.request.urlopen",
                side_effect=AssertionError("complete model should not be downloaded"),
            ):
                ensure_model(str(model_dir))

    def test_checksum_mismatch_rejects_the_download(self):
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory) / self.MODEL
            response = io.BytesIO(self._archive(self.MODEL))
            response.headers = {}
            with patch("voicekey.backends.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(BackendUnavailable, "checksum mismatch"):
                    ensure_model(str(model_dir))  # real digests, fake content
            self.assertFalse(model_dir.exists())

    def test_unknown_model_is_rejected_before_any_download(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BackendUnavailable, "no download is known"):
                ensure_model(os.path.join(directory, "some-other-model"))


if __name__ == "__main__":
    unittest.main()
