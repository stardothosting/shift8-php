"""Mocked local deployment tests; never run RPM or control a real service."""
from __future__ import unicode_literals

import copy
import binascii
import ctypes
import errno
import io
import json
import os
import shutil
import stat
import sys
import unittest
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import deployment as deploy
import metadata as meta


class XattrLibrary(object):
    def __init__(self, attributes):
        self.attributes = attributes
        self.list_errno = None
        self.get_errno = None
        self.list_growth = 0
        self.get_growth = 0
        self.descriptors = []
        self.calls = []

    def response(self, value, buffer, size, error, growth):
        if error:
            ctypes.set_errno(error)
            return -1
        if buffer is None:
            return len(value)
        if getattr(self, growth):
            setattr(self, growth, getattr(self, growth) - 1)
            ctypes.set_errno(errno.ERANGE)
            return -1
        if size < len(value):
            ctypes.set_errno(errno.ERANGE)
            return -1
        ctypes.memmove(buffer, value, len(value))
        return len(value)

    def flistxattr(self, descriptor, buffer, size):
        self.descriptors.append(descriptor)
        self.calls.append("list")
        names = sorted(self.attributes)
        raw = b"\x00".join(names) + b"\x00" if names else b""
        return self.response(raw, buffer, size, self.list_errno, "list_growth")

    def fgetxattr(self, descriptor, name, buffer, size):
        self.descriptors.append(descriptor)
        self.calls.append(name)
        return self.response(self.attributes[name], buffer, size, self.get_errno, "get_growth")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = os.path.join(ROOT, ".deployment-tests-" + uuid.uuid4().hex)
        os.mkdir(self.directory, 0o700)
        self.addCleanup(shutil.rmtree, self.directory)
        self.patches = []
        self.commands = []
        self.installed = {}
        self.active = True
        self.health_calls = 0
        self.fail_health = set()
        self.fail_restart = 0
        self.fail_stop = 0
        self.fail_update = False
        self.fail_test = False
        self.interrupt_update = False
        self.break_rollback = False
        self.unrelated_change = False
        self.bad_service = False
        self.service_override = {}
        self.security = {}
        self.copy_loses_metadata = None
        self.fail_native = None
        self.pid_executable = None
        self.proc_links = {}
        self.proc_files = {}
        self.proc_descriptors = ["7", "8"]
        self.real_run = deploy._run
        self.real_root = deploy._root
        self.real_xattrs = deploy._xattrs
        self.patch(deploy, "_xattrs", self.mock_xattrs)
        original_stat = os.stat
        outer = set()
        ancestor = ROOT
        while True:
            outer.add(ancestor)
            if ancestor == "/":
                break
            ancestor = os.path.dirname(ancestor)
        def trusted_outer_stat(path, *arguments, **keywords):
            status = original_stat(path, *arguments, **keywords)
            if os.path.abspath(path) in outer:
                # The shared checkout is group-writable; model protected host ancestors.
                values = list(status)
                values[0] &= ~0o022
                return os.stat_result(values)
            return status
        self.patch(deploy.os, "stat", trusted_outer_stat)
        self.patch(deploy, "_run", self.command)
        self.patch(deploy, "_root", lambda: None)
        self.rpm = self.write("bin/rpm", "mock RPM", executable=True)
        self.systemctl = self.write("bin/systemctl", "mock systemctl", executable=True)
        self.probe = self.write("bin/application-health", "mock app probe", executable=True)
        self.php = self.write("bin/php", "mock PHP ELF", executable=True)
        self.fpm = self.write("sbin/php-fpm", "mock FPM ELF", executable=True)
        self.patch(deploy, "SERVICE_READY_TIMEOUT", 0)
        self.patch(deploy, "_proc_link", self.proc_link)
        self.patch(deploy, "_proc_read", lambda path: self.proc_files[path])
        self.patch(deploy, "_proc_fds", lambda pid: list(self.proc_descriptors))
        self.unit = self.write("systemd/php-fpm.service", "mock unit")
        self.lib = self.write("lib/libc.so", "mock libc")
        self.ini = self.write("etc/php.ini", "operator=original\n")
        self.fpmconf = self.write("etc/php-fpm.conf", "listen=original\n")
        self.security[self.ini] = dict(
            acl="user::rw-\nuser:1001:r--\ngroup::r--\nmask::r--\nother::---\n",
            xattrs=["security.selinux=0x73797374656d5f753a6f626a6563745f723a6574635f743a733000",
                    "system.posix_acl_access=0x0200000001000600",
                    "user.operator=0x7265766965776564"],
        )
        original_rename = os.rename
        def rename_with_security(source, destination):
            result = original_rename(source, destination)
            if source in self.security:
                self.security[destination] = self.security.pop(source)
            else:
                self.security.pop(destination, None)
            return result
        self.patch(deploy.os, "rename", rename_with_security)
        os.chmod(self.ini, 0o640)
        os.utime(self.ini, (1000000000, 1000000001))
        self.ini_stat = os.stat(self.ini)
        self.patch(deploy, "LOCK_PATH", os.path.join(self.directory, "host.lock"))
        original_which = meta.which
        self.tools = {name: self.write("tools/" + name, "mock " + name, executable=True)
                      for name in ("cp", "stat", "getfacl", "touch")}
        self.tools["rpm"] = self.rpm
        self.patch(meta, "which", lambda name: self.tools.get(name) or original_which(name))
        self.patch(meta, "distribution", lambda: dict(id="centos", version="7.9"))
        self.info_text = (
            "PHP Version => 8.2.20\nPHP API => 20220829\nPHP Extension => 20220829\n"
            "Zend Extension => 420220829\nThread Safety => disabled\n"
            "extension_dir => {0}/modules => {0}/modules\n".format(self.directory)
        )
        core = []
        for name, files, configs in (
            ("php-cli", [self.php], []),
            ("php-common", [self.ini], [self.ini]),
            ("php-fpm", [self.fpm, self.unit, self.fpmconf], [self.fpmconf]),
        ):
            item = self.identity(name)
            self.installed[deploy._key(item)] = copy.deepcopy(item)
            item.update(role="core", files=files, configuration_files=configs, requires=[], provides=[])
            core.append(item)
        extension = self.identity("php-pecl-redis", source="php-pecl-redis-6.0.0-1.src.rpm")
        self.installed[deploy._key(extension)] = copy.deepcopy(extension)
        extension.update(role="separate-extension", files=[], configuration_files=[], requires=[], provides=[])
        other = self.identity("php83-php-cli", version="8.3.8", source="php83-php-8.3.8-1.src.rpm")
        self.installed[deploy._key(other)] = other
        libc = self.identity("glibc", version="2.17", source="glibc-2.17-1.src.rpm")
        self.installed[deploy._key(libc)] = libc
        self.before = copy.deepcopy(self.installed)
        libc_owner = "\t".join(libc[key] for key in deploy.KEYS[:-1])
        self.discovery = dict(
            schema_version=1, package_manager="rpm", architecture=os.uname()[4],
            distribution=dict(id="centos", version="7.9"), environment_fingerprint="a" * 64,
            family=dict(source_name="php", fpm_binaries=[self.fpm], packages=core + [extension]),
            runtime=dict(
                executable=self.php, owner="php-cli", version="8.2.20", branch="8.2",
                php_api="20220829", module_api="20220829", zend_extension_api="420220829",
                zts=False, extension_dir=self.directory + "/modules",
                loaded_extensions=["Core", "json"], build=meta.phpinfo(self.info_text),
            ),
            runtime_libraries=dict(complete=True, diagnostics=[], libraries={
                self.lib: dict(package=libc_owner, sha256=meta.digest(self.lib)),
            }),
            services=[dict(path=self.unit)],
        )
        self.discovery["compatibility"] = meta.compatibility(self.discovery)
        self.artifacts = os.path.join(self.directory, "artifacts")
        self.recovery = os.path.join(self.directory, "recovery")
        self.state = os.path.join(self.directory, "state")
        os.mkdir(self.artifacts, 0o700)
        os.mkdir(self.recovery, 0o700)
        replacements = []
        self.old_paths = []
        self.new_paths = []
        for package in core:
            old = deploy._nevra(package)
            new = self.identity(package["name"], version="8.2.35", release="1.el7.shift8",
                                source="php-8.2.35-1.el7.shift8.src.rpm")
            flags = {path: 17 if path in package["configuration_files"] else 0 for path in package["files"]}
            old_path = os.path.join(self.recovery, package["name"] + ".rpm")
            self.json(old_path, dict(identity=old, signed=True, flags=flags))
            relative = "packages/" + package["name"] + ".rpm"
            path = os.path.join(self.artifacts, relative)
            self.json(path, dict(identity=new, signed=False, flags=flags))
            self.old_paths.append(old_path)
            self.new_paths.append(path)
            record = dict(new, path=relative, sha256=meta.digest(path))
            replacements.append(record)
        self.json(os.path.join(self.artifacts, "discovery.json"), self.discovery)
        self.json(os.path.join(self.artifacts, "release.json"), dict(version="8.2.35", branch="8.2"))
        self.json(os.path.join(self.artifacts, "repositories.json"), {})
        report = dict(status="passed", passed_counts=[100], failed_counts=[0], failures=[])
        self.json(os.path.join(self.artifacts, "php-tests.json"), report)
        self.json(os.path.join(self.artifacts, "artifacts.json"), dict(replacements=replacements, php_tests=report))
        self.write("artifacts/sources/packaging-trust", "operator-reviewed-build-profile\n")
        self.rebuild_manifest()
        self.fresh = os.path.join(self.directory, "fresh.json")
        self.json(self.fresh, self.discovery)
        self.service = dict(name="php-fpm.service", executable=self.fpm,
                            stop=[self.systemctl, "stop", "php-fpm.service"],
                            restart=[self.systemctl, "restart", "php-fpm.service"])
        self.health = [[self.probe, "--application", "example.test"]]

    def tearDown(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)

    def patch(self, owner, name, value):
        self.patches.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def write(self, relative, content, executable=False):
        path = os.path.join(self.directory, relative)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        with io.open(path, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(path, 0o700 if executable else 0o600)
        return path

    def json(self, path, data):
        return self.write(os.path.relpath(path, self.directory), json.dumps(data))

    def identity(self, name, version="8.2.20", release="1.el7.remi", source=None):
        return dict(name=name, epoch="0", version=version, release=release,
                    architecture=os.uname()[4],
                    source_reference=source or "php-8.2.20-1.el7.remi.src.rpm")

    def rebuild_manifest(self):
        meta.manifest(self.artifacts, "isolated-rpm-build")
        self.manifest = os.path.join(self.artifacts, "manifest.json")
        self.trust = dict(reviewed=True, allow_unsigned=True, manifest_sha256=meta.digest(self.manifest),
                          reason="Operator-reviewed local test build; not vendor signature authentication")

    def call(self, apply=False):
        return deploy.plan(self.manifest, self.fresh, self.recovery, self.state,
                           self.service, self.health, apply=apply, replacement_trust=self.trust)

    def journal(self):
        return meta.load(os.path.join(self.state, "journal.json"))

    def result(self, stdout="", failed=False):
        return dict(returncode=1 if failed else 0, stdout=stdout, stderr="mock failure" if failed else "")

    def command(self, argv, timeout=60):
        self.commands.append(list(argv))
        if argv[0] in [path for name, path in self.tools.items() if name != "rpm"]:
            name = os.path.basename(argv[0])
            if name == self.fail_native:
                return self.result(failed=True)
            if name == "stat":
                status = os.lstat(argv[-1])
                return self.result("{}\t{}\t{}\t{}\t{}\t{}\n".format(
                    status.st_mode, status.st_uid, status.st_gid, status.st_size,
                    status.st_atime, status.st_mtime))
            if name == "getfacl":
                return self.result(self.security.get(argv[-1], {}).get("acl", "user::rw-\ngroup::---\nother::---\n"))
            if name == "cp":
                self.assertEqual(argv[1:-2], ["--preserve=all", "--no-dereference", "--"])
                source, destination = argv[-2:]
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(destination)).st_mode), 0o700)
                before = os.stat(source)
                shutil.copy2(source, destination)
                os.utime(destination, (before.st_atime, before.st_mtime))
                self.security[destination] = copy.deepcopy(self.security.get(source, {}))
                if self.copy_loses_metadata == "xattrs":
                    self.security[destination]["xattrs"] = []
                elif self.copy_loses_metadata == "acl":
                    self.security[destination]["acl"] = "user::rw-\ngroup::---\nother::---\n"
                elif self.copy_loses_metadata == "mode":
                    os.chmod(destination, 0o600)
                return self.result()
            if name == "touch":
                status = os.stat(argv[-1])
                self.assertIn(argv[2], ("-a", "-m"), "coreutils 8.22 only accepts -a/-m here")
                value = float(argv[3].split("=@", 1)[1])
                os.utime(argv[-1], (value, status.st_mtime) if argv[2] == "-a"
                         else (status.st_atime, value))
                return self.result()
            self.fail("Unexpected native tool " + name)
        if argv[0] == self.rpm:
            if argv[1] == "-qa":
                return self.result("\n".join("\t".join(item[key] for key in deploy.KEYS)
                                             for _, item in sorted(self.installed.items())) + "\n")
            if argv[1] == "-qp":
                package = meta.load(argv[-1])
                if argv[3] == deploy.RPM_QUERY:
                    return self.result("\t".join(package["identity"][key] for key in deploy.KEYS) + "\n")
                return self.result("\n".join("{}\t{}".format(path, flags)
                                             for path, flags in sorted(package["flags"].items())) + "\n")
            if argv[1] == "--checksig":
                signed = meta.load(argv[-1])["signed"]
                return self.result("Header V4 RSA/SHA256 Signature, key ID deadbeef: OK\n"
                                   if signed else "Header SHA1 digest: OK (4fa779866e750dab1000d31f6cdf8414ab23c10"
                                   "7)\n    MD5 digest: OK (2b54377c8ac82cbaddd4274ef09896a6)\n")
            if argv[1] == "-qf":
                if argv[-1] == self.fpm:
                    return self.result("php-fpm\n")
                if argv[-1] == self.php:
                    return self.result("php-cli\n")
                self.assertEqual(argv[-1], self.lib)
                return self.result("\t".join(self.before["glibc." + os.uname()[4]][key]
                                             for key in deploy.KEYS[:-1]) + "\n")
            if argv[1] == "-Uvh":
                paths = [value for value in argv[2:] if not value.startswith("--")]
                old = "--oldpackage" in argv
                if "--test" in argv:
                    return self.result(failed=self.fail_test or (old and self.break_rollback))
                if old and self.break_rollback:
                    return self.result(failed=True)
                for index, path in enumerate(paths):
                    item = meta.load(path)["identity"]
                    self.installed[deploy._key(item)] = item
                    if not old and (self.fail_update or self.interrupt_update) and index == 0:
                        self.write("etc/php.ini", "partial update\n")
                        self.security.pop(self.ini, None)
                        if self.interrupt_update:
                            raise KeyboardInterrupt("simulated interrupt")
                        return self.result(failed=True)
                if not old:
                    self.write("etc/php.ini", "RPM changed packaged default\n")
                    self.security.pop(self.ini, None)
                    self.write("etc/php.ini.rpmnew", "new RPM default\n")
                    if self.unrelated_change:
                        self.installed["unexpected.noarch"] = self.identity("unexpected")
                return self.result()
        if argv[0] == self.systemctl:
            if argv[1] == "show":
                values = dict(Id="other.service" if self.bad_service else "php-fpm.service",
                              Names="php-fpm.service", FragmentPath=self.unit,
                              ExecStart="{ path=" + self.fpm + " ; argv[]=" + self.fpm + "; }",
                              ActiveState="active" if self.active else "inactive",
                              MainPID="4321" if self.active else "0")
                values.update(self.service_override)
                return self.result(
                    "\n".join("{}={}".format(key, value) for key, value in sorted(values.items())) + "\n")
            self.assertEqual(argv[2:], ["php-fpm.service"])
            if argv[1] == "stop":
                if self.fail_stop:
                    self.fail_stop -= 1
                    return self.result(failed=True)
                self.active = False
                return self.result()
            self.assertEqual(argv[1], "restart")
            if self.fail_restart:
                self.fail_restart -= 1
                return self.result(failed=True)
            self.active = True
            return self.result()
        if argv[0] == self.php:
            return self.result(self.info_text if argv[1] == "-i" else "[PHP Modules]\nCore\njson\n")
        if argv[0] == self.probe:
            self.health_calls += 1
            return self.result("application healthy", failed=self.health_calls in self.fail_health)
        self.fail("Unexpected command: " + repr(argv))

    def proc_link(self, path):
        if path == "/proc/4321/exe":
            return self.pid_executable or self.fpm
        return self.proc_links[path]

    def encoded_xattrs(self, attributes):
        result = []
        for item in attributes:
            name, value = item.split("=0x", 1)
            result.append("0x{}=0x{}".format(binascii.hexlify(name.encode("utf-8")).decode("ascii"), value))
        return sorted(result)

    def mock_xattrs(self, path):
        return self.encoded_xattrs(self.security.get(path, {}).get("xattrs", []))

    def test_default_dry_run_is_read_only(self):
        result = self.call()
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["changed"])
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(deploy.LOCK_PATH))
        self.assertEqual(self.installed, self.before)
        self.assertEqual(self.health_calls, 0)
        self.assertFalse(any("-Uvh" in argv or argv[1] in ("stop", "restart") for argv in self.commands))

    def test_apply_and_explicit_rollback_restore_exact_config_metadata(self):
        result = self.call(apply=True)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.journal()["postactual"], self.installed)
        self.assertEqual(meta.read(self.ini), "operator=original\n")
        self.assertFalse(os.path.exists(self.ini + ".rpmnew"))
        self.assertEqual(stat.S_IMODE(os.stat(self.ini).st_mode), 0o640)
        self.assertEqual(os.stat(self.ini).st_uid, self.ini_stat.st_uid)
        self.assertEqual(os.stat(self.ini).st_gid, self.ini_stat.st_gid)
        self.assertEqual(os.stat(self.ini).st_mtime, self.ini_stat.st_mtime)
        before_commands = len(self.commands)
        self.assertEqual(deploy.rollback(self.state)["status"], "applied")
        self.assertFalse(any("-Uvh" in argv for argv in self.commands[before_commands:]))
        recovered = deploy.rollback(self.state, apply=True)
        self.assertEqual(recovered["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)
        self.assertTrue(self.active)
        self.assertEqual(self.health_calls, 3)
        updates = [argv for argv in self.commands if "-Uvh" in argv]
        self.assertEqual(len(updates), 4)
        self.assertIn("--test", updates[0])
        self.assertNotIn("--oldpackage", updates[1])
        self.assertIn("--test", updates[2])
        self.assertIn("--oldpackage", updates[2])
        self.assertIn("--oldpackage", updates[3])
        for argv in self.commands:
            self.assertFalse(set(argv) & {"--force", "--nodeps", "--nosignature", "--replacepkgs"})
        for path in meta.descendants(self.state):
            if os.path.isfile(path) and not path.startswith(os.path.join(self.state, "snapshots") + "/"):
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.state, "snapshots")).st_mode), 0o700)

    def test_health_failure_automatically_rolls_back(self):
        self.fail_health = {2}
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)
        self.assertEqual(meta.read(self.ini), "operator=original\n")
        self.assertEqual(self.health_calls, 3)
        self.assertEqual(self.journal()["failure_result"]["returncode"], 1)
        self.assertIn("postactual", self.journal())

    def test_restart_failure_automatically_rolls_back(self):
        self.fail_restart = 1
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)
        self.assertTrue(self.active)

    def test_stop_failure_recovers_without_reinstalling_unchanged_packages(self):
        self.fail_stop = 1
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertFalse(any("--oldpackage" in argv for argv in self.commands))
        self.assertEqual(self.installed, self.before)

    def test_partial_update_only_rolls_back_changed_original(self):
        self.fail_update = True
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        rollback_command = [argv for argv in self.commands if "--oldpackage" in argv and "--test" not in argv][0]
        self.assertEqual(len([value for value in rollback_command[2:] if not value.startswith("--")]), 1)
        self.assertEqual(self.installed, self.before)

    def test_keyboard_interrupt_is_recovered_and_recorded(self):
        self.interrupt_update = True
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertIn("KeyboardInterrupt", self.journal()["error"])
        self.assertEqual(self.installed, self.before)

    def test_failed_rollback_is_durable_and_can_be_resumed(self):
        self.fail_health = {2}
        self.break_rollback = True
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rollback_failed")
        self.assertIn("rollback_error", self.journal())
        self.break_rollback = False
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)

    def test_interrupted_updating_journal_is_resumable_without_manifest_or_old_directory(self):
        self.call(apply=True)
        data = self.journal()
        deploy._journal(self.state, data, "updating")
        shutil.rmtree(self.artifacts)
        shutil.rmtree(self.recovery)
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.installed, self.before)

    def test_unrelated_packages_and_parallel_service_are_never_targeted(self):
        self.call(apply=True)
        for key in self.before:
            if key.startswith(("php83-", "php-pecl-", "glibc.")):
                self.assertEqual(self.installed[key], self.before[key])
        for argv in self.commands:
            if argv[0] == self.systemctl:
                self.assertEqual(argv[-1], "php-fpm.service")
            if "-Uvh" in argv:
                for path in [value for value in argv[2:] if not value.startswith("--")]:
                    self.assertTrue(meta.load(path)["identity"]["name"] in ("php-cli", "php-common", "php-fpm"))

    def test_unrelated_scriptlet_mutation_is_not_reported_as_success(self):
        self.unrelated_change = True
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rollback_failed")
        self.assertIn("unrelated packages", self.journal()["rollback_error"])

    def test_missing_recovery_rpm_refuses_before_state_or_service_changes(self):
        os.unlink(self.old_paths[0])
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.installed, self.before)
        self.assertTrue(self.active)

    def test_unsigned_or_wrong_nevra_recovery_is_rejected(self):
        path = self.old_paths[0]
        record = meta.load(path)
        record["signed"] = False
        self.json(path, record)
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        record["signed"] = True
        record["identity"]["epoch"] = "1"
        self.json(path, record)
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_manifest_and_artifact_tampering_is_rejected(self):
        with io.open(self.new_paths[0], "a", encoding="utf-8") as stream:
            stream.write("tampered")
        with self.assertRaises(ValueError):
            self.call()

    def test_recovery_and_snapshot_tampering_is_rejected_before_rollback(self):
        self.call(apply=True)
        record = next(iter(self.journal()["recovery"].values()))
        self.write(os.path.relpath(record["path"], self.directory), "tampered")
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "applied")

    def test_snapshot_tampering_is_rejected(self):
        self.call(apply=True)
        record = next(record for record in self.journal()["snapshots"] if record["exists"])
        self.write(os.path.relpath(record["snapshot"], self.directory), "tampered")
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)

    def test_mismatched_family_and_host_fingerprints_are_rejected(self):
        data = copy.deepcopy(self.discovery)
        data["family"]["source_name"] = "php83-php"
        self.json(self.fresh, data)
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        data = copy.deepcopy(self.discovery)
        data["compatibility"]["sha256"] = "b" * 64
        self.json(self.fresh, data)
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_stale_discovery_and_local_host_mismatch_are_rejected(self):
        self.write("etc/php.ini", "changed since discovery")
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.write("etc/php.ini", "operator=original\n")
        self.patch(meta, "distribution", lambda: dict(id="centos", version="8"))
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_live_family_inventory_drift_is_rejected(self):
        self.installed["php-cli." + os.uname()[4]]["version"] = "8.2.21"
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_unsafe_health_and_service_policies_are_rejected(self):
        self.health = []
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.health = ["curl https://application.test"]
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.health = [[self.php, "-v"]]
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.health = [[self.probe]]
        self.service["restart"][-1] = "php83-php-fpm.service"
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_service_execstart_and_unit_identity_must_be_unambiguous(self):
        self.bad_service = True
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_config_noreplace_and_complete_core_selection_are_required(self):
        path = self.new_paths[1]
        package = meta.load(path)
        package["flags"][self.ini] = 1
        self.json(path, package)
        artifacts_path = os.path.join(self.artifacts, "artifacts.json")
        artifacts = meta.load(artifacts_path)
        artifacts["replacements"][1]["sha256"] = meta.digest(path)
        self.json(artifacts_path, artifacts)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        artifacts["replacements"] = artifacts["replacements"][:1]
        self.json(artifacts_path, artifacts)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_preflight_failures_do_not_stop_service(self):
        self.fail_test = True
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "preflight_failed")
        self.assertTrue(self.active)
        self.assertFalse(any(argv[0] == self.systemctl and argv[1] == "stop" for argv in self.commands))
        self.assertEqual(self.installed, self.before)

    def test_unhealthy_original_is_not_updated(self):
        self.fail_health = {1}
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "preflight_failed")
        self.assertFalse(any("-Uvh" in argv for argv in self.commands))

    def test_existing_partial_state_cannot_be_overwritten(self):
        os.mkdir(self.state, 0o700)
        self.write("state/incomplete", "interrupted staging")
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(meta.read(os.path.join(self.state, "incomplete")), "interrupted staging")

    def test_host_lock_is_exclusive(self):
        with deploy._lock():
            with self.assertRaises(deploy.DeploymentError):
                self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))

    def test_root_and_explicit_boolean_are_required(self):
        self.patch(deploy.os, "geteuid", lambda: 12345)
        self.assertRaises(deploy.DeploymentError, self.real_root)
        self.patch(deploy, "_root", self.real_root)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply="yes")

    def test_symlinked_configuration_or_state_is_refused(self):
        os.unlink(self.ini)
        os.symlink(self.fpmconf, self.ini)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)

    def test_rollback_host_binding_and_state_privacy(self):
        self.call(apply=True)
        data = self.journal()
        data["host"]["machine"] = "different-machine"
        deploy._journal(self.state, data)
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)
        os.chmod(self.state, 0o755)
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state)

    def test_failed_atomic_rename_keeps_previous_journal(self):
        self.call(apply=True)
        original = meta.read(os.path.join(self.state, "journal.json"))
        def fail_rename(source, destination):
            raise OSError("simulated rename failure")
        self.patch(deploy.os, "rename", fail_rename)
        with self.assertRaises(OSError):
            deploy._journal(self.state, self.journal(), "checking")
        self.assertEqual(meta.read(os.path.join(self.state, "journal.json")), original)

    def test_bounded_runner_without_shell_and_without_temporary_files(self):
        result = self.real_run([sys.executable, "-c", "print('probe')"])
        self.assertEqual(result["stdout"].strip(), "probe")
        with self.assertRaises(deploy.DeploymentError):
            self.real_run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.02)
        with self.assertRaises(deploy.DeploymentError):
            self.real_run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 1100000)"])

    def test_service_alias_wrapper_wrong_fragment_and_inactive_state_are_rejected(self):
        for override in (
            dict(Names="php-fpm.service alias.service"),
            dict(ExecStart="{ path=/bin/sh ; argv[]=/bin/sh -c php-fpm; }"),
            dict(FragmentPath="/etc/systemd/system/other.service"),
            dict(ActiveState="inactive"),
        ):
            self.service_override = override
            with self.assertRaises(deploy.DeploymentError):
                self.call()

    def test_health_shell_and_symlink_to_php_selfcheck_are_rejected(self):
        shell = self.write("bin/sh", "mock shell", executable=True)
        self.health = [[shell, "-c", "anything"]]
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        alias = os.path.join(self.directory, "bin/probe-alias")
        os.symlink(self.php, alias)
        self.health = [[alias, "-v"]]
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_valid_manifest_cannot_select_a_separate_extension_or_other_family(self):
        path = self.new_paths[0]
        package = meta.load(path)
        package["identity"]["name"] = "php-pecl-redis"
        self.json(path, package)
        artifact_path = os.path.join(self.artifacts, "artifacts.json")
        artifacts = meta.load(artifact_path)
        artifacts["replacements"][0].update(package["identity"], sha256=meta.digest(path))
        self.json(artifact_path, artifacts)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_changed_family_or_dropped_runtime_in_actual_rpm_is_rejected(self):
        path = self.new_paths[0]
        package = meta.load(path)
        package["identity"]["source_reference"] = "php83-php-8.2.35-1.src.rpm"
        self.json(path, package)
        artifact_path = os.path.join(self.artifacts, "artifacts.json")
        artifacts = meta.load(artifact_path)
        artifacts["replacements"][0]["sha256"] = meta.digest(path)
        self.json(artifact_path, artifacts)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        package["identity"]["source_reference"] = "php-8.2.35-1.el7.shift8.src.rpm"
        package["flags"] = {}
        self.json(path, package)
        artifacts["replacements"][0]["sha256"] = meta.digest(path)
        self.json(artifact_path, artifacts)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_original_manifest_is_reverified_immediately_before_update(self):
        original_command = self.command
        def mutate_manifest(argv, timeout=60):
            result = original_command(argv, timeout)
            if argv[0] == self.rpm and "-Uvh" in argv and "--test" in argv:
                with io.open(self.manifest, "a", encoding="utf-8") as stream:
                    stream.write(" ")
            return result
        self.patch(deploy, "_run", mutate_manifest)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "preflight_failed")
        self.assertEqual(self.installed, self.before)
        self.assertTrue(self.active)

    def test_staged_artifacts_are_rehashed_before_service_stop(self):
        original_command = self.command
        def mutate_staged(argv, timeout=60):
            result = original_command(argv, timeout)
            if argv[0] == self.rpm and "-Uvh" in argv and "--test" in argv:
                record = next(iter(self.journal()["replacements"].values()))
                self.write(os.path.relpath(record["path"], self.directory), "tampered")
            return result
        self.patch(deploy, "_run", mutate_staged)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "preflight_failed")
        self.assertEqual(self.installed, self.before)
        self.assertTrue(self.active)

    def test_crash_leftover_atomic_file_does_not_block_recovery(self):
        self.call(apply=True)
        self.write("state/journal.json.shift8-interrupted", '{"incomplete":')
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)

    def test_config_sidecars_and_original_modes_survive_failed_deployment(self):
        self.write("etc/php.ini.rpmnew", "existing administrator rpmnew\n")
        self.write("etc/php.ini.rpmsave", "existing administrator rpmsave\n")
        self.fail_health = {2}
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(meta.read(self.ini + ".rpmnew"), "existing administrator rpmnew\n")
        self.assertEqual(meta.read(self.ini + ".rpmsave"), "existing administrator rpmsave\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.ini).st_mode), 0o640)
        self.assertEqual(os.stat(self.ini).st_mtime, self.ini_stat.st_mtime)

    def test_failed_original_health_after_rollback_is_not_success(self):
        self.fail_health = {2, 3}
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rollback_failed")
        self.assertEqual(self.installed, self.before)
        self.assertEqual(self.journal()["rollback_failure_result"]["returncode"], 1)

    def test_symlinked_state_and_hardlinked_snapshots_are_rejected(self):
        destination = self.write("state-target/placeholder", "")
        os.symlink(os.path.dirname(destination), self.state)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        os.unlink(self.state)
        self.call(apply=True)
        record = next(record for record in self.journal()["snapshots"] if record["exists"])
        os.link(record["snapshot"], os.path.join(self.directory, "snapshot-hardlink"))
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)

    def test_atomic_journal_fsyncs_data_and_directory_before_reporting_success(self):
        calls = []
        original_fsync = os.fsync
        def fsync(descriptor):
            calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return original_fsync(descriptor)
        self.patch(deploy.os, "fsync", fsync)
        self.call(apply=True)
        self.assertTrue(any(calls))
        self.assertTrue(any(not value for value in calls))
        self.assertEqual(calls[-2:], [False, True])

    def test_state_beneath_writable_parent_is_rejected(self):
        parent = os.path.join(self.directory, "writable-parent")
        os.mkdir(parent, 0o777)
        os.chmod(parent, 0o777)
        self.state = os.path.join(parent, "state")
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))

    def test_original_cli_package_runtime_drift_is_rejected(self):
        self.discovery["runtime"]["version"] = "8.2.21"
        self.discovery["compatibility"] = meta.compatibility(self.discovery)
        self.json(self.fresh, self.discovery)
        self.json(os.path.join(self.artifacts, "discovery.json"), self.discovery)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_missing_or_truncated_recovery_journal_is_clear_and_read_only(self):
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)
        self.assertFalse(os.path.exists(self.state))
        self.write("state/journal.json", '{"partial":')
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state)
        self.assertEqual(meta.read(os.path.join(self.state, "journal.json")), '{"partial":')

    def test_unrelated_install_only_kernel_versions_remain_unchanged(self):
        first = self.identity("kernel", version="3.10.0", release="100.el7",
                              source="kernel-3.10.0-100.el7.src.rpm")
        second = self.identity("kernel", version="3.10.0", release="200.el7",
                               source="kernel-3.10.0-200.el7.src.rpm")
        self.installed["kernel-older"] = first
        self.installed["kernel-newer"] = second
        result = self.call(apply=True)
        kernel_key = "kernel." + os.uname()[4]
        self.assertEqual(len(result["postactual"][kernel_key]), 2)
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.installed["kernel-older"], first)
        self.assertEqual(self.installed["kernel-newer"], second)

    def test_multiple_installed_versions_of_selected_family_are_refused(self):
        duplicate = self.identity("php-cli", version="8.2.19")
        self.installed["php-cli-duplicate"] = duplicate
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))

    def test_php_reports_require_passing_nonempty_suites_without_failures(self):
        invalid = [
            dict(status="failed", passed_counts=[50000], failed_counts=[1], failures=[]),
            dict(status="not-reported", passed_counts=[100], failed_counts=[0], failures=[]),
            dict(status="passed", passed_counts=[], failed_counts=[], failures=[]),
            dict(status="passed", passed_counts=[0], failed_counts=[0], failures=[]),
            dict(status="passed", passed_counts=[True], failed_counts=[0], failures=[]),
            dict(status="passed", passed_counts=[100], failed_counts=[0, 1], failures=[]),
            dict(status="passed", passed_counts=[100], failed_counts=[0], failures=["known vendor failure"]),
            dict(status="passed", passed_counts=[100], failed_counts=[1], failures=[]),
        ]
        path = os.path.join(self.artifacts, "artifacts.json")
        artifacts = meta.load(path)
        for report in invalid:
            artifacts["php_tests"] = report
            self.json(path, artifacts)
            self.json(os.path.join(self.artifacts, "php-tests.json"), report)
            self.rebuild_manifest()
            with self.assertRaises(deploy.DeploymentError):
                self.call(apply=True)
            self.assertFalse(os.path.exists(self.state))
        self.assertFalse(any("-Uvh" in argv for argv in self.commands))

    def test_php_report_must_match_checksum_covered_file(self):
        path = os.path.join(self.artifacts, "php-tests.json")
        report = meta.load(path)
        report["status"] = "failed"
        self.json(path, report)
        self.rebuild_manifest()
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertFalse(os.path.exists(self.state))

    def test_unsigned_replacements_are_never_implicitly_trusted(self):
        self.trust = None
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.rebuild_manifest()
        self.trust["reviewed"] = False
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.trust["reviewed"] = True
        self.trust["manifest_sha256"] = "f" * 64
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.rebuild_manifest()
        result = self.call()
        self.assertTrue(all(record["authentication"].startswith("reviewed-local-unsigned")
                            for record in result["replacements"].values()))
        self.assertEqual(result["replacement_trust"], self.trust)

    def test_signed_replacements_need_no_unsigned_policy(self):
        path = os.path.join(self.artifacts, "artifacts.json")
        artifacts = meta.load(path)
        for index, package_path in enumerate(self.new_paths):
            package = meta.load(package_path)
            package["signed"] = True
            self.json(package_path, package)
            artifacts["replacements"][index]["sha256"] = meta.digest(package_path)
        self.json(path, artifacts)
        self.rebuild_manifest()
        self.trust = None
        result = self.call()
        self.assertTrue(all(record["authentication"] == "signature-verified"
                            for record in result["replacements"].values()))

    def test_acl_xattr_and_selinux_metadata_survive_update_and_rollback(self):
        original = copy.deepcopy(self.security[self.ini])
        result = self.call(apply=True)
        self.assertEqual(self.security[self.ini], original)
        snapshot = next(record for record in result["snapshots"] if record["path"] == self.ini)
        self.assertEqual(self.security[snapshot["snapshot"]], original)
        self.assertEqual(snapshot["security_metadata"]["xattrs"], self.encoded_xattrs(original["xattrs"]))
        self.security[self.ini] = dict(acl="wrong ACL", xattrs=["security.selinux=0x00"])
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.security[self.ini], original)
        self.assertEqual(deploy._config_metadata(self.ini), snapshot["security_metadata"])
        self.assertEqual(os.stat(self.ini).st_uid, self.ini_stat.st_uid)
        self.assertEqual(os.stat(self.ini).st_gid, self.ini_stat.st_gid)
        self.assertEqual(stat.S_IMODE(os.stat(self.ini).st_mode), 0o640)

    def test_security_metadata_only_snapshot_tampering_is_refused(self):
        self.call(apply=True)
        snapshot = next(record for record in self.journal()["snapshots"] if record["path"] == self.ini)
        self.security[snapshot["snapshot"]]["xattrs"] = ["security.selinux=0x00"]
        before = list(self.commands)
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "applied")
        self.assertFalse(any("-Uvh" in argv for argv in self.commands[len(before):]))

    def test_native_copy_silent_metadata_loss_fails_before_changes(self):
        for missing in ("acl", "xattrs", "mode"):
            self.copy_loses_metadata = missing
            self.state = os.path.join(self.directory, "state-" + missing)
            with self.assertRaises(deploy.DeploymentError):
                self.call(apply=True)
            self.assertTrue(self.active)
            self.assertEqual(self.installed, self.before)
        self.assertFalse(any("-Uvh" in argv for argv in self.commands))

    def test_metadata_inspection_or_preservation_failure_is_explicit(self):
        self.fail_native = "getfacl"
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertTrue(self.active)
        self.assertFalse(any("-Uvh" in argv for argv in self.commands))

    def test_metadata_restore_failure_is_durable_and_resumable(self):
        self.call(apply=True)
        self.copy_loses_metadata = "xattrs"
        with self.assertRaises(deploy.DeploymentError):
            deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rollback_failed")
        self.copy_loses_metadata = None
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)

    def tcp_listeners(self):
        self.service["listeners"] = [dict(type="tcp", address="127.0.0.1", port=port) for port in (9004, 9005)]
        self.proc_links.update({"/proc/4321/fd/7": "socket:[101]", "/proc/4321/fd/8": "socket:[102]"})
        self.proc_files["/proc/4321/net/tcp"] = (
            "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
            "0: 0100007F:232C 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 101\n"
            "1: 0100007F:232D 00000000:0000 0A 00000000:00000000 00:00000000 00000000 0 0 102\n")

    def test_mainpid_must_be_actual_selected_fpm_not_only_configured_execstart(self):
        self.pid_executable = "/foreign/sbin/php-fpm"
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.health_calls, 0)
        self.assertFalse(os.path.exists(self.state))
        self.pid_executable = None
        self.service_override["MainPID"] = "0"
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_owned_ipv4_listeners_are_checked_before_application_probes(self):
        self.tcp_listeners()
        result = self.call(apply=True)
        ready = result["service_postactual"]["readiness"]
        self.assertEqual(ready["MainPID"], 4321)
        self.assertEqual(ready["executable"], self.fpm)
        self.assertEqual(ready["listeners"], self.service["listeners"])
        self.assertEqual(result["original_service"]["readiness"]["listeners"], self.service["listeners"])

    def test_foreign_owned_or_nonlistening_socket_is_not_a_ready_target(self):
        self.tcp_listeners()
        self.proc_links["/proc/4321/fd/7"] = "socket:[999]"
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.health_calls, 0)
        self.proc_links["/proc/4321/fd/7"] = "socket:[101]"
        self.proc_files["/proc/4321/net/tcp"] = self.proc_files["/proc/4321/net/tcp"].replace(" 0A ", " 01 ")
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.assertEqual(self.health_calls, 0)

    def test_owned_unix_listener_and_explicit_unsupported_ipv6(self):
        path = os.path.join(self.directory, "run/fpm.sock")
        self.service["listeners"] = [dict(type="unix", path=path)]
        self.proc_descriptors = ["7"]
        self.proc_links["/proc/4321/fd/7"] = "socket:[201]"
        self.proc_files["/proc/4321/net/unix"] = (
            "Num RefCount Protocol Flags Type St Inode Path\n"
            "0000: 00000002 00000000 00010000 0001 01 201 " + path + "\n")
        result = self.call()
        self.assertEqual(result["service"]["readiness"]["listeners"], self.service["listeners"])
        self.service["listeners"] = [dict(type="tcp", address="::1", port=9004)]
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.service["listeners"] = [dict(type="tcp6", address="::1", port=9004)]
        with self.assertRaises(deploy.DeploymentError):
            self.call()

    def test_failed_postrestart_foreign_pid_is_not_killed_and_recovery_can_resume(self):
        original = self.command
        restarts = [0]
        def wrong_first_restart(argv, timeout=60):
            result = original(argv, timeout)
            if argv[0] == self.systemctl and argv[1] == "restart":
                restarts[0] += 1
                self.pid_executable = "/foreign/fpm" if restarts[0] == 1 else None
            return result
        self.patch(deploy, "_run", wrong_first_restart)
        with self.assertRaises(deploy.DeploymentError):
            self.call(apply=True)
        self.assertEqual(self.journal()["status"], "rollback_failed")
        self.assertEqual(self.health_calls, 1)
        self.assertIn("MainPID", self.journal()["error"])
        self.assertEqual(len([argv for argv in self.commands
                              if argv[0] == self.systemctl and argv[1] == "stop"]), 1)
        self.assertEqual(restarts[0], 1)
        self.pid_executable = None
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertEqual(self.installed, self.before)
        self.assertEqual(self.health_calls, 2)

    def test_readiness_polls_with_a_bound_without_killing_foreign_processes(self):
        self.patch(deploy, "SERVICE_READY_TIMEOUT", 1)
        self.pid_executable = "/foreign/fpm"
        waits = []
        def wait_once(duration):
            waits.append(duration)
            self.pid_executable = None
        self.patch(deploy.time, "sleep", wait_once)
        result = self.call()
        self.assertTrue(waits)
        self.assertEqual(result["service"]["readiness"]["MainPID"], 4321)
        self.assertFalse(any(argv[0].endswith("kill") for argv in self.commands))

    def test_optional_cli_forwards_explicit_json_policy_and_defaults_to_dry_run(self):
        service = self.json(os.path.join(self.directory, "service.json"), self.service)
        health = self.json(os.path.join(self.directory, "health.json"), self.health)
        trust = self.json(os.path.join(self.directory, "trust.json"), self.trust)
        calls = []
        def planned(*arguments, **keywords):
            calls.append((arguments, keywords))
            return dict(status="planned")
        self.patch(deploy, "plan", planned)
        self.patch(deploy.sys if hasattr(deploy, "sys") else sys, "stdout", io.StringIO())
        result = deploy.main(["plan", "--manifest", self.manifest, "--fresh-discovery", self.fresh,
                              "--recovery-dir", self.recovery, "--state-dir", self.state,
                              "--service-policy", service, "--health-policy", health,
                              "--replacement-trust", trust])
        self.assertEqual(result, 0)
        self.assertEqual(calls[0][0][4:6], (self.service, self.health))
        self.assertIs(calls[0][1]["apply"], False)
        self.assertEqual(calls[0][1]["replacement_trust"], self.trust)

    def test_rpm_queries_pass_real_argv_validation_without_embedded_newlines(self):
        def validated(arguments, timeout=60):
            return self.command(deploy._argv(arguments), timeout)
        self.patch(deploy, "_run", validated)
        deploy._rpm("-qa", "--qf", deploy.RPM_QUERY)
        self.call()
        queries = [arguments[arguments.index("--qf") + 1] for arguments in self.commands if "--qf" in arguments]
        self.assertTrue(queries)
        self.assertIn(deploy.RPM_FILES_QUERY, queries)
        for query in queries:
            self.assertNotIn("\n", query)
            self.assertNotIn("\t", query)
            self.assertIn("\\n", query)
        self.assertIn("\\t", deploy.RPM_QUERY)
        with self.assertRaises(deploy.DeploymentError):
            deploy._rpm("-qa", "--qf", "%{NAME}\n")

    def test_metadata_preservation_does_not_require_getfattr_or_setfattr(self):
        which = meta.which
        self.patch(meta, "which", lambda name: None if name in ("getfattr", "setfattr") else which(name))
        result = self.call(apply=True)
        self.assertEqual(result["status"], "applied")
        deploy.rollback(self.state, apply=True)
        self.assertEqual(self.journal()["status"], "rolled_back")
        self.assertFalse(any(os.path.basename(arguments[0]) in ("getfattr", "setfattr")
                             for arguments in self.commands))

    def libc_xattrs(self, attributes):
        library = XattrLibrary(attributes)
        self.patch(deploy, "_xattr_api", lambda: (None, None))
        self.patch(deploy, "_xattr_libc", lambda: library)
        return library

    def test_python27_libc_xattrs_capture_exact_binary_names_and_values(self):
        attributes = {
            b"security.selinux": b"system_u:object_r:etc_t:s0\x00",
            b"system.posix_acl_access": b"\x02\x00\x00\x00\xff",
            b"user.binary\xff": b"\x00\xff\x00",
            b"user.empty": b"",
        }
        library = self.libc_xattrs(attributes)
        result = self.real_xattrs(self.ini)
        expected = sorted("0x{}=0x{}".format(binascii.hexlify(name).decode("ascii"),
                                            binascii.hexlify(value).decode("ascii"))
                          for name, value in attributes.items())
        self.assertEqual(result, expected)
        self.assertIn(b"security.selinux", library.calls)
        self.assertIn(b"system.posix_acl_access", library.calls)
        self.assertTrue(library.descriptors)
        with self.assertRaises(OSError):
            os.fstat(library.descriptors[-1])

    def test_os_xattr_api_and_libc_fallback_produce_identical_hex_records(self):
        attributes = {b"user.alpha": b"\x00\x01", b"security.selinux": b"context\x00"}
        self.libc_xattrs(attributes)
        fallback = self.real_xattrs(self.ini)
        self.patch(deploy, "_xattr_api",
                   lambda: (lambda descriptor: sorted(attributes),
                            lambda descriptor, name: attributes[name]))
        self.assertEqual(self.real_xattrs(self.ini), fallback)

    def test_empty_xattr_list_is_genuine_and_unsupported_filesystems_fail_explicitly(self):
        library = self.libc_xattrs({})
        self.assertEqual(self.real_xattrs(self.ini), [])
        for code in (errno.ENOSYS, getattr(errno, "ENOTSUP", 95), getattr(errno, "EOPNOTSUPP", 95)):
            library.list_errno = code
            with self.assertRaises(deploy.DeploymentError) as caught:
                self.real_xattrs(self.ini)
            self.assertIn("filesystem unsupported", str(caught.exception))
        library.list_errno = errno.EACCES
        with self.assertRaises(deploy.DeploymentError) as caught:
            self.real_xattrs(self.ini)
        self.assertNotIn("filesystem unsupported", str(caught.exception))

    def test_libc_xattr_erange_retries_are_bounded_and_get_errors_are_not_dropped(self):
        library = self.libc_xattrs({b"user.value": b"original"})
        library.list_growth = 1
        library.get_growth = 1
        self.assertTrue(self.real_xattrs(self.ini))
        library.list_growth = 3
        with self.assertRaises(deploy.DeploymentError) as caught:
            self.real_xattrs(self.ini)
        self.assertIn("changed repeatedly", str(caught.exception))
        library.list_growth = 0
        library.get_errno = errno.EACCES
        with self.assertRaises(deploy.DeploymentError):
            self.real_xattrs(self.ini)
        library.get_errno = getattr(errno, "ENODATA", 61)
        with self.assertRaises(deploy.DeploymentError):
            self.real_xattrs(self.ini)

    def test_xattr_size_limits_and_symlink_safety(self):
        library = self.libc_xattrs({b"user.too-large": b"x" * (deploy.MAX_XATTR_VALUE + 1)})
        with self.assertRaises(deploy.DeploymentError):
            self.real_xattrs(self.ini)
        library.attributes = {}
        link = os.path.join(self.directory, "config-symlink")
        os.symlink(self.ini, link)
        with self.assertRaises(deploy.DeploymentError):
            self.real_xattrs(link)


if __name__ == "__main__":
    unittest.main()
