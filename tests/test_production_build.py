import copy
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_milestones import ROOT, TemporaryTest, discovery, metadata


sys.path.insert(0, str(ROOT / "lib"))
SPEC = importlib.util.spec_from_file_location("build_metadata", str(ROOT / "lib/build_metadata.py"))
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class ProductionFixture(TemporaryTest):
    def snapshot(self, family_source="php", runtime_version="8.2.20", branch="8.2"):
        data = discovery()
        data.update(distribution=dict(id="centos", version="7.9.2009"))
        package_prefix = "php83-php" if family_source == "php83-php" else "php"
        cli_name = package_prefix + "-cli"
        extension_name = package_prefix + "-pecl-redis5"
        executable = "/opt/remi/php83/root/usr/bin/php" if family_source == "php83-php" else "/usr/bin/php"
        extension_dir = "/opt/remi/php83/root/usr/lib64/php/modules" if family_source == "php83-php" else "/usr/lib64/php/modules"
        data["runtime"].update(
            version=runtime_version, branch=branch,
            executable=executable, php_api="20220829", module_api="20220829",
            zend_extension_api="420220829", zts=False, extension_dir=extension_dir,
            loaded_extensions=["Core", "redis", "igbinary", "msgpack", "zip"],
            build={"Configure Command": "'/opt/rh/devtoolset-10/root/usr/bin/gcc'"},
        )
        data["family"].update(source_name=family_source, source_reference="{}-{}.src.rpm".format(family_source, runtime_version + "-1.el7.remi"))
        package = dict(
            name=cli_name, epoch="0", version=runtime_version, release="1.el7.remi", architecture="x86_64",
            source_reference=data["family"]["source_reference"], requires=["libssl.so.10()(64bit)"],
            provides=["php(api) = 20220829"], role="core", configuration_files=[], files=[],
        )
        extension = dict(package, name=extension_name, version="5.3.7", role="separate-extension")
        data["family"].update(packages=[package, extension], names=[cli_name, extension_name], fpm_binaries=[])
        data["runtime_libraries"] = dict(
            complete=True, diagnostics=[],
            libraries={"/usr/lib64/libssl.so.10": dict(package="openssl-libs\t1:1.0.2k-26.el7_9\tx86_64", sha256="e" * 64)},
        )
        data["compatibility"] = metadata.compatibility(data)
        return data

    def profile(self, data=None, target_version="8.2.35"):
        data = data or self.snapshot()
        spec_name = "php83.spec" if data["family"]["source_name"] == "php83-php" else "php82.spec"
        spec_family = data["family"]["source_name"]
        spec = self.write(spec_name, "Name: {}\nVersion: {}\n%prep\n%autosetup -p1\n%build\n".format(spec_family, target_version))
        patch = self.write("openssl-compat.patch", "--- a/ext/openssl/xp_ssl.c\n+++ b/ext/openssl/xp_ssl.c\n")
        original = self.write("original.patch", "original vendor patch\n")
        config = self.write("mock.cfg", "# operator-reviewed local configuration fixture\n")
        trust = self.write("archive-key", "reviewed archive key fixture\n")
        value = dict(
            schema_version=1, reviewed=True, configuration_closure_reviewed=True, signature_policy_reviewed=True,
            compatibility_sha256=data["compatibility"]["sha256"],
            installed_source=data["family"]["source_reference"], target=target_version,
            mock_config=dict(path=str(config), sha256=metadata.digest(config)),
            trust_files=[dict(path=str(trust), sha256=metadata.digest(trust))],
            compiler=dict(cc="/opt/rh/devtoolset-10/root/usr/bin/gcc",
                          cxx="/opt/rh/devtoolset-10/root/usr/bin/g++", major=10),
            jobs=1,
            inputs=[
                dict(name=spec_name, role="spec", path=str(spec), sha256=metadata.digest(spec)),
                dict(name="openssl-compat.patch", role="patch", path=str(patch), sha256=metadata.digest(patch),
                     original=dict(path=str(original), sha256=metadata.digest(original)),
                     review="Reviewed all five accessors, version gate and behavior; fixture does not supply a production patch"),
            ],
        )
        path = self.directory / "profile.json"
        metadata.dump(value, path)
        return path, value

    def prepare_build(self, data=None, target_version="8.2.35", branch="8.2"):
        data = data or self.snapshot()
        profile_path, profile = self.profile(data, target_version=target_version)
        directory = self.directory / "build"
        directory.mkdir()
        metadata.dump(data, directory / "discovery.json")
        metadata.dump(dict(
            version=target_version, branch=branch,
            source=dict(url="https://www.php.net/distributions/php-{}.tar.xz".format(target_version), sha256="a" * 64),
        ), directory / "release.json")
        (directory / "sources").mkdir()
        (directory / "sources/php-{}.tar.xz".format(target_version)).write_bytes(b"authenticated upstream fixture")
        release = metadata.load(directory / "release.json")
        release["source"]["sha256"] = metadata.digest(directory / "sources/php-{}.tar.xz".format(target_version))
        metadata.dump(release, directory / "release.json")
        return directory, profile_path, profile


