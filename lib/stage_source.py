#!/usr/bin/env python
"""Keep only the reviewed replacement payload after INSTALL_ROOT staging."""
from __future__ import print_function, unicode_literals

import io
import json
import os
import sys
import re
import stat
import subprocess


def verify_runtime_paths(root, private):
    """Every staged ELF that needs the private OpenSSL must find it at its final prefix, never a build path."""
    library = private + "/lib64"
    for current, _, files in os.walk(root):
        for name in files:
            target = os.path.join(current, name)
            if os.path.islink(target) or not os.path.isfile(target):
                continue
            with open(target, "rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            dynamic = subprocess.Popen(["readelf", "-d", target], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = dynamic.communicate()[0].decode("utf-8", "replace")
            paths = []
            for line in output.splitlines():
                if "(RPATH)" in line or "(RUNPATH)" in line:
                    paths.extend(line.split("[", 1)[1].rsplit("]", 1)[0].split(":"))
            relative = target[len(root):]
            if any(path.startswith(("/builddir", "/tmp", "/var/tmp")) or "openssl-stage" in path for path in paths):
                raise ValueError("Build-only runtime path remains in " + relative + ": " + ":".join(paths))
            needs = re.search(r"Shared library: \[lib(?:ssl|crypto)\.so\.3\]", output) is not None
            if needs and library not in paths and not relative.startswith(private + "/"):
                raise ValueError("Missing private OpenSSL runtime path in " + relative)
            if needs and relative.startswith(private + "/") and library not in paths and "libssl.so" in relative:
                raise ValueError("Private libssl lacks its own runtime path")


def stage(root, specification):
    root = os.path.abspath(root)
    with io.open(specification, encoding="utf-8") as stream:
        data = json.load(stream)
    allowed = set(data["files"])
    private = data["private_prefix"]
    if root == "/" or not private.startswith("/opt/shift8-php/deps/"):
        raise ValueError("Only an isolated staging root and private dependency prefix are permitted")
    for path in allowed:
        if not path.startswith("/") or os.path.normpath(path) != path or ".." in path.split("/"):
            raise ValueError("Unsafe staging file path")
        target = root + path
        if not os.path.lexists(target):
            raise ValueError("Build is missing an installed replacement file: " + path)
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        for name in files + [name for name in directories if os.path.islink(os.path.join(current, name))]:
            target = os.path.join(current, name)
            relative = target[len(root):]
            if relative not in allowed and not relative.startswith(private + "/"):
                os.unlink(target)
                continue
            facts = os.lstat(target)
            if stat.S_ISREG(facts.st_mode):
                # Binaries/scripts executable; modules and dependency data readable.
                os.chmod(target, 0o755 if facts.st_mode & 0o111 else 0o644)
            elif stat.S_ISLNK(facts.st_mode):
                resolved = os.path.normpath(os.path.join(os.path.dirname(relative), os.readlink(target)))
                if resolved not in allowed and not resolved.startswith(private + "/"):
                    raise ValueError("Replacement symlink escapes the reviewed payload: " + relative)
            else:
                raise ValueError("Special files are forbidden in staged payloads")
        for name in directories:
            target = os.path.join(current, name)
            if not os.path.islink(target):
                if not os.listdir(target):
                    os.rmdir(target)
                else:
                    os.chmod(target, 0o755)
    verify_runtime_paths(root, private)


if __name__ == "__main__":
    try:
        stage(*sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("[shift8-php] staging error: " + str(error), file=sys.stderr)
        sys.exit(7)
