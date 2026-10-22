#!/usr/bin/env python
"""Run isolated RPM phases with a usable UTF-8 locale and orphan reaping."""
from __future__ import print_function, unicode_literals

import ctypes
import errno
import json
import locale
import os
import re
import signal
import subprocess
import sys
import time


def utf8_locale():
    listing = subprocess.check_output(["locale", "-a"], env=dict(os.environ, LC_ALL="C"))
    result = [str(name.decode("ascii")) for name in listing.splitlines() if re.match(br"^[A-Za-z0-9_.@-]+\Z", name)]
    preferred = ("c.utf8", "c.utf-8", "en_us.utf8", "en_us.utf-8")
    candidates = sorted(set(result), key=lambda name: (
        preferred.index(name.lower()) if name.lower() in preferred else len(preferred), name,
    ))
    original = locale.setlocale(locale.LC_CTYPE)
    try:
        for candidate in candidates:
            if "utf8" not in candidate.lower().replace("-", ""):
                continue
            locale.setlocale(locale.LC_CTYPE, candidate)
            if locale.nl_langinfo(locale.CODESET).lower().replace("-", "") == "utf8":
                return candidate
    finally:
        locale.setlocale(locale.LC_CTYPE, original)
    raise ValueError("No usable UTF-8 locale in the isolated root; include locale data in its reviewed configuration")


def enable_reaper():
    if os.getpid() == 1:
        return "pid-1"
    if not sys.platform.startswith("linux"):
        raise ValueError("Isolated build reaping requires Linux")
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return "linux-subreaper"


def run(command):
    if not command:
        raise ValueError("An isolated build command is required")
    selected = utf8_locale()
    os.environ["LC_CTYPE"] = selected
    os.environ.pop("LC_ALL", None)
    mode = enable_reaper()
    print("SHIFT8_BUILD_ENVIRONMENT " + json.dumps(dict(
        utf8_locale=selected, reaper=mode, pid=os.getpid(),
    ), sort_keys=True))
    sys.stdout.flush()
    child = subprocess.Popen(command)

    def forward(number, frame):
        if child.returncode is None:
            try:
                os.kill(child.pid, number)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    raise

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, forward)
    while child.returncode is None:
        try:
            pid, status = os.waitpid(-1, 0)
        except OSError as error:
            if error.errno == errno.EINTR:
                continue
            raise
        if pid == child.pid:
            child.returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
    deadline = time.time() + 5
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except OSError as error:
            if error.errno == errno.ECHILD:
                break
            if error.errno == errno.EINTR:
                continue
            raise
        if pid == 0:
            if time.time() >= deadline:
                raise ValueError("Build command left running orphan processes; inspect the preserved isolated root")
            time.sleep(0.05)
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1:]))
    except (ValueError, OSError, locale.Error, subprocess.CalledProcessError) as error:
        print("[shift8-php] isolated environment error: " + str(error), file=sys.stderr)
        sys.exit(7)
