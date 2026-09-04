from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock

from voicekey import polish
from voicekey.config import PolishConfig, PolishServerConfig
from voicekey.polish import (
    InstructFormat, LlamaServer, OpenAIChat, PolishError, Polisher, Reply, S1MiniFormat,
    create_polisher, judge, max_tokens_for, words,
)


class FormatTests(unittest.TestCase):
    def test_s1_mini_sends_its_trained_prompt_and_control_line(self):
        system, user = S1MiniFormat("formal").messages("so um hello")
        self.assertTrue(system.startswith("You are a text normalizer for speech-to-text transcripts."))
        self.assertEqual(user, "[Styling: formal] [Structure: prose] [Context: general]\nso um hello")
        self.assertEqual(S1MiniFormat.extra, {"chat_template_kwargs": {"enable_thinking": False}})

    def test_instruct_fills_the_style_into_the_prompt(self):
        system, user = InstructFormat("Clean it. Style: {style}.", "academic").messages("hi")
        self.assertEqual(system, "Clean it. Style: academic.")
        self.assertEqual(user, "hi")

    def test_prompt_file_replaces_the_built_in_prompt(self):
        self.assertIn("{style}", polish.load_prompt(""))
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write("  mine  \n")
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(polish.load_prompt(handle.name), "mine")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as empty:
            pass
        self.addCleanup(os.unlink, empty.name)
        with self.assertRaises(PolishError):
            polish.load_prompt(empty.name)
        with self.assertRaises(PolishError):
            polish.load_prompt("/nonexistent/prompt.md")


class JudgeTests(unittest.TestCase):
    def test_a_cleanup_is_accepted_however_much_it_removes(self):
        raw = "so um i need to like send the the report by uh friday no wait make that thursday"
        self.assertIsNone(judge(raw, Reply("I need to send the report by Thursday.", True)))
        self.assertIsNone(judge("Yes.", Reply("Yes.", True)))

    def test_a_reply_cut_off_at_the_token_limit_is_rejected(self):
        self.assertIn("cut off", judge("hello there", Reply("Hello there", False)))

    def test_nothing_left_of_a_filler_is_fine_but_not_of_a_sentence(self):
        self.assertIsNone(judge("um", Reply("", True)))
        self.assertIsNone(judge("uh um hmm", Reply("  \n", True)))
        self.assertIn("empty reply", judge("the proof is short and follows", Reply("", True)))

    def test_a_reply_that_grew_is_rejected(self):
        raw = "a short note"
        self.assertIsNone(judge(raw, Reply("A short note, that is.", True)))
        self.assertIn("grew", judge(raw, Reply("A short note. " * 20, True)))

    def test_words_the_speaker_never_said_are_rejected_unless_few(self):
        raw = "the report is due on friday"
        invented = "The quarterly report is due on Friday, and the board expects revenue figures."
        self.assertIn("never said", judge(raw, Reply(invented, True)))
        # Contractions expanded and numbers written out are not inventions.
        self.assertIsNone(judge("i'm gonna send twenty five copies",
                                Reply("I am going to send 25 copies.", True)))
        # A couple of joined or respelled words are tolerated; many are not.
        self.assertIsNone(judge("the voice key code base is alright",
                                Reply("The voicekey codebase is all right.", True)))
        self.assertIn("never said", judge("the voice key code base is alright",
                                          Reply("The voicekey codebase is fine, clean, tested.", True)))

    def test_words_strip_punctuation_and_case(self):
        self.assertEqual(words("Don't, I'm 'fine'."), ["don't", "i'm", "fine"])

    def test_token_room_scales_with_the_input_and_is_capped(self):
        self.assertEqual(max_tokens_for(""), 32)
        self.assertEqual(max_tokens_for("x" * 100), 82)
        self.assertEqual(max_tokens_for("x" * 100000), polish.MAX_TOKENS)