class ProductionTests(ProductionFixture):
    def test_pinned_resume_rejects_symlinks_and_shared_files(self):
        path = self.directory / "partial"
        target = self.write("target", "source")
        path.symlink_to(target)
        result = metadata.run([sys.executable, str(ROOT / "lib/metadata.py"), "partial", str(path), "a" * 64])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Untrusted", result.stderr)

    def test_failure_classifies_phpt_and_packaging_separately(self):
        logs = self.directory / "build-logs"
        logs.mkdir()
        (logs / "compile.stdout").write_text("")
        for message, expected in (
            ("error: Bad exit status from /var/tmp/rpm-tmp (%check)", "tests"),
            ("error: Installed (but unpackaged) file(s) found", "rpm-packaging"),
            ("xp_ssl.c: error: ASN1_STRING_get0_data undeclared", "compilation-or-spec-phase; inspect-log"),
        ):
            (logs / "compile.stderr").write_text(message)
            build.failure(str(self.directory), "compile", "1")
            self.assertEqual(metadata.load(self.directory / "failure.json")["classification"], expected)

    def test_fingerprint_is_stable_across_service_state_and_free_resources(self):
        data = self.snapshot()
        expected = metadata.compatibility(data)
        data.update(services=[dict(path="/unavailable/service", state="inactive", details="ActiveState=inactive")])
        data["environment"] = dict(free_bytes=1234, memory_kib=12345)
        data["distribution"]["description"] = "another friendly name"
        first = metadata.compatibility(data)
        data["services"][0]["state"] = "active"
        self.assertEqual(first["sha256"], metadata.compatibility(data)["sha256"])
        self.assertNotEqual(expected["sha256"], first["sha256"])  # Service definition set matters.

    def test_fingerprint_includes_libraries_abi_extensions_and_paths(self):
        original = self.snapshot()
        changes = (
            lambda data: data["runtime_libraries"]["libraries"]["/usr/lib64/libssl.so.10"].update(package="different OpenSSL"),
            lambda data: data["runtime"].update(module_api="20230831"),
            lambda data: data["runtime"].update(zend_extension_api="420230831"),
            lambda data: data["runtime"]["loaded_extensions"].remove("redis"),
            lambda data: data["family"]["packages"][0]["requires"].append("new-capability"),
            lambda data: data["distribution"].update(version="8"),
            lambda data: data["runtime"].update(executable="/opt/remi/php82/root/usr/bin/php"),
        )
        for change in changes:
            data = copy.deepcopy(original)
            change(data)
            self.assertNotEqual(metadata.compatibility(data)["sha256"], original["compatibility"]["sha256"])

    def test_configuration_content_changes_invalidate_fingerprint(self):
        data = self.snapshot()
        config = self.write("php.ini", "memory_limit = 128M\n")
        data["family"]["packages"][0]["configuration_files"] = [str(config)]
        original = metadata.compatibility(data)["sha256"]
        config.write_text("memory_limit = 256M\n")
        self.assertNotEqual(original, metadata.compatibility(data)["sha256"])

    def test_profile_requires_exact_complete_compatibility_and_supported_family(self):
        data = self.snapshot()
        path, value = self.profile(data)
        release = dict(version="8.2.35", branch="8.2")
        self.assertEqual(build.profile(str(path), data, release)["compiler"]["major"], 10)
        for mutate in (
            lambda item: item.update(reviewed=False),
            lambda item: item.update(compatibility_sha256="b" * 64),
            lambda item: item.update(target="8.2.34"),
            lambda item: item.update(configuration_closure_reviewed=False),
        ):
            invalid = copy.deepcopy(value)
            mutate(invalid)
            metadata.dump(invalid, path)
            with self.assertRaises(ValueError):
                build.profile(str(path), data, release)
        metadata.dump(value, path)
        data["distribution"]["id"] = "almalinux"
        with self.assertRaisesRegex(ValueError, "gated"):
            build.profile(str(path), data, release)

    def test_parallel_remi_php83_profile_is_supported(self):
        data = self.snapshot(family_source="php83-php", runtime_version="8.3.8", branch="8.3")
        path, _ = self.profile(data, target_version="8.3.36")
        release = dict(version="8.3.36", branch="8.3")
        profile = build.profile(str(path), data, release)
        self.assertEqual(profile["target"], "8.3.36")

    def test_patch_context_changes_are_never_invented_or_silently_accepted(self):
        data = self.snapshot()
        path, value = self.profile(data)
        for name in ("php-8.1.0-systzdata-v24.patch", "php-8.1.0-phpinfo.patch"):
            invalid = copy.deepcopy(value)
            invalid["inputs"][1]["name"] = name
            invalid["inputs"][1]["review"] = ""
            metadata.dump(invalid, path)
            with self.assertRaisesRegex(ValueError, "review"):
                build.profile(str(path), data, dict(version="8.2.35", branch="8.2"))

    def test_changed_final_patch_invalidates_build_and_preserves_original(self):
        directory, path, profile = self.prepare_build()
        build.stage(str(path), str(directory))
        original = directory / "build-inputs/originals/openssl-compat.patch"
        self.assertEqual(original.read_text(), "original vendor patch\n")
        (directory / "build-inputs/openssl-compat.patch").write_text("unreviewed accessor substitution")
        with self.assertRaisesRegex(ValueError, "changed"):
            build.validate_staged(str(directory))

    def test_mock_config_and_trust_file_changes_stop_build(self):
        directory, path, profile = self.prepare_build()
        build.stage(str(path), str(directory))
        pathlib.Path(profile["mock_config"]["path"]).write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            build.validate_staged(str(directory))

    def test_compiler_version_checked_inside_isolated_root(self):
        directory, path, profile = self.prepare_build()
        build.stage(str(path), str(directory))
        (directory / "build-logs").mkdir()
        for name in ("cc", "cxx"):
            (directory / "build-logs" / (name + ".stdout")).write_text("warning from wrapper\n10.3.1\n")
        build.compiler_report(str(directory))
        (directory / "build-logs/cc.stdout").write_text("4.8.5\n")
        with self.assertRaisesRegex(ValueError, "compiler"):
            build.compiler_report(str(directory))

    def test_native_expanded_prep_retains_actual_patch_order(self):
        directory, _, _ = self.prepare_build()
        logs = directory / "build-logs"
        logs.mkdir()
        (logs / "spec.stdout").write_text("Name: php\n%prep\npatch --fuzz=0 -p1 < system-timezone.patch\npatch --fuzz=0 -p1 < phpinfo.patch\n%build\nmake\n")
        (logs / "spec-identity.stdout").write_text("php\t8.2.35\n")
        (logs / "buildrequires.stdout").write_text("pkgconfig(oniguruma)\npkgconfig(gdlib)\npkgconfig(icu-uc)\n")
        build.spec_report(str(directory))
        report = metadata.load(directory / "spec-report.json")
        self.assertLess(report["prep_script"].index("system-timezone"), report["prep_script"].index("phpinfo"))
        self.assertIn("pkgconfig(icu-uc)", report["build_requires"])

    def test_binary_selection_does_not_select_every_rpm_or_replace_pecl(self):
        directory, _, _ = self.prepare_build()
        (directory / "packages").mkdir()
        srpm = "php-8.2.35-1.el7.shift8.src.rpm"
        for name in ("php-cli.rpm", "php-devel.rpm", srpm):
            (directory / "packages" / name).write_text(name)

        def query(arguments):
            path = arguments[-1]
            if "--qf" in arguments:
                name = "php" if path.endswith(".src.rpm") else pathlib.Path(path).name[:-4]
                source_flag = "1" if path.endswith(".src.rpm") else "(none)"
                return metadata.Result(0, "\t".join([name, "8.2.35", "1.el7.shift8", "x86_64", srpm, source_flag]), "")
            return metadata.Result(0, "php(api) = 20220829\n", "")

        with mock.patch.object(build.meta, "run", side_effect=query):
            build.artifact_report(str(directory))
        artifacts = metadata.load(directory / "artifacts.json")
        self.assertEqual([item["name"] for item in artifacts["replacements"]], ["php-cli"])
        self.assertEqual([item["name"] for item in artifacts["not_selected"]], ["php-devel"])
        self.assertEqual(artifacts["retained_extensions"], ["php-pecl-redis5"])
        self.assertFalse(artifacts["deployable"])

    def test_missing_core_replacement_or_final_srpm_fails(self):
        directory, _, _ = self.prepare_build()
        (directory / "packages").mkdir()
        with self.assertRaisesRegex(ValueError, "every installed core"):
            build.artifact_report(str(directory))


