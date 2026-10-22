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
    supported = {
        ("php", "8.2"): "standard Remi PHP 8.2",
        ("php83-php", "8.3"): "parallel Remi php83-php 8.3",
    }
    family = (discovery["family"]["source_name"], release["branch"])
    if (
        platform["id"] != "centos" or platform["version"].split(".")[0] != "7"
        or family not in supported
        or not all("remi" in item["release"] for item in discovery["family"]["packages"] if item["role"] == "core")
    ):
        raise ValueError(
            "Mock builds are gated to CentOS 7 with supported Remi families (currently standard PHP 8.2 and parallel php83-php 8.3); other combinations need separate validation"
        )
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
        if name in names or name in ("profile.json", "audit.json", "originals") or name == "php-{}.tar.xz".format(release["version"]):
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


def packaging(profile_path, directory):
    data = profile(profile_path, meta.load(os.path.join(directory, "discovery.json")),
                   meta.load(os.path.join(directory, "release.json")))
    target = os.path.join(directory, "sources", "packaging")
    os.mkdir(target, 0o700)
    shutil.copyfile(profile_path, os.path.join(target, "profile.json"))
    for item in data["inputs"]:
        shutil.copyfile(item["path"], os.path.join(target, item["name"]))
    with open(os.path.join(directory, "sources", "packaging-trust"), "w") as stream:
        stream.write("operator-reviewed-build-profile\n")


def el7_profile(profile_path, directory, destination):
    discovery = meta.load(os.path.join(directory, "discovery.json"))
    release = meta.load(os.path.join(directory, "release.json"))
    data = profile(profile_path, discovery, release)
    if discovery["family"]["source_name"] != "php83-php" or release["branch"] != "8.3":
        raise ValueError("The EL7 compatibility recipe supports only parallel Remi PHP 8.3")
    spec = next(item for item in data["inputs"] if item["role"] == "spec")
    text = meta.read(spec["path"])
    replacements = [
        ("%bcond_with           iodbc", "%bcond_without        iodbc"),
        ("BuildRequires: pkgconfig(icu-i18n) >= 74\n"
         "BuildRequires: pkgconfig(icu-io)   >= 74\n"
         "BuildRequires: pkgconfig(icu-uc)   >= 74\n",
         "%if 0%{?rhel} == 7\n"
         "BuildRequires: pkgconfig(icu-i18n) >= 73\n"
         "BuildRequires: pkgconfig(icu-io)   >= 73\n"
         "BuildRequires: pkgconfig(icu-uc)   >= 73\n%else\n"
         "BuildRequires: pkgconfig(icu-i18n) >= 74\n"
         "BuildRequires: pkgconfig(icu-io)   >= 74\n"
         "BuildRequires: pkgconfig(icu-uc)   >= 74\n%endif\n"),
        ("Patch49: php-8.2.0-iodbc.patch\n",
         "Patch49: php-8.2.0-iodbc.patch\nPatch9999: shift8-php83-el7.patch\n"),
        ("%patch -P49 -p1 -b .iodbc\n",
         "%patch -P49 -p1 -b .iodbc\n%patch -P9999 -p1 -b .shift8-el7\n"),
    ]
    if re.search(r"(?m)^Patch9999:", text) or any(item["name"] == "shift8-php83-el7.patch" for item in data["inputs"]):
        raise ValueError("EL7 compatibility patch is already present or its name/number is in use")
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError("EL7 spec recipe does not match exactly once; review changed provider packaging: " + old.splitlines()[0])
        text = text.replace(old, new, 1)
    destination = os.path.abspath(destination)
    os.mkdir(destination, 0o700)
    originals = os.path.join(destination, "originals")
    os.mkdir(originals, 0o700)
    for item in data["inputs"]:
        target = os.path.join(destination, item["name"])
        shutil.copyfile(item["path"], target)
        if item is spec:
            original = os.path.join(originals, item["name"])
            shutil.copyfile(item["path"], original)
            item["original"] = dict(path=original, sha256=item["sha256"])
            with open(target, "w") as stream:
                stream.write(text)
            item["review"] = (
                "EL7 recipe: iODBC default (do not deploy ODBC replacement without application review); "
                "ICU 73 floor only on EL7; explicit zero-fuzz FFI/OpenSSL compatibility patch; "
                "vendor patches and tests retained. Built RPMs still require runtime validation."
            )
        elif "original" in item:
            original = os.path.join(originals, item["name"])
            shutil.copyfile(item["original"]["path"], original)
            item["original"]["path"] = original
        item.update(path=target, sha256=meta.digest(target))
    patch = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "patches", "php83-el7.patch")
    target = os.path.join(destination, "shift8-php83-el7.patch")
    shutil.copyfile(patch, target)
    data["inputs"].append(dict(
        name="shift8-php83-el7.patch", role="patch", path=target, sha256=meta.digest(target),
        review="Explicit strncmp lengths preserve ZEND_STRL semantics under EL7 fortify; "
               "OpenSSL <1.1 accessor borrows const ASN1 data, with no allocation/free or verification bypass.",
    ))
    path = os.path.join(destination, "profile.json")
    meta.dump(data, path)
    profile(path, discovery, release)
    print(path)


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
    identity_text = meta.read(os.path.join(logs, "spec-identity.stdout")).strip()
    identity_match = re.search(r"(?m)^([^\t\n]+)\t([^\t\n]+)$", identity_text)
    identity = list(identity_match.groups()) if identity_match else identity_text.split("\t")
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
        match = re.search(r"(?m)(?:^|\s)(\d+(?:\.\d+)+)\s*$", text)
        version = match.group(1) if match else text
        if not re.match(str(data["compiler"]["major"]) + r"(?:\.|\Z)", version):
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