class _Handler(BaseHTTPRequestHandler):
    """A canned OpenAI-compatible endpoint; the test sets ``script``."""

    script: dict = {}
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append((self.path, body, self.headers.get("Authorization")))
        script = self.script
        time.sleep(script.get("delay", 0))
        status = script.get("status", 200)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if status != 200:
            self.wfile.write(b'{"error": "loading model"}')
            return
        if "raw" in script:
            self.wfile.write(script["raw"])
            return
        reply = {"choices": [{"message": {"content": script.get("content", "")},
                              "finish_reason": script.get("finish", "stop")}]}
        self.wfile.write(json.dumps(reply).encode())

    def log_message(self, *args):
        pass


class ChatTests(unittest.TestCase):
    def setUp(self):
        _Handler.script = {}
        _Handler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.chat = OpenAIChat(self.url, "s1-mini", {"chat_template_kwargs": {"enable_thinking": False}},
                               api_key="s3cret")

    def test_request_is_greedy_bounded_and_carries_the_extras(self):
        _Handler.script = {"content": "Hello."}
        reply = self.chat.chat("sys", "user text", 50, 5.0)
        self.assertEqual(reply, Reply("Hello.", True))
        path, body, authorization = _Handler.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer s3cret")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 50)
        self.assertEqual(body["model"], "s1-mini")
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "user text")

    def test_a_reply_that_hit_the_limit_says_so(self):
        _Handler.script = {"content": "Hello", "finish": "length"}
        self.assertFalse(self.chat.chat("s", "u", 5, 5.0).complete)

    def test_server_errors_and_bad_replies_are_polish_errors(self):
        _Handler.script = {"status": 503}
        with self.assertRaisesRegex(PolishError, "HTTP 503"):
            self.chat.chat("s", "u", 5, 5.0)
        _Handler.script = {"raw": b"not json"}
        with self.assertRaises(PolishError):
            self.chat.chat("s", "u", 5, 5.0)
        _Handler.script = {"raw": b'{"choices": []}'}
        with self.assertRaisesRegex(PolishError, "shape"):
            self.chat.chat("s", "u", 5, 5.0)

    def test_nobody_listening_is_a_polish_error_at_once(self):
        started = time.monotonic()
        with self.assertRaises(PolishError):
            OpenAIChat("http://127.0.0.1:1/v1", "m").chat("s", "u", 5, 5.0)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_slow_model_is_abandoned_at_the_timeout(self):
        _Handler.script = {"content": "late", "delay": 1.5}
        started = time.monotonic()
        with self.assertRaises(PolishError):
            self.chat.chat("s", "u", 5, 0.3)
        self.assertLess(time.monotonic() - started, 1.0)


class PolisherTests(unittest.TestCase):
    def _polisher(self, reply=None, error=None):
        backend = Mock()
        if error is not None:
            backend.chat.side_effect = error
        else:
            backend.chat.return_value = reply
        return Polisher(backend, S1MiniFormat("semi-formal"), timeout=10.0), backend

    def test_cleaned_text_comes_back_stripped(self):
        polisher, backend = self._polisher(Reply("  Hello there.\n", True))
        self.assertEqual(polisher.polish("um hello there", 4.0), "Hello there.")
        _system, _user, max_tokens, timeout = backend.chat.call_args[0]
        self.assertEqual(max_tokens, max_tokens_for("um hello there"))
        self.assertEqual(timeout, 4.0, "the wait, being shorter than the request timeout")

    def test_the_raw_text_lands_when_the_model_fails_or_is_not_trusted(self):
        polisher, _ = self._polisher(error=PolishError("down"))
        self.assertIsNone(polisher.polish("hello there friend", 4.0))
        polisher, _ = self._polisher(error=RuntimeError("bug"))
        self.assertIsNone(polisher.polish("hello there friend", 4.0))
        polisher, _ = self._polisher(Reply("Hello", False))
        self.assertIsNone(polisher.polish("hello there friend", 4.0))

    def test_nothing_to_type_is_an_empty_string_not_a_failure(self):
        polisher, _ = self._polisher(Reply("", True))
        self.assertEqual(polisher.polish("um", 4.0), "")

    def test_create_polisher_honours_the_config(self):
        self.assertIsNone(create_polisher(PolishConfig()))
        polisher = create_polisher(PolishConfig(backend="openai", url="http://h:1/v1/", style="formal"))
        self.assertIsInstance(polisher.format, S1MiniFormat)
        self.assertEqual(polisher.format.style, "formal")
        self.assertEqual(polisher.backend.url, "http://h:1/v1")
        self.assertEqual(polisher.backend.extra, S1MiniFormat.extra)
        self.assertIsNone(polisher.backend.api_key)
        server = Mock(api_key="child-key")
        self.assertEqual(create_polisher(PolishConfig(backend="openai"), server).backend.api_key,
                         "child-key")
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as handle:
            handle.write("file-key\n")
        self.addCleanup(os.unlink, handle.name)
        cfg = PolishConfig(backend="openai", api_key_file=handle.name)
        self.assertEqual(create_polisher(cfg).backend.api_key, "file-key")
        with self.assertRaises(PolishError):
            create_polisher(PolishConfig(backend="openai", api_key_file="/nonexistent.key"))
        polisher = create_polisher(PolishConfig(backend="openai", format="instruct", style="plain"))
        self.assertIsInstance(polisher.format, InstructFormat)
        self.assertIn("Style: plain.", polisher.format.system)


