from __future__ import annotations

import io
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from voicekey.backends import (
    PARAKEET_MODELS,
    PARAKEET_REQUIRED_FILES,
    FasterWhisperBackend,
    predownload,
)
from voicekey.config import BackendConfig


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
                FasterWhisperBackend(BackendConfig(), "en")

        download_model.assert_called_once_with(
            "large-v3-turbo", local_files_only=True
        )
        whisper_model.assert_called_once_with(
            "/cached/model",
            device="auto",
            compute_type="default",
        )


class ParakeetDownloadTests(unittest.TestCase):
    @staticmethod
    def _archive(model_name: str) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:bz2") as archive:
            for name in PARAKEET_REQUIRED_FILES:
                content = name.encode()
                info = tarfile.TarInfo(f"{model_name}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return output.getvalue()

    def test_downloads_extracts_and_then_skips_complete_model(self):
        model_name = next(iter(PARAKEET_MODELS))
        with tempfile.TemporaryDirectory() as directory:
            model_dir = Path(directory) / model_name
            cfg = BackendConfig(type="parakeet", model_dir=str(model_dir))
            response = io.BytesIO(self._archive(model_name))
            response.headers = {"Content-Length": str(len(response.getvalue()))}
            with patch("voicekey.backends.urllib.request.urlopen", return_value=response):
                predownload(cfg)

            for name in PARAKEET_REQUIRED_FILES:
                self.assertTrue((model_dir / name).is_file())

            with patch(
                "voicekey.backends.urllib.request.urlopen",
                side_effect=AssertionError("complete model should not be downloaded"),
            ):
                predownload(cfg)


if __name__ == "__main__":
    unittest.main()