def test_report(directory):
    logs = os.path.join(directory, "build-logs")
    summaries, failures = [], []
    for name in ("compile.stdout", "compile.stderr"):
        text = meta.read(os.path.join(logs, name))
        summaries.extend(int(value) for value in re.findall(r"(?m)^Tests failed\s*:\s*(\d+)", text))
        failures.extend(re.findall(r"(?m)^TEST FAILURE: (.+?) --\s*$", text))
        for match in re.finditer(r"(?m)^FAILED TEST SUMMARY\s*\n-+\s*\n(.*?)(?=^=+|\Z)", text, re.DOTALL):
            failures.extend(line.strip() for line in match.group(1).splitlines() if line.strip())
    status = "failed" if any(summaries) or failures else ("passed" if summaries else "not-reported")
    meta.dump(dict(status=status, failed_counts=summaries, failures=sorted(set(failures)),
                   deployable=False, policy="RPM build success is not evidence that PHP tests passed."),
              os.path.join(directory, "php-tests.json"))
    if status != "passed":
        print("[shift8-php] WARNING: PHP tests {}; inspect php-tests.json and native logs. "
              "Do not install these artifacts on production.".format(status), file=sys.stderr)


def transaction_report(directory, discovery_path):
    manifest_path = os.path.join(directory, "manifest.json")
    meta.verify_manifest(manifest_path)
    manifest = meta.load(manifest_path)
    if manifest["kind"] != "isolated-rpm-build":
        raise ValueError("Transaction testing requires an isolated RPM build manifest")
    actual = meta.load(discovery_path).get("compatibility", {})
    expected = manifest["discovery"].get("compatibility", {})
    if not actual.get("complete") or not expected.get("complete") or actual.get("sha256") != expected.get("sha256"):
        raise ValueError("Transaction-test host differs from the complete build compatibility fingerprint")
    paths = []
    listed = {item["path"] for item in manifest["files"]}
    for item in manifest["build"]["replacements"]:
        if item["path"] not in listed:
            raise ValueError("Replacement RPM is not covered by the manifest")
        path = os.path.abspath(os.path.join(directory, item["path"]))
        if meta.digest(path) != item["sha256"]:
            raise ValueError("Replacement RPM differs from its recorded digest")
        paths.append(path)
    if not paths:
        raise ValueError("No core replacement RPMs to test")
    command = ["rpm", "-Uvh", "--test"] + paths
    result = meta.run(command)
    report = dict(
        status="passed" if result.returncode == 0 else "failed",
        exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr,
        manifest_sha256=meta.digest(manifest_path), compatibility_sha256=actual["sha256"],
        command=command, changed_host_php=False, executed_scriptlets=False, deployable=False,
        limitations="Native RPM transaction test only; runtime, application, signatures and rollback are not certified.",
    )
    meta.dump(report, os.path.join(directory, "transaction-check.json"))
    if result.returncode:
        raise ValueError("Read-only RPM transaction test failed; inspect transaction-check.json")
    print("Native RPM --test passed; no packages installed or scriptlets run. Deployment remains disabled.")


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
        php_tests=meta.load(os.path.join(directory, "php-tests.json")) if os.path.exists(os.path.join(directory, "php-tests.json")) else dict(status="not-reported"),
    ), os.path.join(directory, "artifacts.json"))


def main(args):
    command, args = args[0], args[1:]
    if command == "validate":
        profile(args[0], meta.load(args[1]), meta.load(args[2]))
    elif command == "stage":
        stage(*args)
    elif command == "packaging":
        packaging(*args)
    elif command == "el7-profile":
        el7_profile(*args)
    elif command == "check-inputs":
        validate_staged(*args)
    elif command == "spec":
        spec_report(*args)
    elif command == "compiler":
        compiler_report(*args)
    elif command == "artifacts":
        artifact_report(*args)
    elif command == "tests":
        test_report(*args)
    elif command == "transaction":
        transaction_report(*args)
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
