import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from schoolhub_gui import independent_process_environment


class RestartEnvironmentTests(unittest.TestCase):
    def test_restart_does_not_reuse_deleted_frozen_runtime(self):
        original = {'PATH': 'system-path', 'LOCALAPPDATA': 'user-data',
                    '_PYI_APPLICATION_HOME_DIR': 'deleted-runtime',
                    '_PYI_ARCHIVE_FILE': 'SchoolHub.exe',
                    '_PYI_PARENT_PROCESS_LEVEL': '1', '_PYI_SPLASH_IPC': '123',
                    '_MEIPASS2': 'old-runtime', 'PYINSTALLER_SUPPRESS_SPLASH_SCREEN': '1'}
        with patch.dict(os.environ, original, clear=True):
            env = independent_process_environment()
            self.assertEqual(dict(os.environ), original)
        self.assertEqual(env, {'PATH': 'system-path', 'LOCALAPPDATA': 'user-data',
                              'PYINSTALLER_RESET_ENVIRONMENT': '1'})

    def test_clean_launch_still_requests_independent_runtime(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(independent_process_environment(),
                             {'PYINSTALLER_RESET_ENVIRONMENT': '1'})

class IntroAnimationTests(unittest.TestCase):
    def test_boot_animation_moves_and_schedules_next_frame(self):
        import tkinter as tk
        tcl = tk.Tcl()
        tcl.eval('proc winfo {args} {return 1}')
        tcl.eval('proc .root.canvas {args} {set ::last_canvas $args}')
        tcl.eval('proc after {ms callback} {set ::next_frame [list $ms $callback]}')
        tcl.eval((Path(__file__).resolve().parents[1] / 'intro.tcl').read_text())
        first = tcl.eval('set last_canvas')
        tcl.eval('sh_animate')
        self.assertNotEqual(first, tcl.eval('set last_canvas'))
        self.assertEqual(tcl.eval('set next_frame'), '33 sh_animate')
