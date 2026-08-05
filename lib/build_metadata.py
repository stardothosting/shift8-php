#!/usr/bin/env python
"""Validate explicitly reviewed build inputs; do not evaluate RPM macros here."""
from __future__ import print_function, unicode_literals

import glob
import hashlib
import json
import os
import re
import shutil
import sys

import metadata as meta


def pinned(item):
    path = item["path"]
    if not isinstance(path, meta.STRING) or not os.path.isabs(path) or any(c in path for c in "\t\r\n"):
        raise ValueError("Profile input paths must be absolute and single-line")
    if not meta.SHA256.match(item["sha256"]) or os.path.islink(path) or meta.digest(path) != item["sha256"]:
        raise ValueError("Reviewed input changed or is a symlink: " + path)
    return path


def profile(path, discovery, release):
    data = meta.load(path)
    if data.get("schema_version") != 1 or data.get("reviewed") is not True:
        raise ValueError("Build profile needs schema_version=1 and explicit operator review")
    if discovery["package_manager"] != "rpm":
        raise ValueError("Isolated builds currently support RPM only; Debian preparation remains available")
    platform = discovery["distribution"]
    if (
        platform["id"] != "centos" or platform["version"].split(".")[0] != "7"
        or discovery["family"]["source_name"] != "php" or release["branch"] != "8.2"
        or not all("remi" in item["release"] for item in discovery["family"]["packages"] if item["role"] == "core")
    ):
        raise ValueError("Mock builds are gated to standard Remi PHP 8.2 on CentOS 7; other combinations need separate validation")
    compatibility = discovery.get("compatibility", {})
    if not compatibility.get("complete") or data.get("compatibility_sha256") != compatibility.get("sha256"):
        raise ValueError("Build profile does not match a complete target compatibility fingerprint")
    if data.get("installed_source") != discovery["family"]["source_reference"] or data.get("target") != release["version"]:
        raise ValueError("Build profile must bind the exact original source and target release")
    if data.get("configuration_closure_reviewed") is not True:
        raise ValueError("Mock configuration includes, trust keys and repository settings require closure review")
    config = pinned(data["mock_config"])
    if not config.endswith(".cfg"):
        raise ValueError("mock_config must name a reviewed .cfg file, not an inferred root name")
    for item in data.get("trust_files", []):
        pinned(item)
    if data.get("signature_policy_reviewed") is not True:
        raise ValueError("Archive key identity, TLS and package signature verification must be explicitly reviewed in the mock configuration closure")
    if not data.get("trust_files"):
        raise ValueError("Pin the archive signing key/configuration trust files; an empty build trust closure is not accepted")
    compiler = data["compiler"]
    if not re.match(r"/[A-Za-z0-9_./+-]+\Z", compiler["cc"]) or not re.match(r"/[A-Za-z0-9_./+-]+\Z", compiler["cxx"]):
        raise ValueError("Compiler paths must be absolute paths inside the isolated root")
    if os.path.dirname(compiler["cc"]) != os.path.dirname(compiler["cxx"]):
        raise ValueError("C/C++ compilers must use the same scoped toolchain directory")
    if not isinstance(compiler["major"], int) or compiler["major"] < 1:
        raise ValueError("Compiler major version is required")
    jobs = data["jobs"]
    if not isinstance(jobs, int) or not 1 <= jobs <= 64:
        raise ValueError("jobs must be an explicit integer between 1 and 64")
    inputs = data["inputs"]
    if not isinstance(inputs, list) or not inputs or len(inputs) > 1000:
        raise ValueError("A complete, bounded set of reviewed final packaging inputs is required")
    names, specs = set(), []
    for item in inputs:
        pinned(item)
        name = meta.filename(item["name"])
        if name in names or name == "php-{}.tar.xz".format(release["version"]):
            raise ValueError("Duplicate input or attempted replacement of authenticated upstream archive: " + name)
        names.add(name)
        if item["role"] not in ("spec", "source", "patch"):
            raise ValueError("Input role must be spec, source or patch")
        if item["role"] == "spec":
            specs.append(name)
        if item["role"] == "patch" and not item.get("review"):
            raise ValueError("Every patch needs a semantic/security review note")
        if "original" in item:
            pinned(item["original"])
            if not item.get("review"):
                raise ValueError("Adapted patches/specs require an explicit review note")
    if len(specs) != 1:
        raise ValueError("Profile must contain exactly one reviewed final spec")
    return data


