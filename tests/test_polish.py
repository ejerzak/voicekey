from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock, patch

from voicekey import polish
from voicekey.config import PolishConfig, PolishServerConfig
from voicekey.polish import (
    InstructFormat, LlamaServer, OpenAIChat, PolishError, Polisher, Reply, S1MiniFormat,
    create_polisher, diagnostic_polisher, judge, max_tokens_for, words,
)


class FormatTests(unittest.TestCase):
    def test_destination_override_does_not_change_default_or_leak_between_requests(self):
        for format in (S1MiniFormat('semi-formal'),
                       InstructFormat('Style: {style}', 'semi-formal')):
            backend = Mock()
            backend.chat.return_value = Reply('Hello there.', True)
            polisher = Polisher(backend, format, 1, {'org.signal.Signal': 'semi-casual'})
            for app_id, expected in (('org.signal.Signal', 'semi-casual'),
                                     ('emacs', 'semi-formal'), (None, 'semi-formal'),
                                     ('org.signal.Signal.other', 'semi-formal')):
                self.assertEqual(polisher.polish('Hello there.', 1, app_id=app_id), 'Hello there.')
                system, user = backend.chat.call_args.args[:2]
                self.assertIn(expected, system + user)

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


class ContextTests(unittest.TestCase):
    def polisher(self, reply, *alone):
        """REPLY answers the request with context; ALONE, if given, the retry without it."""
        backend = Mock(chat=Mock(return_value=Reply(reply, True)))
        if alone:
            backend.chat.side_effect = [Reply(text, True) for text in (reply, *alone)]
        return Polisher(backend, S1MiniFormat('semi-formal'), 1)

    def test_exact_context_is_removed_and_only_new_text_is_returned(self):
        p = self.polisher("I'd like to go to the store.")
        self.assertEqual(p.polish('Go to the store.', 1, context="I'd like to"), 'go to the store.')
        self.assertTrue(p.backend.chat.call_args.args[1].endswith("I'd like to Go to the store."))
        self.assertEqual(p.last_reason, 'applied')

    def test_names_and_i_are_not_mechanically_lowercased(self):
        for context, raw in [('The person is', 'John Smith.'), ('If so,', 'I agree.')]:
            p = self.polisher(context + ' ' + raw)
            self.assertEqual(p.polish(raw, 1, context=context), raw)

    def test_the_models_edits_to_context_are_discarded(self):
        # Shapes of observed S1-mini replies: it cleans the whole request.
        for context, raw, reply, new in [
            ('Some of the flats on that street', 'Could be', 'Some of the flats on that street could be', 'could be'),
            ('could be', "Reasonable, but I'll look.", "Could be reasonable, but I'll look.", "reasonable, but I'll look."),
            ('rewrite paragraph five, tighten that.', 'Six is fine.', 'rewrite paragraph 5, tighten that. 6 is fine.',
             '6 is fine.'),
            # The dropped 'Um' stays in the buffer; the model's full stop follows it.
            ('burnt on the bottom, etc. Um', 'Yeah, lowering it makes sense.',
             'burnt on the bottom, etc. Yeah, lowering it makes sense.', '. Yeah, lowering it makes sense.'),
            ("I'd like to", 'Go shopping.', 'I want to go shopping.', 'go shopping.'),
            ("I'd like to", 'Go shopping.', 'go shopping.', 'go shopping.'),
        ]:
            p = self.polisher(reply)
            self.assertEqual(p.polish(raw, 1, context=context), new, reply)
            self.assertEqual(p.last_reason, 'applied')
            self.assertEqual(p.backend.chat.call_count, 1)

    def test_a_mark_where_the_context_ends_without_one_leads_the_new_text(self):
        for context, raw, reply, new in [
            ('the question I keep coming back to is', 'What is the cheapest fix?',
             'the question I keep coming back to is, what is the cheapest fix?', ', what is the cheapest fix?'),
            ('So, like', "I'm asking", "So, I'm asking", ", I'm asking"),
            ('the last one leaves around eleven', 'So we can stay.', 'the last one leaves around 11. So we can stay.',
             '. So we can stay.'),
            ('the last one leaves around eleven', 'so we can stay.', 'the last one leaves around 11. so we can stay.',
             '. So we can stay.'),
        ]:
            p = self.polisher(reply)
            self.assertEqual(p.polish(raw, 1, context=context), new, reply)
            self.assertIsNone(p.last_ending)

    def test_a_mark_already_in_the_buffer_stands_and_the_new_text_follows_it(self):
        p = self.polisher('cut this kind of repetitive summary of the method.')
        self.assertEqual(p.polish('Summary of the method.', 1, context='cut this kind of repetitive.'),
                         'Summary of the method.')
        p = self.polisher('lighter than the one I have, or a gravel bike.')
        self.assertEqual(p.polish('Or a gravel bike.', 1, context='lighter than the one I have.'),
                         'Or a gravel bike.')

    def test_an_unusable_reply_is_retried_without_context(self):
        for reply in ("I'd like today.", "I'd like to", 'Shopping. I would like to go.'):
            p = self.polisher(reply, 'Go shopping.')
            self.assertEqual(p.polish('Go shopping.', 1, context="I'd like to"), 'Go shopping.')
            self.assertEqual(p.last_reason, 'applied without context: could not separate new text from context')
            self.assertEqual(p.backend.chat.call_args.args[1], S1MiniFormat('semi-formal').messages('Go shopping.')[1])
        p = self.polisher("I'd like to", "")
        self.assertIsNone(p.polish('Go shopping.', 1, context="I'd like to"))
        self.assertEqual(p.last_reason, 'empty reply')

    def test_draft_may_revise_only_the_contexts_final_marks(self):
        context = "It's possible we should pop into insert mode."
        for reply, ending in ((context[:-1] + " when we're doing dictation.", ''),
                              (context[:-1] + ", when we're doing dictation.", ',')):
            p = self.polisher(reply)
            self.assertEqual(p.polish("when we're doing dictation.", 1, context=context, revise_end=True),
                             "when we're doing dictation.")
            self.assertEqual(p.last_ending, ending)
            # Ordinary dictation's context is already in the buffer: its full
            # stop stands, so the new text starts a sentence.
            p = self.polisher(reply)
            self.assertEqual(p.polish("when we're doing dictation.", 1, context=context),
                             "When we're doing dictation.")
            self.assertIsNone(p.last_ending)
        p = self.polisher('It works in drafts. Then we accept.')
        self.assertEqual(p.polish('then we accept.', 1, context='It works in drafts', revise_end=True),
                         'Then we accept.')
        self.assertEqual(p.last_ending, '.')
        p = self.polisher(context + ' When dictating.')
        self.assertEqual(p.polish('When dictating.', 1, context=context, revise_end=True), 'When dictating.')
        self.assertIsNone(p.last_ending)

    def test_recorded_s1_mini_replies_keep_their_new_text(self):
        # Synthetic requests shaped like failures seen in persistent sessions,
        # with S1-mini's actual replies (2026-10-03). 'old_rule' is what the
        # exact-prefix check did with them.
        path = os.path.join(os.path.dirname(__file__), 'data', 'polish_context_replies.json')
        with open(path, encoding='utf-8') as handle:
            cases = json.load(handle)
        for case in cases:
            p = self.polisher(case['reply'])
            self.assertEqual(p.polish(case['raw'], 1, context=case['context']), case['new'], case['reply'])
            self.assertEqual(p.last_reason, 'applied')

    def test_words_copied_from_context_into_suffix_are_rejected(self):
        p = self.polisher('Send it to Ethan. Tomorrow Ethan.', 'Tomorrow.')
        self.assertEqual(p.polish('Tomorrow.', 1, context='Send it to Ethan.'), 'Tomorrow.')
        self.assertEqual(p.last_reason, 'applied without context: previous context leaked into new text')
        p = self.polisher('We agreed. We will go.')
        self.assertEqual(p.polish("We'll go.", 1, context='We agreed.'), 'We will go.')

    def test_suffix_is_judged_against_new_text_not_context(self):
        p = self.polisher('Earlier we agreed. Publish this.')
        self.assertIsNone(p.polish('Do not publish this.', 1, context='Earlier we agreed.'))
        p = self.polisher('Earlier we agreed. ' + 'Invented explanation. ' * 20)
        self.assertIsNone(p.polish('Yes.', 1, context='Earlier we agreed.'))
        p = self.polisher('Earlier we agreed. Yes.')
        p.backend.chat.return_value = Reply('Earlier we agreed. Yes.', False)
        self.assertIsNone(p.polish('Yes.', 1, context='Earlier we agreed.'))

    def test_context_is_bounded_preserves_whitespace_and_does_not_split_words(self):
        text = ' '.join(str(n) for n in range(70))
        self.assertEqual(polish.context_tail(text), ' '.join(str(n) for n in range(20, 70)))
        self.assertEqual(polish.context_tail('a' * 900 + ' tail.'), 'tail.')
        self.assertEqual(polish.context_tail('a' * 900), '')
        self.assertEqual(polish.context_tail('  A  sentence,\nwith spacing.  '), 'A  sentence,\nwith spacing.')
        p = self.polisher('Yes.')
        self.assertEqual(p.polish('Yes.', 1, context='a' * 900), 'Yes.')

    def test_empty_context_preserves_original_request(self):
        p = self.polisher('Hello.')
        self.assertEqual(p.polish('Hello.', 1), 'Hello.')
        self.assertEqual(p.backend.chat.call_args.args[:2], S1MiniFormat('semi-formal').messages('Hello.'))