FAKE_MOCK = r"""#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, time
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_BUILD"])
if "--help" in args:
    print("--init --uniqueext --resultdir --chroot --copyin --copyout --installdeps --clean")
    sys.exit(0)
with (root / "mock-calls").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
if os.environ.get("FAIL_PREP") and "-bp" in args:
    print("Hunk #1 FAILED at ext/date/config0.m4; rejects saved to config0.m4.rej", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAIL_DEPS") and "--installdeps" in args:
    print("gd-devel conflicts with gd3php-devel; libicu-devel conflicts with libicu73-devel; harfbuzz-devel reverse dependency", file=sys.stderr)
    sys.exit(1)
if os.environ.get("FAIL_COMPILE") and "-ba" in args:
    print("xp_ssl.c: error: incompatible OpenSSL accessor", file=sys.stderr)
    sys.exit(1)
if os.environ.get("BLOCK_COMPILE") and "-ba" in args:
    (root / "active-compile-pid").write_text(str(os.getpid()))
    time.sleep(60)
if "--version" in args:
    print("mock fixture version")
elif "--parse" in args:
    print("Name: php\n%prep\npatch --fuzz=0 -p1 < php-8.1.0-systzdata-v24.patch\n%build\nmake")
elif "rpmspec" in args and "--qf" in args:
    print("php\t8.2.35")
elif "--buildrequires" in args:
    print("pkgconfig(oniguruma)\npkgconfig(gdlib)\npkgconfig(icu-uc)")
elif "-dumpversion" in args:
    print("10.3.1")
elif "--copyout" in args:
    source = args[args.index("--copyout") + 1]
    destination = pathlib.Path(args[-1])
    if source.endswith("SRPMS"):
        name = "php-8.2.35-1.el7.shift8.src.rpm"
    else:
        name = "php-cli-8.2.35-1.el7.shift8.x86_64.rpm"
    (destination / name).write_text("generated fixture")
elif "-qa" in args:
    print("gcc\t0\t10.3.1\t1.el7\tx86_64")
"""

