from __future__ import annotations

import subprocess
import unittest
from unittest.mock import call, patch

from voicekey import agent
from voicekey.config import AgentConfig


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class AgentDispatchTests(unittest.TestCase):
    def test_empty_composer_requires_ready_status_and_empty_input(self):
        self.assertTrue(agent._empty_composer("─ ready │ gpt-5\n\n❯ "))
        self.assertTrue(
            agent._empty_composer("─ ready │ gpt-5\n\n❯ Ask me anything…")
        )
        self.assertFalse(agent._empty_composer("─ running… │ gpt-5\n\n❯ "))
        self.assertFalse(agent._empty_composer("─ ready │ gpt-5\n\n❯ draft"))
        self.assertFalse(agent._empty_composer("Approve this command?\n1. Yes\n2. No"))

    @patch("voicekey.agent._tmux")
    @patch("voicekey.agent._wait_for_submission_started")
    @patch("voicekey.agent._wait_for_composer_text")
    @patch("voicekey.agent._wait_for_empty_composer")
    @patch("voicekey.agent._ensure_terminal")
    @patch("voicekey.agent._ensure_session")
    def test_prompt_uses_stdin_and_bracketed_paste(
        self,
        ensure_session,
        ensure_terminal,
        wait_empty,
        wait_text,
        wait_submission,
        tmux,
    ):
        prompt = 'quote " and unicode ∇'
        cfg = AgentConfig()
        target = agent.send_prompt(cfg, prompt)

        ensure_session.assert_called_once_with(cfg)
        ensure_terminal.assert_called_once_with(cfg)
        wait_empty.assert_called_once_with(cfg)
        wait_text.assert_called_once_with(cfg)
        wait_submission.assert_called_once_with(cfg)
        self.assertEqual(target, "Hermes — voicekey-hermes")
        self.assertEqual(
            tmux.call_args_list,
            [
                call(
                    cfg,
                    "load-buffer",
                    "-b",
                    "voicekey-prompt",
                    "-",
                    input_text=prompt + " ",
                ),
                call(
                    cfg,
                    "paste-buffer",
                    "-p",
                    "-d",
                    "-b",
                    "voicekey-prompt",
                    "-t",
                    "voicekey-hermes:0.0",
                ),
                call(
                    cfg,
                    "send-keys",
                    "-t",
                    "voicekey-hermes:0.0",
                    "Enter",
                ),
            ],
        )
        command_arguments = " ".join(
            str(arg)
            for invocation in tmux.call_args_list
            for arg in invocation.args[1:]
        )
        self.assertNotIn(prompt, command_arguments)

    def test_voice_text_cannot_invoke_tui_commands(self):
        self.assertEqual(
            agent._safe_prompt("/new session"), "Voice request: /new session "
        )
        self.assertEqual(
            agent._safe_prompt("!rm -rf /"), "Voice request: !rm -rf / "
        )
        self.assertEqual(agent._safe_prompt("run {!whoami}"), "run { !whoami} ")
        self.assertEqual(agent._safe_prompt("hello\nworld"), "hello world ")

    @patch("voicekey.agent._require", return_value="/opt/hermes")
    @patch("voicekey.agent._workspace", return_value="/work")
    @patch("voicekey.agent._tmux")
    @patch("voicekey.agent._ensure_server")
    def test_new_session_runs_persistent_tui_without_auto_approval(
        self, ensure_server, tmux, workspace, require
    ):
        def run_tmux(_cfg, *args, **_kwargs):
            return completed(1 if args[0] == "has-session" else 0)

        tmux.side_effect = run_tmux
        cfg = AgentConfig()
        self.assertTrue(agent._ensure_session(cfg))

        ensure_server.assert_called_once_with(cfg)
        new_session = next(
            invocation
            for invocation in tmux.call_args_list
            if invocation.args[1] == "new-session"
        )
        command = new_session.args[-1]
        self.assertEqual(command, "/opt/hermes --tui")
        self.assertNotIn("oneshot", command)
        self.assertNotIn("-z", command)
        set_options = [
            invocation
            for invocation in tmux.call_args_list
            if invocation.args[1:3] == ("set-option", "-t")
        ]
        self.assertTrue(set_options)
        self.assertTrue(
            all(invocation.args[3] == "voicekey-hermes" for invocation in set_options)
        )

    @patch("voicekey.agent._run", return_value=completed())
    @patch("voicekey.agent._require", side_effect=lambda name: f"/usr/bin/{name}")
    @patch(
        "voicekey.agent._tmux",
        side_effect=[completed(), completed(stdout="12345\n")],
    )
    def test_terminal_attaches_in_separate_systemd_unit(self, tmux, require, run):
        cfg = AgentConfig()
        self.assertTrue(agent._ensure_terminal(cfg))

        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], [
            "/usr/bin/systemd-run",
            "--user",
            "--quiet",
            "--collect",
        ])
        self.assertIn("/usr/bin/ghostty", argv)
        self.assertEqual(argv[-6:], [
            "/usr/bin/tmux",
            "-L",
            "voicekey-hermes",
            "attach-session",
            "-t",
            "voicekey-hermes",
        ])

    @patch("voicekey.agent._run")
    @patch(
        "voicekey.agent._tmux",
        return_value=completed(stdout="12345\n"),
    )
    def test_existing_terminal_is_reused(self, tmux, run):
        self.assertFalse(agent._ensure_terminal(AgentConfig()))
        run.assert_not_called()

    @patch("voicekey.agent._server_running", return_value=True)
    @patch("voicekey.agent._run", return_value=completed())
    @patch("voicekey.agent._require", side_effect=lambda name: f"/usr/bin/{name}")
    def test_tmux_server_has_stable_supervised_unit(self, require, run, running):
        agent._start_server(AgentConfig())

        argv = run.call_args.args[0]
        self.assertIn("--unit=voicekey-voicekey-hermes-tmux", argv)
        self.assertIn("--property=Restart=always", argv)
        self.assertEqual(argv[-6:], [
            "/usr/bin/tmux",
            "-L",
            "voicekey-hermes",
            "-f",
            "/dev/null",
            "-D",
        ])


if __name__ == "__main__":
    unittest.main()