def stage(profile_path, directory):
    discovery = meta.load(os.path.join(directory, "discovery.json"))
    release = meta.load(os.path.join(directory, "release.json"))
    data = profile(profile_path, discovery, release)
    target = os.path.join(directory, "build-inputs")
    os.mkdir(target, 0o700)
    os.mkdir(os.path.join(target, "originals"), 0o700)
    shutil.copyfile(profile_path, os.path.join(target, "profile.json"))
    audit = []
    for item in data["inputs"]:
        shutil.copyfile(item["path"], os.path.join(target, item["name"]))
        record = dict(item)
        if "original" in item:
            shutil.copyfile(item["original"]["path"], os.path.join(target, "originals", item["name"]))
        audit.append(record)
    archive = "php-{}.tar.xz".format(release["version"])
    shutil.copyfile(os.path.join(directory, "sources", archive), os.path.join(target, archive))
    meta.dump(dict(
        profile_sha256=meta.digest(profile_path), mock_configuration=data["mock_config"],
        trust_files=data.get("trust_files", []), inputs=audit,
        upstream=release["source"], compiler=data["compiler"],
        compatibility=discovery["compatibility"]["sha256"],
        authentication="Operator-reviewed local packaging, not vendor-authenticated build output",
    ), os.path.join(target, "audit.json"))
    return data


def validate_staged(directory):
    target = os.path.join(directory, "build-inputs")
    audit = meta.load(os.path.join(target, "audit.json"))
    path = os.path.join(target, "profile.json")
    if meta.digest(path) != audit["profile_sha256"]:
        raise ValueError("Build profile changed after review")
    data = profile(path, meta.load(os.path.join(directory, "discovery.json")), meta.load(os.path.join(directory, "release.json")))
    for item in data["inputs"]:
        if meta.digest(os.path.join(target, item["name"])) != item["sha256"]:
            raise ValueError("Staged final patch/spec/source changed: " + item["name"])
    release = meta.load(os.path.join(directory, "release.json"))
    if meta.digest(os.path.join(target, "php-{}.tar.xz".format(release["version"]))) != release["source"]["sha256"]:
        raise ValueError("Staged upstream source changed")
    return data


def spec_report(directory):
    logs = os.path.join(directory, "build-logs")
    parsed = meta.read(os.path.join(logs, "spec.stdout"))
    identity = meta.read(os.path.join(logs, "spec-identity.stdout")).strip().split("\t")
    discovery = meta.load(os.path.join(directory, "discovery.json"))
    release = meta.load(os.path.join(directory, "release.json"))
    if identity != [discovery["family"]["source_name"], release["version"]]:
        raise ValueError("RPM-expanded spec changes the package family or target version")
    prep = re.search(r"^%prep\s*\n(.*?)(?=^%(?:build|install|check|files|changelog)\b|\Z)", parsed, re.MULTILINE | re.DOTALL)
    if not prep:
        raise ValueError("RPM-expanded spec does not contain an inspectable %prep section")
    requirements = sorted(set(meta.read(os.path.join(logs, "buildrequires.stdout")).splitlines()))
    if not requirements:
        raise ValueError("RPM tooling returned no build requirements")
    meta.dump(dict(
        parser="Native rpmspec inside the reviewed isolated root; no host macro execution",
        prep_script=prep.group(1), build_requires=requirements,
        policy="Preserve declared patch order; no automated patch rewrites, omissions or fuzz increases.",
    ), os.path.join(directory, "spec-report.json"))
    for requirement in requirements:
        if not requirement.strip() or any(character in requirement for character in "\t\r\n"):
            raise ValueError("Invalid build requirement")
        print(requirement)


def compiler_report(directory):
    data = validate_staged(directory)
    for role in ("cc", "cxx"):
        text = meta.read(os.path.join(directory, "build-logs", role + ".stdout")).strip()
        if not re.match(str(data["compiler"]["major"]) + r"(?:\.|\Z)", text):
            raise ValueError("Isolated {} compiler differs from the reviewed major version".format(role))


def failure(directory, phase, code):
    logs = os.path.join(directory, "build-logs")
    text = meta.read(os.path.join(logs, phase + ".stdout")) + meta.read(os.path.join(logs, phase + ".stderr"))
    classification = phase
    if phase == "compile":
        if re.search(r"Bad exit status.*\(%check\)", text):
            classification = "tests"
        elif re.search(r"Bad exit status.*\(%install\)|Installed \(but unpackaged\)|File not found:", text):
            classification = "rpm-packaging"
        else:
            classification = "compilation-or-spec-phase; inspect-log"
    meta.dump(dict(
        phase=phase, classification=classification, exit_code=int(code),
        changed_host_php=False, restarted_services=False, deployable=False,
        recovery="Preserve original patches and inspect rejects in the recorded mock root; revise/review the profile and start a new build.",
    ), os.path.join(directory, "failure.json"))
    print("[shift8-php] Failure classification: " + classification, file=sys.stderr)


