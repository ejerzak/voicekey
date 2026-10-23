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
        self.assertGreaterEqual(len(results), 20, output)

    def test_whole_widget_loads_with_revision_and_selects_profiles(self):
        with tempfile.TemporaryDirectory(prefix='voicekey-widget-test-') as directory:
            shell = Path(directory) / 'shell'
            shutil.copytree(ROOT / 'tests/qml/hermes_sidebar', shell)
            shutil.copy(ROOT / 'contrib/dms/Voicekey/Voicekey.qml', shell)
            run = subprocess.run(['qs', '-p', str(shell / 'widget.qml')],
                                 capture_output=True, text=True, timeout=20)
        output = re.sub(r'\x1b\[[0-9;]*m', '', run.stdout + run.stderr)
        self.assertIn('DONE failures=0', output, output)
        self.assertNotIn('FAIL ', output, output)
        self.assertGreaterEqual(output.count('PASS '), 7, output)


class DesktopEntryTests(unittest.TestCase):
    def test_installer_preserves_legacy_and_adds_selected_profile_launcher(self):
        script = (ROOT / 'install.sh').read_text()
        function = script[script.index('install_desktop_entry() {'):script.index('# Link contrib/dms/')]
        with tempfile.TemporaryDirectory(prefix='voicekey-desktop-test-') as directory:
            base = Path(directory)
            env = dict(os.environ, HERE=str(ROOT), VENV=str(base / 'venv with spaces'),
                       DESKTOP=str(base / 'applications/dev.voicekey.Hermes.desktop'),
                       ICON=str(base / 'icons/dev.voicekey.Hermes.png'),
                       AGENT_DESKTOP=str(base / 'applications/dev.voicekey.Agent.desktop'),
                       AGENT_ICON=str(base / 'icons/dev.voicekey.Agent.svg'))
            subprocess.run(['bash', '-eu', '-c', function + '\ninstall_desktop_entry'],
                           env=env, capture_output=True, text=True, check=True)
            for identity in ('Agent', 'Hermes'):
                desktop = base / f'applications/dev.voicekey.{identity}.desktop'
                text = desktop.read_text()
                self.assertIn(f'StartupWMClass=dev.voicekey.{identity}\n', text)
                self.assertIn(f'Exec="{env["VENV"]}/bin/python" -m voicekey --control agent-tap\n', text)
                if shutil.which('desktop-file-validate'):
                    subprocess.run(['desktop-file-validate', str(desktop)],
                                   capture_output=True, text=True, check=True)
            self.assertEqual(Path(env['ICON']).read_bytes(),
                             (ROOT / 'contrib/hermes/dev.voicekey.Hermes.png').read_bytes())
            self.assertEqual(Path(env['AGENT_ICON']).read_bytes(),
                             (ROOT / 'contrib/dms/Voicekey/dev.voicekey.Agent.svg').read_bytes())