FAKE_SERVER = textwrap.dedent("""\
    import json, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        def log_message(self, *a): pass
    import os
    print("model loaded", "key", os.environ.get("LLAMA_API_KEY", "none"), flush=True)
    HTTPServer(("127.0.0.1", port), H).serve_forever()
    """)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = os.path.join(self.directory.name, "state")
        self.model = os.path.join(self.directory.name, "s1-mini-q4_k_m.gguf")
        with open(self.model, "w") as handle:
            handle.write("weights")
        self.fake = os.path.join(self.directory.name, "fake-llama-server")
        with open(self.fake, "w") as handle:
            handle.write(f"#!{sys.executable}\n{FAKE_SERVER}")
        os.chmod(self.fake, 0o755)
        self._state = polish.recovery.STATE_DIR
        polish.recovery.STATE_DIR = self.state
        self.addCleanup(setattr, polish.recovery, "STATE_DIR", self._state)

    def _cfg(self, **server):
        server.setdefault("command", self.fake)
        server.setdefault("model_file", self.model)
        return PolishConfig(backend="openai", url="http://127.0.0.1:18642/v1",
                            server=PolishServerConfig(**server))

    def test_argv_targets_the_configured_url_and_turns_thinking_off(self):
        argv = LlamaServer(self._cfg(threads=3, context=2048)).argv
        self.assertEqual(argv[:3], [self.fake, "-m", self.model])
        for flag, value in (("--host", "127.0.0.1"), ("--port", "18642"), ("-t", "3"), ("-c", "2048"),
                            ("--chat-template-kwargs", '{"enable_thinking":false}'), ("--temp", "0")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--jinja", argv)

    def test_start_runs_the_server_until_stopped_and_logs_privately(self):
        server = polish.start_server(self._cfg())
        self.addCleanup(server.stop)
        self.assertTrue(server.alive)
        self.assertTrue(server.ready(1.0))
        self.assertEqual(oct(os.stat(server.log_path).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o700")
        server.stop()
        self.assertFalse(server.alive)
        with open(server.log_path) as handle:
            self.assertIn(f"model loaded key {server.api_key}", handle.read())
        self.assertNotIn(server.api_key, " ".join(server.argv), "the key is not on the command line")

    def test_missing_command_or_model_is_reported_not_raised_later(self):
        with self.assertRaisesRegex(PolishError, "not found"):
            polish.start_server(self._cfg(command="/nonexistent/llama-server"))
        with self.assertRaisesRegex(PolishError, "missing"):
            polish.start_server(self._cfg(model_file="/nonexistent.gguf"))

    def test_a_server_that_exits_reports_its_output(self):
        with open(self.fake, "w") as handle:
            handle.write(f"#!{sys.executable}\nimport sys; print('bad model'); sys.exit(1)\n")
        with self.assertRaisesRegex(PolishError, "exited: bad model"):
            polish.start_server(self._cfg())

    def test_nothing_is_started_without_a_model_file_or_with_polish_off(self):
        self.assertIsNone(polish.start_server(self._cfg(model_file="")))
        self.assertIsNone(polish.start_server(PolishConfig()))


if __name__ == "__main__":
    unittest.main()
