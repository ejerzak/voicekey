from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import Mock, patch

from voicekey.backends import FasterWhisperBackend
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


if __name__ == "__main__":
    unittest.main()
