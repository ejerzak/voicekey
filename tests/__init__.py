"""Voicekey's own emacsclient calls must never reach the user's Emacs server.

Point them at a socket that does not exist (and never start an alternate
editor). Tests that need Emacs run a private server and name it with -s.
"""
import os
import tempfile

os.environ["EMACS_SOCKET_NAME"] = os.path.join(tempfile.gettempdir(), "voicekey-tests-no-emacs-server")
os.environ["ALTERNATE_EDITOR"] = "false"

# Nor may any test reach the user's Hermes: give the agent module a subprocess
# whose run() refuses, so tmux, systemd-run, Ghostty and niri never start.
# Tests mock voicekey.agent._run or the pipeline's send_agent instead.
import subprocess
import sys
import types

import voicekey.agent


def _refuse(argv, *args, **kwargs):
    message = f"test tried to run {argv[0]!r} for real; mock the agent instead"
    print(f"voicekey tests: {message}", file=sys.__stderr__)
    raise AssertionError(message)


voicekey.agent.subprocess = types.ModuleType("subprocess")
voicekey.agent.subprocess.__dict__.update(vars(subprocess))
voicekey.agent.subprocess.run = _refuse
