from __future__ import annotations

import unittest

from voicekey.notify import _coalesce


class CoalesceTests(unittest.TestCase):
    def test_backlog_keeps_every_error_and_the_newest_preview_per_channel(self):
        batch = [
            ("dictate", ["preview 1"]),
            (None, ["error a"]),
            ("agent", ["agent 1"]),
            ("dictate", ["preview 2"]),
            (None, ["error b"]),
            ("dictate", ["preview 3"]),
        ]
        self.assertEqual(
            _coalesce(batch),
            [["error a"], ["error b"], ["agent 1"], ["preview 3"]],
        )


if __name__ == "__main__":
    unittest.main()
