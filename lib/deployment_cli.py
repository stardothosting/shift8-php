#!/usr/bin/env python
"""Explicit policy entry point for the local managed RPM transaction."""
from __future__ import print_function, unicode_literals

import os
import sys

import metadata as meta
import deployment


def main(command, package, manifest, discovery, policy_path, apply):
    if os.path.islink(policy_path):
        raise ValueError("Deployment policy cannot be a symlink")
    policy = meta.load(policy_path)
    if policy.get("schema_version") != 1 or policy.get("reviewed") is not True:
        raise ValueError("Deployment policy requires schema_version=1 and explicit review")
    if policy.get("package") != package:
        raise ValueError("Deployment policy must bind the exact selected CLI package")
    if command == "deploy":
        if not policy["service"].get("listeners"):
            raise ValueError("Deployment policy must include explicit target FPM listeners")
        data = deployment.plan(
            manifest, discovery, policy["recovery_dir"], policy["state_dir"],
            policy["service"], policy["health_commands"], apply=apply,
            replacement_trust=policy.get("replacement_trust"),
        )
    elif command == "rollback":
        data = deployment.rollback(policy["state_dir"], apply=apply)
    else:
        raise ValueError("Unsupported managed transaction")
    meta.dump(data)


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6] == "1")
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("[shift8-php] managed transaction error: " + str(error), file=sys.stderr)
        sys.exit(7)
