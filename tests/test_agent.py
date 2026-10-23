from __future__ import annotations

import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest.mock import call, patch

from voicekey import agent
from voicekey.config import AgentConfig


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class AgentTests(unittest.TestCase):
    @patch("voicekey.agent._run", return_value=completed())
    @patch("voicekey.agent._require", side_effect=lambda name: f"/usr/bin/{name}")
    def test_remote_tmux_uses_strict_openssh_over_tailscale_and_stdin(
        self, require, run
    ):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
        )
        agent._tmux(
            cfg,
            "load-buffer",
            "-b",
            "voicekey-prompt",
            "-",
            input_text="private prompt",
        )

        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("/home/alice/.ssh/id_ed25519", argv)
        self.assertIn("ProxyCommand=/usr/bin/tailscale nc %h %p", argv)
        self.assertIn("alice@desktop", argv)
        self.assertEqual(
            argv[-1],
            "tmux -L voicekey-hermes load-buffer -b voicekey-prompt -",
        )
        self.assertEqual(run.call_args.kwargs["input_text"], "private prompt")
        self.assertNotIn("private prompt", " ".join(argv))

    @patch("voicekey.agent._remote_run", return_value=completed())
    def test_remote_target_check_verifies_remote_dependencies(self, remote_run):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
        )
        self.assertIsNone(agent.check_target(cfg))
        command = remote_run.call_args.args[1]
        self.assertEqual(command[-3:], ["tmux", "systemd-run", "hermes"])
        self.assertIn('for cmd in "$@"', command[2])

    @patch(
        "voicekey.agent._remote_run",
        side_effect=agent.AgentError("command timed out after 10s: ssh"),
    )
    def test_remote_target_check_reports_connection_failure(self, remote_run):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
        )
        self.assertEqual(
            agent.check_target(cfg), "command timed out after 10s: ssh"
        )

    @patch("voicekey.agent.Path.stat", return_value=SimpleNamespace(st_size=10))
    @patch("voicekey.agent.Path.is_file", return_value=True)
    @patch("voicekey.agent._client_count", side_effect=[1, 2])
    @patch("voicekey.agent._terminal_window_open", side_effect=[False, True])
    @patch("voicekey.agent._run", return_value=completed())
    @patch("voicekey.agent._require", side_effect=lambda name: f"/usr/bin/{name}")
    def test_remote_terminal_uses_verified_ssh_and_allocates_tty(
        self, require, run, window_open, client_count, is_file, stat
    ):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
        )
        self.assertTrue(agent._ensure_terminal(cfg))

        argv = run.call_args.args[0]
        ssh_index = argv.index("/usr/bin/ssh")
        ssh_argv = argv[ssh_index:]
        self.assertIn("-tt", ssh_argv)
        self.assertIn("StrictHostKeyChecking=yes", ssh_argv)
        self.assertIn(
            "ProxyCommand=/usr/bin/tailscale nc %h %p", ssh_argv
        )
        self.assertIn("alice@desktop", ssh_argv)
        self.assertEqual(
            ssh_argv[-1],
            "tmux -L voicekey-hermes attach-session -t voicekey-hermes",
        )
        self.assertIn("--title=Voicekey Hermes", argv)
        self.assertIn("--class=dev.voicekey.Hermes", argv)

    @patch("voicekey.agent.focus.compositor", return_value="niri")
    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch(
        "voicekey.agent._run",
        return_value=completed(
            stdout='[{"id":3,"app_id":"dev.voicekey.Hermes","title":"Other"},'
            '{"id":5,"app_id":"com.mitchellh.ghostty","title":"Voicekey Hermes"},'
            '{"id":7,"app_id":"dev.voicekey.Hermes","title":"Voicekey Hermes"}]'
        ),
    )
    def test_remote_terminal_window_is_identified_by_app_id_and_title(self, run, require, compositor):
        cfg = AgentConfig(terminal_title="Voicekey Hermes")
        self.assertTrue(agent._terminal_window_open(cfg))
        self.assertEqual(agent._terminal_window(cfg)["id"], 7)

    @patch("voicekey.agent.focus.compositor", return_value="niri")
    @patch("voicekey.agent._terminal_window", return_value={"id": 7, "workspace_id": 1})
    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch("voicekey.agent._run", return_value=completed())
    def test_focus_raises_the_titled_terminal_on_niri(self, run, require, window, compositor):
        self.assertEqual(agent._focus_terminal(AgentConfig()), 7)
        run.assert_called_once_with(
            ["/usr/bin/niri", "msg", "action", "focus-window", "--id", "7"], timeout=10.0)

    _WORKSPACES = ('[{"id":1,"idx":1,"name":"emacs","output":"eDP-1","is_focused":false},'
                   '{"id":3,"idx":3,"name":null,"output":"eDP-1","is_focused":true},'
                   '{"id":5,"idx":1,"name":"hermes","output":"HDMI-1","is_focused":false}]')

    def _action(self, *argv):
        return call(["/usr/bin/niri", "msg", "action", *argv], timeout=10.0)

    def _raise_parked(self, run, workspace_id):
        """Raise a terminal on WORKSPACE_ID with parking on; return the actions."""
        run.side_effect = [completed(stdout=self._WORKSPACES), completed(), completed()]
        with patch("voicekey.agent._terminal_window",
                   return_value={"id": 7, "workspace_id": workspace_id}):
            self.assertEqual(agent._focus_terminal(AgentConfig(park_workspace="hermes")), 7)
        return run.call_args_list[1:]

    @patch("voicekey.agent.focus.compositor", return_value="niri")
    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch("voicekey.agent._run")
    def test_parked_terminal_comes_to_the_focused_workspace(self, run, require, compositor):
        self.assertEqual(self._raise_parked(run, 1), [
            self._action("move-window-to-workspace", "--window-id", "7", "--focus", "false", "3"),
            self._action("focus-window", "--id", "7"),
        ])

    @patch("voicekey.agent.focus.compositor", return_value="niri")
    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch("voicekey.agent._run")
    def test_terminal_on_another_monitor_moves_to_the_focused_one(self, run, require, compositor):
        self.assertEqual(self._raise_parked(run, 5), [
            self._action("move-window-to-monitor", "--id", "7", "eDP-1"),
            self._action("focus-window", "--id", "7"),
        ])

    @patch("voicekey.agent.focus.compositor", return_value="niri")
    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch("voicekey.agent._run")
    def test_terminal_already_on_the_focused_workspace_is_only_focused(self, run, require, compositor):
        self.assertEqual(self._raise_parked(run, 3), [self._action("focus-window", "--id", "7")])

    @patch("voicekey.agent._require", return_value="/usr/bin/niri")
    @patch("voicekey.agent._run", return_value=completed())
    def test_park_sends_the_terminal_back_without_moving_focus(self, run, require):
        agent.park_terminal(AgentConfig(park_workspace="hermes"), 7, deadline=time.monotonic() + 1)
        run.assert_called_once_with(
            ["/usr/bin/niri", "msg", "action", "move-window-to-workspace", "--window-id", "7",
             "--focus", "false", "hermes"], timeout=10.0)

    @patch("voicekey.agent.focus.compositor", return_value="sway")
    @patch("voicekey.agent._run")
    def test_focus_is_skipped_without_niri(self, run, compositor):
        self.assertIsNone(agent._focus_terminal(AgentConfig()))
        run.assert_not_called()

    @patch("voicekey.agent._ensure_session")
    @patch("voicekey.agent._focus_terminal", return_value=None)
    def test_raise_terminal_never_sets_anything_up(self, focus_terminal, session):
        self.assertIsNone(agent.raise_terminal(AgentConfig(), deadline=time.monotonic() + 1))
        session.assert_not_called()

    @patch("voicekey.agent._focus_terminal", side_effect=[None, 7])
    @patch("voicekey.agent._ensure_terminal")
    @patch("voicekey.agent._ensure_session")
    def test_show_without_a_window_starts_session_and_terminal_then_raises_it(
        self, session, terminal, focus_terminal
    ):
        cfg = AgentConfig()
        agent.show(cfg, deadline=time.monotonic() + 10)
        session.assert_called_once_with(cfg)
        terminal.assert_called_once_with(cfg)
        self.assertEqual(focus_terminal.call_count, 2)

    @patch("voicekey.agent._focus_terminal", return_value=7)
    @patch("voicekey.agent._ensure_terminal")
    @patch("voicekey.agent._ensure_session")
    def test_show_raises_an_open_window_without_session_checks(self, session, terminal, focus_terminal):
        agent.show(AgentConfig(transport="ssh-over-tailscale"), deadline=time.monotonic() + 10)
        focus_terminal.assert_called_once()
        session.assert_not_called()
        terminal.assert_not_called()

    @patch("voicekey.agent._session_has_client", return_value=True)
    @patch("voicekey.agent.focus.compositor", return_value="sway")
    def test_without_niri_the_terminal_check_falls_back_to_tmux_clients(self, compositor, clients):
        self.assertTrue(agent._terminal_window_open(AgentConfig()))

    @patch("voicekey.agent._terminal_window_open")
    @patch("voicekey.agent._run")
    def test_no_terminal_wanted_means_no_compositor_query(self, run, window_open):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
            open_terminal=False,
        )
        self.assertFalse(agent._ensure_terminal(cfg))
        window_open.assert_not_called()
        run.assert_not_called()

    @patch(
        "voicekey.agent._target_run",
        return_value=completed(stdout="/home/alice/.local/share/voicekey/hermes\n"),
    )
    def test_remote_workspace_resolves_home_on_the_remote_host(self, target_run):
        cfg = AgentConfig(
            transport="ssh-over-tailscale",
            remote_host="desktop",
            remote_user="alice",
            identity_file="/home/alice/.ssh/id_ed25519",
            working_directory="~/.local/share/voicekey/hermes",
        )
        self.assertEqual(agent._workspace(cfg), "/home/alice/.local/share/voicekey/hermes")
        argv = target_run.call_args.args[1]
        self.assertEqual(argv[:2], ["sh", "-c"])
        self.assertIn('"$HOME/$1"', argv[2])
        self.assertEqual(argv[-1], ".local/share/voicekey/hermes")
        self.assertNotIn("/home/", " ".join(argv), "no local path reaches the remote")

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
        self.assertIn("--class=dev.voicekey.Hermes", argv)  # its own app in docks
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

