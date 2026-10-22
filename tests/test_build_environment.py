import errno
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from test_milestones import ROOT

SPEC = importlib.util.spec_from_file_location("build_environment", ROOT / "lib/build_environment.py")
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)


class BuildEnvironmentTests(unittest.TestCase):
    def test_locale_selection_uses_available_utf8_not_host_default(self):
        listing = b"C\nPOSIX\nlegacy_\xe5\nzz_ZZ.utf8\nen_US.utf8\n"
        with mock.patch.object(environment.subprocess, "check_output", return_value=listing), \
                mock.patch.object(environment.locale, "setlocale", return_value="C") as setting, \
                mock.patch.object(environment.locale, "nl_langinfo", return_value="UTF-8"):
            self.assertEqual(environment.utf8_locale(), "en_US.utf8")
        self.assertEqual(setting.call_args_list[-1], mock.call(environment.locale.LC_CTYPE, "C"))

    def test_c_utf8_is_preferred_when_available(self):
        with mock.patch.object(environment.subprocess, "check_output", return_value=b"en_US.utf8\nC.utf8\n"), \
                mock.patch.object(environment.locale, "setlocale", return_value="C"), \
                mock.patch.object(environment.locale, "nl_langinfo", return_value="UTF-8"):
            self.assertEqual(environment.utf8_locale(), "C.utf8")

    def test_missing_utf8_locale_is_explicit_failure(self):
        with mock.patch.object(environment.subprocess, "check_output", return_value=b"C\nPOSIX\n"), \
                mock.patch.object(environment.locale, "setlocale", return_value="C"):
            with self.assertRaisesRegex(ValueError, "No usable UTF-8 locale"):
                environment.utf8_locale()

    def test_pid_one_does_not_need_subreaper_syscall(self):
        with mock.patch.object(environment.os, "getpid", return_value=1), mock.patch.object(environment.ctypes, "CDLL") as library:
            self.assertEqual(environment.enable_reaper(), "pid-1")
        library.assert_not_called()

    def test_failed_subreaper_syscall_is_not_ignored(self):
        library = mock.Mock()
        library.prctl.return_value = -1
        with mock.patch.object(environment.os, "getpid", return_value=20), \
                mock.patch.object(environment.ctypes, "CDLL", return_value=library), \
                mock.patch.object(environment.ctypes, "get_errno", return_value=errno.EPERM):
            with self.assertRaises(OSError):
                environment.enable_reaper()

    def test_child_status_is_preserved_and_environment_is_recorded(self):
        result = subprocess.run([sys.executable, str(ROOT / "lib/build_environment.py"),
                                 sys.executable, "-c", "raise SystemExit(23)"],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        report = json.loads(result.stdout.split("SHIFT8_BUILD_ENVIRONMENT ", 1)[1])
        self.assertEqual(report["reaper"], "linux-subreaper")
        self.assertIn("utf8", report["utf8_locale"].lower().replace("-", ""))

    def test_adopted_dead_grandchild_is_reaped_while_main_command_runs(self):
        code = '''
import errno, os, subprocess, sys, time
inner = subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(.1)']); print(p.pid)"],
    stdout=subprocess.PIPE, universal_newlines=True)
pid = int(inner.communicate()[0].strip())
deadline = time.time() + 3
while time.time() < deadline:
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            raise SystemExit(0)
        raise
    time.sleep(.05)
raise SystemExit("Adopted grandchild was not reaped")
'''
        result = subprocess.run([sys.executable, str(ROOT / "lib/build_environment.py"), sys.executable, "-c", code],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