def artifact_report(directory):
    discovery = meta.load(os.path.join(directory, "discovery.json"))
    release = meta.load(os.path.join(directory, "release.json"))
    paths = sorted(glob.glob(os.path.join(directory, "packages", "*.rpm")))
    expected = {item["name"]: item for item in discovery["family"]["packages"] if item["role"] == "core"}
    selected, other, sources = {}, [], []
    for path in paths:
        result = meta.run(["rpm", "-qp", "--qf", "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\t%{SOURCEPACKAGE}\n", path])
        if result.returncode:
            raise ValueError("Generated RPM metadata is unreadable: " + path)
        fields = result.stdout.strip().split("\t")
        if len(fields) != 6:
            raise ValueError("Malformed generated RPM metadata")
        name, version, revision, architecture, source, source_package = fields
        record = dict(path=os.path.relpath(path, directory), name=name, version=version, release=revision,
                      architecture=architecture, source=source, sha256=meta.digest(path), signature="local-unsigned")
        if source_package == "1":
            if name != discovery["family"]["source_name"] or version != release["version"]:
                raise ValueError("Regenerated source package has an unexpected family/version")
            sources.append(record)
        elif name in expected:
            if version != release["version"] or architecture != expected[name]["architecture"]:
                raise ValueError("Replacement RPM changes architecture or PHP version")
            if name in selected:
                raise ValueError("Duplicate replacement package: " + name)
            for query in ("--requires", "--provides", "--scripts", "--configfiles", "--dump"):
                details = meta.run(["rpm", "-qp", query, path])
                if details.returncode:
                    raise ValueError("Generated package validation failed: " + query)
                record[query[2:]] = details.stdout.splitlines()
            selected[name] = record
        else:
            other.append(record)
    if set(selected) != set(expected) or len(sources) != 1:
        raise ValueError("Build must contain exactly one final SRPM and replacements for every installed core package")
    if any(item["source"] != os.path.basename(sources[0]["path"]) for item in selected.values()):
        raise ValueError("Binary RPMs do not reference the final regenerated SRPM")
    expected_abi = {
        capability for item in expected.values() for capability in item["provides"]
        if re.search(r"php\((?:api|zend-abi)\)", capability)
    }
    actual_abi = {capability for item in selected.values() for capability in item["provides"]}
    if not expected_abi or not expected_abi.issubset(actual_abi):
        raise ValueError("Generated core RPMs do not preserve the installed PHP ABI Provides")
    for name, item in selected.items():
        expected_configs = {line.strip() for line in expected[name]["configuration_files"] if line.strip()}
        if not expected_configs.issubset(set(item["configfiles"])):
            raise ValueError("Generated RPM drops managed configuration paths: " + name)
        required_paths = {
            path for path in expected[name]["files"]
            if path == discovery["runtime"]["executable"] or path in discovery["family"]["fpm_binaries"]
            or path.endswith(".service") or path.startswith(discovery["runtime"]["extension_dir"].rstrip("/") + "/")
        }
        actual_paths = {line.split()[0] for line in item["dump"] if line.split()}
        if not required_paths.issubset(actual_paths):
            raise ValueError("Generated RPM changes runtime/module/service paths: " + name)
    meta.dump(dict(
        replacements=[selected[name] for name in sorted(selected)], not_selected=other,
        retained_extensions=[item["name"] for item in discovery["family"]["packages"] if item["role"] != "core"],
        source_package=sources[0], deployable=False,
        runtime_compatibility="not-validated; extension load/FPM/application checks remain mandatory",
    ), os.path.join(directory, "artifacts.json"))


def main(args):
    command, args = args[0], args[1:]
    if command == "validate":
        profile(args[0], meta.load(args[1]), meta.load(args[2]))
    elif command == "stage":
        stage(*args)
    elif command == "check-inputs":
        validate_staged(*args)
    elif command == "spec":
        spec_report(*args)
    elif command == "compiler":
        compiler_report(*args)
    elif command == "artifacts":
        artifact_report(*args)
    elif command == "failure":
        failure(*args)
    elif command == "input-id":
        directory = args[0]
        files = sorted(os.path.relpath(path, directory) for path in meta.descendants(directory) if os.path.isfile(path))
        values = [(path, meta.digest(os.path.join(directory, path))) for path in files]
        print(hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest())
    else:
        raise ValueError("Unknown build metadata operation: " + command)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("[shift8-php] build metadata error: " + str(error), file=sys.stderr)
        sys.exit(7)