class CancellationTests(unittest.TestCase):
    def test_cancelled_operation_never_starts_another_external_command(self):
        import threading
        import time
        from voicekey import agent
        cancelled = threading.Event()
        cancelled.set()
        token = agent._operation.set((time.monotonic() + 10, cancelled))
        try:
            with patch('voicekey.agent.subprocess.run') as run:
                with self.assertRaises(agent.AgentError):
                    agent._run(['tmux', 'send-keys', 'Enter'], timeout=1)
            run.assert_not_called()
        finally:
            agent._operation.reset(token)

class AgentProfileTests(unittest.TestCase):
    @patch("voicekey.agent._require", return_value="/opt/bin/codex with spaces")
    @patch("voicekey.agent._workspace", return_value="/work")
    @patch("voicekey.agent._tmux")
    @patch("voicekey.agent._ensure_server")
    def test_custom_command_preserves_every_argument(self, server, tmux, workspace, require):
        import shlex
        tmux.side_effect = lambda _cfg, *args, **kwargs: completed(1 if args[0] == "has-session" else 0)
        cfg = AgentConfig(profile_id="codex", command=["codex", "$(touch /tmp/no)", "a'b", "", "two words"])
        agent._ensure_session(cfg)
        invocation = next(c for c in tmux.call_args_list if c.args[1] == "new-session")
        self.assertEqual(shlex.split(invocation.args[-1]),
                         ["/opt/bin/codex with spaces", *cfg.command[1:]])
        require.assert_called_once_with("codex")

    @patch("voicekey.agent._remote_executable", return_value="/home/alice/bin/claude")
    @patch("voicekey.agent._workspace", return_value="/work")
    @patch("voicekey.agent._tmux")
    @patch("voicekey.agent._ensure_server")
    def test_remote_custom_executable_is_resolved_on_remote_host(self, server, tmux, workspace, executable):
        tmux.side_effect = lambda _cfg, *args, **kwargs: completed(1 if args[0] == "has-session" else 0)
        cfg = AgentConfig(transport="ssh-over-tailscale", command=["~/bin/claude", "--continue"])
        agent._ensure_session(cfg)
        executable.assert_called_once_with(cfg, "~/bin/claude")
        invocation = next(c for c in tmux.call_args_list if c.args[1] == "new-session")
        self.assertEqual(invocation.args[-1], "/home/alice/bin/claude --continue")

    @patch("voicekey.agent._require", side_effect=lambda command: (_ for _ in ()).throw(agent.AgentError("codex not found")) if command == "codex" else "/usr/bin/" + command)
    def test_local_diagnostics_check_the_actual_command(self, require):
        self.assertEqual(agent.check_target(AgentConfig(command=["codex", "--resume"])), "codex not found")
        self.assertIn(call("codex"), require.call_args_list)
        self.assertNotIn(call("hermes"), require.call_args_list)

    @patch("voicekey.agent._remote_run", return_value=completed())
    def test_remote_diagnostics_pass_command_as_an_argument(self, run):
        cfg = AgentConfig(transport="ssh-over-tailscale", command=["/opt/my agent;literal", "--continue"])
        self.assertIsNone(agent.check_target(cfg))
        command = run.call_args.args[1]
        self.assertEqual(command[-1], "/opt/my agent;literal")
        self.assertNotIn("/opt/my agent;literal", command[2])

    def test_minimal_daemon_path_finds_user_tools_and_explicit_paths(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / ".local/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"HOME": directory, "PATH": "/usr/bin"}):
                self.assertEqual(agent._require("codex"), str(binary))
                self.assertEqual(agent._require("~/.local/bin/codex"), str(binary))
                self.assertEqual(agent._require(str(binary)), str(binary))

    @patch("voicekey.agent._niri_list", return_value=[
        {"id": 1, "app_id": "dev.voicekey.Agent", "title": "Voicekey hermes"},
        {"id": 2, "app_id": "dev.voicekey.Agent", "title": "Voicekey codex"},
    ])
    def test_shared_agent_app_id_keeps_profile_windows_distinct(self, windows):
        cfg = AgentConfig(profile_id="codex", app_id="dev.voicekey.Agent", terminal_title="Voicekey codex")
        self.assertEqual(agent._terminal_window(cfg)["id"], 2)
