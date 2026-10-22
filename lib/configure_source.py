#!/usr/bin/env python
"""Execute and verify the persisted PHP configure argv without a shell."""
from __future__ import print_function, unicode_literals

import io
import json
import re
import shlex
import subprocess
import sys


def validate(arguments, help_text):
    if not isinstance(arguments, list) or not arguments:
        raise ValueError("Nonempty configure argument array is required")
    names = []
    for argument in arguments:
        if not isinstance(argument, type("")) or not re.match(r"--[a-z][a-z0-9-]*(?:=[^\x00\r\n]+)?\Z", argument):
            raise ValueError("Malformed configure argument: " + str(argument))
        name = argument.split("=", 1)[0]
        canonical = name.replace("--disable-", "--enable-", 1).replace("--without-", "--with-", 1)
        inverse = canonical.replace("--enable-", "--disable-", 1).replace("--with-", "--without-", 1)
        if not any(re.search(re.escape(candidate) + r"(?:[=\s\[]|$)", help_text) for candidate in (name, canonical, inverse)):
            raise ValueError("Target configure does not support " + name)
        names.append(canonical)
    if len(names) != len(set(names)):
        raise ValueError("Duplicate or contradictory configure options")


def verify_nice(arguments, text):
    tokens = shlex.split(text.replace("\\\n", ""), comments=True)
    actual = [token for token in tokens if token.startswith("--")]
    if actual != arguments:
        raise ValueError("config.nice does not preserve the exact effective argument array")


def run(path):
    with io.open(path, encoding="utf-8") as stream:
        arguments = json.load(stream)
    help_text = subprocess.check_output(["./configure", "--help"]).decode("utf-8")
    validate(arguments, help_text)
    subprocess.check_call(["./configure"] + arguments)
    with io.open("config.nice", encoding="utf-8") as stream:
        verify_nice(arguments, stream.read())
    print("SHIFT8_CONFIGURE_ARGUMENTS " + json.dumps(arguments))


if __name__ == "__main__":
    try:
        run(sys.argv[1])
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("[shift8-php] configure error: " + str(error), file=sys.stderr)
        sys.exit(7)