FAKE_RPM = r"""#!/usr/bin/env python3
import sys
args = sys.argv[1:]
if "--qf" in args:
    source = args[-1].endswith(".src.rpm")
    print("\t".join(["php" if source else "php-cli", "8.2.35", "1.el7.shift8", "x86_64",
                     "php-8.2.35-1.el7.shift8.src.rpm", "1" if source else "(none)"]))
else:
    print("php(api) = 20220829")
"""


class MockDriverTests(ProductionFixture):
    def driver_setup(self, **variables):
        directory, path, _ = self.prepare_build()
        work = self.directory / "work"
        work.mkdir()
        metadata.dump(dict(configured=[]), work / "repository-config.json")
        binaries = self.directory / "bin"
        binaries.mkdir()
        for name, content in (("mock", FAKE_MOCK), ("rpm", FAKE_RPM), ("sleep", "#!/bin/sh\n/bin/sleep 0.03\n")):
            script = binaries / name
            script.write_text(content)
            script.chmod(0o755)
        environment = dict(
            os.environ, PATH=str(binaries) + ":" + os.environ["PATH"],
            BASE_DIR=str(ROOT), BUILD_PROFILE=str(path), BUILD_DIR=str(directory),
            TEST_BUILD=str(directory), WORK=str(work), OFFLINE="0", LOG_FILE=str(directory / "preparation.log"),
            OPERATION="build", VERBOSE="0", **variables
        )
        command = 'set -Eeuo pipefail; source "$BASE_DIR/lib/common.sh"; source "$BASE_DIR/lib/build-rpm.sh"; source "$BASE_DIR/lib/repositories.sh"; trap cleanup EXIT; trap "exit 143" TERM; build_rpm'
        return directory, environment, command

    def driver(self, **variables):
        directory, environment, command = self.driver_setup(**variables)
        result = subprocess.run(["bash", "-c", command], env=environment, text=True, capture_output=True, timeout=90)
        return directory, result

    def test_mock_driver_runs_prep_before_compile_and_regenerates_final_source(self):
        directory, result = self.driver()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in (directory / "mock-calls").read_text().splitlines()]
        prep = next(index for index, call in enumerate(calls) if "-bp" in call)
        compile_step = next(index for index, call in enumerate(calls) if "-ba" in call)
        self.assertLess(prep, compile_step)
        self.assertTrue(any("-bs" in call for call in calls[compile_step + 1:]))
        self.assertIn("PATH=/opt/rh/devtoolset-10/root/usr/bin:/usr/bin:/bin", calls[compile_step])
        self.assertTrue(all(any(part.startswith("--uniqueext=shift8-") for part in call) for call in calls))
        self.assertFalse(metadata.load(directory / "artifacts.json")["deployable"])
        self.assertEqual((directory / "build-state").read_text().strip(), "static-build-complete-runtime-unverified")
        metadata.dump(dict(candidates=[]), directory / "repositories.json")
        (directory / "sources/packaging-trust").write_text("operator-reviewed-checksum-map")
        metadata.manifest(str(directory), "isolated-rpm-build")
        metadata.verify_manifest(str(directory / "manifest.json"))
        manifest = metadata.load(directory / "manifest.json")
        self.assertFalse(manifest["deployable"])
        self.assertEqual(len(manifest["build"]["replacements"]), 1)

    def test_failed_vendor_patch_stops_before_compilation(self):
        directory, result = self.driver(FAIL_PREP="1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("Hunk #1 FAILED", result.stderr)
        calls = [json.loads(line) for line in (directory / "mock-calls").read_text().splitlines()]
        self.assertFalse(any("-ba" in call for call in calls))
        self.assertFalse((directory / "artifacts.json").exists())
        self.assertEqual((directory / "build-state").read_text().strip(), "failed:prep")

    def test_development_conflicts_are_isolated_and_explicit_not_forced(self):
        directory, result = self.driver(FAIL_DEPS="1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("harfbuzz-devel reverse dependency", result.stderr)
        self.assertEqual((directory / "build-state").read_text().strip(), "failed:dependencies")
        calls = (directory / "mock-calls").read_text()
        self.assertNotIn("--nodeps", calls)
        self.assertNotIn("--force", calls)
        self.assertNotIn("swap", calls)

    def test_failed_compilation_never_produces_a_success_artifact_report(self):
        directory, result = self.driver(FAIL_COMPILE="1")
        self.assertEqual(result.returncode, 7)
        self.assertIn("incompatible OpenSSL accessor", result.stderr)
        self.assertEqual(metadata.load(directory / "failure.json")["phase"], "compile")
        self.assertFalse((directory / "artifacts.json").exists())

    def test_interrupt_terminates_only_the_active_build_process_group(self):
        directory, environment, command = self.driver_setup(BLOCK_COMPILE="1")
        process = subprocess.Popen(["bash", "-c", command], env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        marker = directory / "active-compile-pid"
        try:
            deadline = time.monotonic() + 20
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists(), "Build never entered the blocking compilation fixture")
            child = int(marker.read_text())
            process.send_signal(signal.SIGTERM)
            _, errors = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 143, errors)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertFalse((directory / "artifacts.json").exists())
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