class JudgeTests(unittest.TestCase):
    def test_filler_only_matches_stretched_noise_but_preserves_meaningful_text(self):
        for text in ('Um', 'er ach um errrr uhhhh ugh gah', 'Uuuummmm…',
                     'Ah! Ehh, erm.', 'hmmmm', 'arrrghhh'):
            with self.subTest(text=text):
                self.assertTrue(polish.filler_only(text))
        for text in ('', '...', 'No.', 'Yes.', 'I disagree.', 'Do not publish.',
                     'uh-huh', 'uh-uh', 'um 42', 'um 你好', '"um"', '“um”',
                     'The word is um.', 'Ugh, this is wrong.', 'early', 'grammar'):
            with self.subTest(text=text):
                self.assertFalse(polish.filler_only(text))

    def test_a_cleanup_can_remove_corrections_and_fillers(self):
        raw = "so um i need to like send the the report by uh friday no wait make that thursday"
        self.assertIsNone(judge(raw, Reply("I need to send the report by Thursday.", True)))
        self.assertIsNone(judge("Yes.", Reply("Yes.", True)))

    def test_a_reply_cut_off_at_the_token_limit_is_rejected(self):
        self.assertIn("cut off", judge("hello there", Reply("Hello there", False)))

    def test_all_empty_results_fall_back_to_raw(self):
        self.assertEqual(judge("um", Reply("", True)), "empty reply")
        self.assertEqual(judge("uh um hmm", Reply("  \n", True)), "empty reply")
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
        try:
            self.wfile.write(json.dumps(reply).encode())
        except BrokenPipeError:
            pass  # timeout tests deliberately close the client first

    def log_message(self, *args):
        pass


