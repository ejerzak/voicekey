"""The DMS plugins' QML, run against mock DMS services in a headless Quickshell."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which('qs') and os.environ.get('WAYLAND_DISPLAY'),
                     'needs Quickshell (qs) in a Wayland session')
class HermesSidebarTests(unittest.TestCase):
    def test_hooks_route_agent_clicks_through_the_daemon(self):
        with tempfile.TemporaryDirectory(prefix='voicekey-qml-test-') as directory:
            shell = Path(directory) / 'shell'
            shutil.copytree(ROOT / 'tests/qml/hermes_sidebar', shell)
            shutil.copy(ROOT / 'contrib/dms/HermesSidebar/HermesSidebar.qml', shell)
            run = subprocess.run(['qs', '-p', str(shell / 'shell.qml')],
                                 capture_output=True, text=True, timeout=20)
        output = re.sub(r'\x1b\[[0-9;]*m', '', run.stdout + run.stderr)
        results = re.findall(r'(PASS|FAIL) (.+)', output)
        failed = [name for verdict, name in results if verdict == 'FAIL']
        self.assertIn('DONE failures=0', output, '\n'.join(failed) or output)
        self.assertEqual(failed, [])
        self.assertGreaterEqual(len(results), 15, output)
