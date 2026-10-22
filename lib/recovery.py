#!/usr/bin/env python
"""Collect signature-verified rollback RPMs for the exact installed core packages."""
from __future__ import print_function, unicode_literals

import os
import re
import subprocess
import sys

import metadata as meta

FORMAT = "%{NAME}\\t%{EPOCH}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}\\n"
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def nevra(item):
    return "{name}-{version}-{release}.{architecture}".format(**item)


def locations(item, repositories):
    command = ["repoquery", "--qf", "%{name}", "--location", nevra(item)]
    if repositories:
        command[1:1] = ["--disablerepo=*", "--enablerepo=" + ",".join(repositories)]
    result = meta.run(command)
    if result.returncode:
        raise ValueError("repoquery failed for {}: {}".format(nevra(item), result.stderr.strip()[-300:]))
    urls = sorted(set(
        "https://" + line[len("http://"):] if line.startswith("http://") else line
        for line in result.stdout.splitlines() if line.startswith(("http://", "https://"))
    ))
    if len(urls) != 1:
        raise ValueError("Exactly one repository copy of {} is required; found {}".format(nevra(item), len(urls)))
    return urls[0]


def collect(discovery_path, destination, repositories=()):
    for name in repositories:
        if not REPOSITORY.match(name):
            raise ValueError("Invalid repository id: " + name)
    data = meta.load(discovery_path)
    if data["package_manager"] != "rpm":
        raise ValueError("Recovery collection supports RPM installations only")
    core = [item for item in data["family"]["packages"] if item["role"] == "core"]
    if not core:
        raise ValueError("No installed core packages in discovery")
    os.mkdir(destination, 0o700)
    for item in core:
        url = meta.https(locations(item, repositories))
        path = os.path.join(destination, nevra(item) + ".rpm")
        subprocess.check_call([
            "curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
            "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "300",
            "--max-filesize", "268435456", "--output", path, url,
        ])
        identity = meta.run(["rpm", "-qp", "--qf", FORMAT, path])
        expected = "\t".join([item["name"], "0" if item["epoch"] in ("0", "(none)", "") else item["epoch"],
                              item["version"], item["release"], item["architecture"]])
        fields = identity.stdout.strip().split("\t")
        if len(fields) == 5 and fields[1] == "(none)":
            fields[1] = "0"
        if identity.returncode or "\t".join(fields) != expected:
            raise ValueError("Downloaded RPM is not the installed package: " + nevra(item))
        signature = meta.run(["rpm", "--checksig", "--verbose", path])
        text = signature.stdout + signature.stderr
        if signature.returncode or re.search(r"NOKEY|NOTTRUSTED|NOT OK|BAD", text, re.I) or \
                not re.search(r"signature[^:\n]*:\s*OK\s*$", text, re.I | re.M):
            raise ValueError("Recovery RPM signature is not verified by the local trust database: " + nevra(item))
        print("verified " + nevra(item))


if __name__ == "__main__":
    try:
        collect(sys.argv[1], sys.argv[2], [name for name in sys.argv[3].split(",") if name] if len(sys.argv) > 3 else ())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("[shift8-php] recovery error: " + str(error), file=sys.stderr)
        sys.exit(7)