class ChatTests(unittest.TestCase):
    def setUp(self):
        _Handler.script = {}
        _Handler.requests = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
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

    def test_diagnostic_uses_the_external_endpoint_and_its_key(self):
        _Handler.script = {"content": "Hello there."}
        with tempfile.NamedTemporaryFile("w") as key:
            key.write("external-key\n")
            key.flush()
            cfg = PolishConfig(backend="openai", url=self.url, api_key_file=key.name)
            with patch.object(polish, "start_server") as start:
                with diagnostic_polisher(cfg) as polisher:
                    self.assertEqual(polisher.polish("um hello there", 4.0), "Hello there.")
                start.assert_not_called()
        self.assertEqual(_Handler.requests[0][2], "Bearer external-key")


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

    def test_empty_reply_uses_raw_fallback(self):
        polisher, _ = self._polisher(Reply("", True))
        self.assertIsNone(polisher.polish("um", 4.0))

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
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Authorization") != "Bearer " + os.environ["LLAMA_API_KEY"]:
                self.send_response(401); self.end_headers()
                return
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": "Hello there."},
                                                      "finish_reason": "stop"}]}).encode())
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
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return PolishConfig(backend="openai", url=f"http://127.0.0.1:{port}/v1",
                            server=PolishServerConfig(**server))

    def test_argv_targets_the_configured_url_and_turns_thinking_off(self):
        server = LlamaServer(self._cfg(threads=3, context=2048))
        argv = server.argv
        self.assertEqual(argv[:3], [self.fake, "-m", self.model])
        for flag, value in (("--host", "127.0.0.1"), ("--port", str(server.port)), ("-t", "3"), ("-c", "2048"),
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

    def test_diagnostics_leave_the_running_server_config_and_log_alone(self):
        cfg = self._cfg()
        original_url = cfg.url
        live = polish.start_server(cfg)
        self.addCleanup(live.stop)
        with open(live.log_path) as handle:
            original_log = handle.read()
        with diagnostic_polisher(cfg) as first, diagnostic_polisher(cfg) as second:
            urls = {original_url, first.backend.url, second.backend.url}
            self.assertEqual(len(urls), 3)
            keys = {live.api_key, first.backend.api_key, second.backend.api_key}
            self.assertEqual(len(keys), 3)
            for polisher in (first, second):
                self.assertEqual(polisher.polish("um hello there", 4.0), "Hello there.")
        self.assertEqual(cfg.url, original_url)
        self.assertTrue(live.alive)
        self.assertTrue(live.ready(1.0))
        with open(live.log_path) as handle:
            self.assertEqual(handle.read(), original_log)
        for polisher in (first, second):
            with self.assertRaises(PolishError):
                polisher.backend.chat("s", "u", 5, 1.0)

    def test_failed_diagnostic_preserves_the_live_log_and_reports_its_own(self):
        cfg = self._cfg()
        live = polish.start_server(cfg)
        self.addCleanup(live.stop)
        with open(live.log_path) as handle:
            original_log = handle.read()
        with open(self.fake, "w") as handle:
            handle.write(f"#!{sys.executable}\nprint('bad diagnostic model'); raise SystemExit(1)\n")
        with self.assertRaisesRegex(PolishError, "exited: bad diagnostic model"):
            with diagnostic_polisher(cfg):
                self.fail("a failed diagnostic must not yield a polisher")
        self.assertTrue(live.alive)
        with open(live.log_path) as handle:
            self.assertEqual(handle.read(), original_log)

    def test_diagnostic_cleans_up_its_process_and_log_on_error(self):
        started = []
        original_start = polish.start_server

        def start(*args, **kwargs):
            server = original_start(*args, **kwargs)
            started.append(server)
            return server

        with patch.object(polish, "start_server", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "interrupted check"):
                with diagnostic_polisher(self._cfg()):
                    raise RuntimeError("interrupted check")
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].alive)
        self.assertFalse(os.path.exists(os.path.dirname(started[0].log_path)))

    def test_interrupted_start_stops_the_child(self):
        with patch.object(LlamaServer, "ready", side_effect=KeyboardInterrupt):
            with patch.object(LlamaServer, "stop", autospec=True, side_effect=LlamaServer.stop) as stop:
                with self.assertRaises(KeyboardInterrupt):
                    polish.start_server(self._cfg())
        server = stop.call_args.args[0]
        self.assertFalse(server.alive)


if __name__ == "__main__":
    unittest.main()

class PreservationTests(unittest.TestCase):
    def test_empty_content_and_lost_negations_are_rejected(self):
        for text in ('I disagree.', 'Do not publish.', 'Yes.'):
            self.assertIsNotNone(judge(text, Reply('', True)))
        for raw, final in (("The conclusion does not follow.", "The conclusion does follow."),
                           ("It isn't valid.", "It is valid."),
                           ("It isn’t valid.", "It is valid."),
                           ("Perhaps the premise holds.", "The premise holds."),
                           ("See page 25.", "See page 52.")):
            self.assertIsNotNone(judge(raw, Reply(final, True)))

    def test_total_deadline_and_busy_slot_are_bounded(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        backend = Mock()
        def chat(*args):
            entered.set()
            release.wait(2)
            return Reply('Late.', True)
        backend.chat.side_effect = chat
        polisher = Polisher(backend, S1MiniFormat('formal'), 1)
        self.assertIsNone(polisher.polish('raw text', 0.03))
        self.assertTrue(entered.is_set())
        self.assertIsNone(polisher.polish('second', 0.03))
        self.assertEqual(backend.chat.call_count, 1)
        release.set()
        polisher._slot.join(1)
