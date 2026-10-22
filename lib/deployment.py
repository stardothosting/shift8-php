"""Opt-in, local RPM deployment; not a CLI or a production certification.

plan(..., apply=False) performs read-only inspection and returns a proposal.
service is an explicit dict with name, executable, stop and restart argv. Only
direct systemctl control of one package-owned FPM unit is supported. Health
commands are operator-reviewed external application probes, not PHP self-tests.
The actual MainPID executable must match that FPM. Optional service["listeners"]
contains {"type": "tcp", "address": "127.0.0.1", "port": 9004} or
{"type": "unix", "path": "/absolute/fpm.sock"} objects. Only master-owned,
listening IPv4/Unix sockets qualify; IPv6 is explicitly unsupported.
Service control is refused if its current MainPID is foreign or unverifiable;
such a recovery remains rollback_failed rather than killing an unrelated PID.
rollback(state_dir, apply=False) inspects an existing journal; apply=True resumes
recovery after an interrupted transaction or reverts a completed deployment.

Use a trusted, already reviewed isolated-rpm-build manifest. Its checksums are
integrity evidence, not authentication. RPM scriptlets and application probes
are trusted executable code. Symlinked configurations, non-systemd services,
unbounded package/configuration sets, and automatic dependency changes are
deliberately unsupported. GNU cp/stat/touch plus getfacl must be available
to verify exact configuration bytes, ownership, modes, timestamps, ACLs and all
xattrs (including SELinux labels). Xattrs use os APIs or descriptor-based libc
calls through ctypes on Python 2; getfattr/setfattr are not required. Attribute
names and values are recorded as exact hexadecimal bytes. Preservation failures
are fatal, including unsupported xattr filesystems. Inode/ctime
and external application data are not restored. Preserved payloads retain their
original metadata inside caller-owned mode-0700 snapshot/staging directories.
State/configuration ancestors must be owned by root or the caller and must not
be group/world-writable. Multiple installed versions are refused only for the
discovered PHP family; unrelated install-only packages are retained unchanged.
Replacement RPMs require verified signatures unless replacement_trust explicitly
allows reviewed local unsigned RPMs and binds the exact manifest SHA-256.
"""
from __future__ import unicode_literals

import contextlib
import binascii
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

import metadata as meta


LOCK_PATH = "/run/shift8-php-deployment.lock"
RPM_QUERY = r"%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCERPM}\n"
RPM_FILES_QUERY = r"[%{FILENAMES}\t%{FILEFLAGS}\n]"
KEYS = ("name", "epoch", "version", "release", "architecture", "source_reference")
MAX_PACKAGES = 128
MAX_CONFIG_BYTES = 64 * 1024 * 1024
MAX_RPM_BYTES = 256 * 1024 * 1024
SERVICE_READY_TIMEOUT = 10
MAX_XATTR_BYTES = 1024 * 1024
MAX_XATTR_NAMES = 1024
MAX_XATTR_VALUE = 65536
RECOVERABLE = ("prepared", "stopping", "updating", "restarting", "checking",
               "applied", "rolling_back", "rollback_failed")


class DeploymentError(ValueError):
    """A refused operation or a recorded deployment/recovery failure."""

    def __init__(self, message, result=None):
        ValueError.__init__(self, message)
        self.result = result


def _require(condition, message):
    if not condition:
        raise DeploymentError(message)


def _argv(arguments):
    _require(isinstance(arguments, (list, tuple)) and 0 < len(arguments) <= 128,
             "Commands must be bounded argv lists, never shell strings")
    _require(all(isinstance(arg, meta.STRING) and arg and not any(c in arg for c in "\x00\r\n")
                 for arg in arguments), "Invalid command argument")
    _require(os.path.isabs(arguments[0]) and os.path.isfile(arguments[0])
             and os.access(arguments[0], os.X_OK), "Command executable must be an existing absolute path")
    return list(arguments)


def _run(arguments, timeout=60):
    """Bound elapsed time and captured output, including inherited pipe holders."""
    arguments = _argv(arguments)
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, preexec_fn=os.setsid, close_fds=True)
    streams = {process.stdout.fileno(): [], process.stderr.fileno(): []}
    opened = list(streams)
    clock = getattr(time, "monotonic", time.time)
    deadline, size = clock() + timeout, 0
    try:
        while opened or process.poll() is None:
            _require(clock() < deadline, "Command timed out: " + arguments[0])
            ready = select.select(opened, [], [], min(0.1, max(0, deadline - clock())))[0]
            for descriptor in ready:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    opened.remove(descriptor)
                    continue
                size += len(chunk)
                _require(size <= 1024 * 1024, "Command output limit exceeded")
                streams[descriptor].append(chunk)
        return dict(returncode=process.wait(),
                    stdout=b"".join(streams[process.stdout.fileno()]).decode("utf-8", "replace"),
                    stderr=b"".join(streams[process.stderr.fileno()]).decode("utf-8", "replace"))
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def _checked(arguments, timeout=60):
    result = _run(arguments, timeout)
    if result["returncode"]:
        raise DeploymentError("Command failed: " + repr(arguments), result)
    return result


def _rpm(*arguments):
    executable = meta.which("rpm")
    _require(executable is not None, "rpm executable is required")
    return _checked([os.path.abspath(executable)] + list(arguments))


def _path(path):
    _require(isinstance(path, meta.STRING) and os.path.isabs(path)
             and os.path.normpath(path) == path and path != "/"
             and not any(c in path for c in "\x00\t\r\n"), "Unsafe absolute path")
    current = path
    while current != "/":
        _require(not os.path.islink(current), "Symlink path is unsupported: " + current)
        current = os.path.dirname(current)
    return path


def _trusted_parents(path):
    current = os.path.dirname(_path(path))
    while True:
        status = os.stat(current)
        _require(stat.S_ISDIR(status.st_mode) and status.st_uid in (0, os.geteuid())
                 and not status.st_mode & 0o022, "Writable/untrusted parent directory: " + current)
        if current == "/":
            return
        current = os.path.dirname(current)


def _identity(line):
    fields = line.split("\t")
    _require(len(fields) == len(KEYS), "Malformed RPM identity")
    fields[1] = "0" if fields[1] in ("(none)", "(null)") else fields[1]
    _require(all(fields) and meta.PACKAGE_NAME.match(fields[0]), "Invalid RPM identity")
    return dict(zip(KEYS, fields))


def _key(item):
    return item["name"] + "." + item["architecture"]


def _nevra(item):
    return {key: str(item[key]) for key in KEYS}


def _inventory():
    groups = {}
    for line in _rpm("-qa", "--qf", RPM_QUERY)["stdout"].splitlines():
        item = _identity(line)
        groups.setdefault(_key(item), []).append(item)
    _require(groups, "Empty RPM inventory")
    return {key: items[0] if len(items) == 1 else sorted(items, key=lambda item: tuple(item[field] for field in KEYS))
            for key, items in groups.items()}


def _package(path):
    _path(path)
    _require(os.path.isfile(path), "Missing local RPM: " + path)
    lines = _rpm("-qp", "--qf", RPM_QUERY, "--", path)["stdout"].splitlines()
    _require(len(lines) == 1, "Expected exactly one binary RPM identity")
    item = _identity(lines[0])
    _require(item["architecture"] not in ("src", "nosrc"), "Source RPM cannot be installed")
    return item


def _signature(path):
    output = _rpm("--checksig", "--verbose", "--", path)["stdout"]
    _require(not re.search(r"NOKEY|NOTTRUSTED|NOT OK|BAD|unsigned", output, re.I)
             and re.search(r"signature[^:\n]*:\s*OK\s*$", output, re.I | re.M),
             "RPM needs a signature verified by the local RPM trust database: " + path)


def _replacement_policy(policy, manifest_sha256):
    if policy is None:
        return None
    _require(isinstance(policy, dict) and policy.get("reviewed") is True
             and policy.get("allow_unsigned") is True
             and policy.get("manifest_sha256") == manifest_sha256
             and isinstance(policy.get("reason"), meta.STRING)
             and 0 < len(policy["reason"].strip()) <= 1000,
             "Unsigned replacement trust needs explicit review, reason and exact manifest SHA-256")
    return dict(reviewed=True, allow_unsigned=True, manifest_sha256=manifest_sha256,
                reason=policy["reason"])


def _replacement_authentication(path, policy):
    output = _rpm("--checksig", "--verbose", "--", path)["stdout"]
    _require(not re.search(r"NOKEY|NOTTRUSTED|NOT OK|BAD", output, re.I),
             "Replacement RPM integrity/signature verification failed")
    if re.search(r"signature[^:\n]*:\s*OK\s*$", output, re.I | re.M):
        return "signature-verified"
    _require(policy is not None and not re.search(r"signature", output, re.I)
             and re.search(r"digest[^:\n]*:\s*OK\s*$", output, re.I | re.M),
             "Unsigned replacement RPM requires an explicit reviewed local trust policy")
    return "reviewed-local-unsigned; checksums are not signature authentication"


def _php_tests(manifest, root):
    report = manifest["build"].get("php_tests", {})
    _require(isinstance(report, dict), "Malformed PHP test report")
    passed, failed = report.get("passed_counts"), report.get("failed_counts")
    _require(report.get("status") == "passed"
             and isinstance(passed, list) and 0 < len(passed) <= 128
             and all(type(value) is int and 0 < value <= 10000000 for value in passed)
             and isinstance(failed, list) and len(failed) == len(passed)
             and all(type(value) is int and value == 0 for value in failed)
             and report.get("failures") == [],
             "Deployment requires passing PHP suites with nonzero passed_counts and no failures")
    _require("php-tests.json" in {item["path"] for item in manifest["files"]}
             and meta.load(os.path.join(root, "php-tests.json")) == report,
             "PHP test report must match its checksum-covered manifest file")
    return report


def _file_flags(path):
    result = {}
    output = _rpm("-qp", "--qf", RPM_FILES_QUERY, "--", path)["stdout"]
    for line in output.splitlines():
        fields = line.split("\t")
        _require(len(fields) == 2 and fields[1].isdigit(), "Malformed RPM file flags")
        name, flags = fields[0], int(fields[1])
        _path(name)
        if flags & 1:
            _require(flags & 16, "Replacement configuration must be %config(noreplace): " + name)
        _require(name not in result, "Duplicate RPM file path")
        result[name] = flags
    return result


def _fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _fresh(discovery):
    _require(discovery.get("package_manager") == "rpm", "Deployment supports RPM only")
    platform = meta.distribution()
    _require(all(platform.get(key) == discovery["distribution"].get(key) for key in ("id", "version"))
             and os.uname()[4] == discovery["architecture"], "Fresh discovery does not match this host")
    expected = discovery.get("compatibility", {})
    actual = meta.compatibility(discovery)
    _require(expected.get("complete") and actual["complete"]
             and expected.get("sha256") == _fingerprint(expected["inputs"]) == actual["sha256"],
             "Fresh compatibility fingerprint is incomplete, stale or tampered")
    for path, item in expected["inputs"]["runtime_libraries"].items():
        _require(item and os.path.isfile(path) and meta.digest(path) == item["sha256"],
                 "Runtime library changed: " + path)
        owner = _rpm("-qf", "--qf", r"%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n",
                     "--", path)["stdout"].strip()
        _require(owner == item["package"], "Runtime library ownership changed: " + path)
    inventory = _inventory()
    for item in discovery["family"]["packages"]:
        _require(inventory.get(_key(item)) == _nevra(item), "Installed family changed: " + item["name"])
    runtime = discovery["runtime"]
    owners = [item for item in discovery["family"]["packages"]
              if item["role"] == "core" and item["name"] == runtime["owner"]
              and runtime["executable"] in item["files"]]
    _require(len(owners) == 1 and owners[0]["version"] == runtime["version"],
             "Package/runtime drift must be reconciled before managed deployment")
    owner = _rpm("-qf", "--qf", r"%{NAME}\n", "--", runtime["executable"])["stdout"].strip()
    _require(owner == runtime["owner"], "CLI executable ownership changed")
    info = meta.phpinfo(_checked([runtime["executable"], "-i"])["stdout"])
    modules = [line.strip() for line in _checked([runtime["executable"], "-m"])["stdout"].splitlines()
               if line.strip() and not line.startswith("[")]
    _require(info == runtime["build"] and sorted(set(modules)) == runtime["loaded_extensions"],
             "Running PHP does not match fresh discovery")
    return inventory


def _listeners(listeners):
    _require(isinstance(listeners, list) and len(listeners) <= 16, "Explicit listener policy must be a bounded list")
    result = []
    for item in listeners:
        _require(isinstance(item, dict), "Listener policy must use objects")
        if item.get("type") == "tcp":
            address, port = item.get("address"), item.get("port")
            _require(isinstance(address, meta.STRING),
                     'A tcp listener needs an "address" key (explicit IPv4) and an integer "port"')
            _require(":" not in address, "Only explicit IPv4 listeners are supported; IPv6 is unsupported")
            parts = address.split(".")
            _require(len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)
                     and ".".join(str(int(part)) for part in parts) == address
                     and type(port) is int and 1 <= port <= 65535, "Invalid IPv4 listener identity")
            result.append(dict(type="tcp", address=address, port=port))
        else:
            _require(item.get("type") == "unix", "Only IPv4 TCP and Unix listener policies are supported")
            result.append(dict(type="unix", path=_path(item.get("path"))))
    return result


def _proc_read(path):
    return meta.read(path)


def _proc_link(path):
    return os.readlink(path)


def _proc_fds(pid):
    return os.listdir("/proc/{}/fd".format(pid))


def _owned_listeners(pid, listeners):
    owned = set()
    for descriptor in _proc_fds(pid):
        try:
            match = re.match(r"socket:\[([0-9]+)\]\Z",
                             _proc_link("/proc/{}/fd/{}".format(pid, descriptor)))
            if match:
                owned.add(match.group(1))
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
    tcp, unix = set(), set()
    if any(item["type"] == "tcp" for item in listeners):
        for line in _proc_read("/proc/{}/net/tcp".format(pid)).splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 10 and fields[3] == "0A" and fields[9] in owned:
                host, port = fields[1].split(":")
                # Python 2.7 requires a native str format despite unicode_literals.
                tcp.add((socket.inet_ntoa(struct.pack(str("=I"), int(host, 16))), int(port, 16)))
    if any(item["type"] == "unix" for item in listeners):
        for line in _proc_read("/proc/{}/net/unix".format(pid)).splitlines()[1:]:
            fields = line.split(None, 7)
            if (len(fields) == 8 and fields[4] == "0001" and fields[5] == "01"
                    and int(fields[3], 16) & 0x10000 and fields[6] in owned):
                unix.add(fields[7])
    _require(all((item["address"], item["port"]) in tcp if item["type"] == "tcp"
                 else item["path"] in unix for item in listeners),
             "Expected listeners are not listening sockets owned by the selected FPM master")
    return listeners


def _ready(show, values, executable, listeners):
    clock = getattr(time, "monotonic", time.time)
    deadline = clock() + SERVICE_READY_TIMEOUT
    identity = {key: values.get(key) for key in ("Id", "Names", "FragmentPath", "ExecStart")}
    while True:
        try:
            _require(all(values.get(key) == value for key, value in identity.items()),
                     "Configured service identity changed while waiting for readiness")
            pid = values.get("MainPID", "")
            _require(values.get("ActiveState") == "active" and pid.isdigit() and int(pid) > 1,
                     "FPM is not active with a valid MainPID")
            actual = _proc_link("/proc/{}/exe".format(pid))
            _require(actual == executable, "FPM MainPID executable is not the selected package-owned FPM")
            if listeners:
                _owned_listeners(pid, listeners)
            return dict(MainPID=int(pid), executable=actual, listeners=listeners)
        except (DeploymentError, OSError, IOError, ValueError) as error:
            if clock() >= deadline:
                raise DeploymentError("FPM readiness failed: " + str(error))
        time.sleep(min(0.1, max(0, deadline - clock())))
        output = _checked(show)["stdout"]
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def _service(service, discovery, active=True):
    _require(isinstance(service, dict), "Explicit FPM service policy is required")
    name, executable = service.get("name", ""), service.get("executable", "")
    _require(re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.service\Z", name) is not None,
             "Use one exact, non-template systemd service identity")
    _path(executable)
    core = [item for item in discovery["family"]["packages"] if item["role"] == "core"]
    owners = [item for item in core if executable in item["files"]]
    _require(executable in discovery["family"]["fpm_binaries"] and len(owners) == 1,
             "FPM executable must belong to the selected core family")
    stop, restart = _argv(service.get("stop")), _argv(service.get("restart"))
    _require(stop[1:] == ["stop", name] and restart[1:] == ["restart", name]
             and stop[0] == restart[0] and os.path.basename(os.path.realpath(stop[0])) == "systemctl",
             "Service argv must stop/restart only the explicit unit with systemctl")
    owner = _rpm("-qf", "--qf", r"%{NAME}\n", "--", executable)["stdout"].strip()
    _require(owner == owners[0]["name"], "FPM executable ownership changed")
    listeners = _listeners(service.get("listeners", []))
    show = [stop[0], "show", "--no-pager", "--property=Id", "--property=Names",
            "--property=FragmentPath", "--property=ExecStart", "--property=ActiveState",
            "--property=MainPID", name]
    output = _checked(show)["stdout"]
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    fragments = [path for item in core for path in item["files"] if os.path.basename(path) == name]
    paths = re.findall(r"(?:^|\s)path=([^ ;}]+)", values.get("ExecStart", ""))
    _require(values.get("Id") == name and values.get("Names", "").split() == [name]
             and values.get("FragmentPath") in fragments and paths == [executable],
             "Service identity, fragment or direct ExecStart does not match selected FPM")
    readiness = _ready(show, values, executable, listeners) if active else None
    return dict(name=name, executable=executable, stop=stop, restart=restart,
                listeners=listeners, readiness=readiness,
                MainPID=str(readiness["MainPID"]) if readiness else values.get("MainPID", ""))


def _control(data, action):
    service = _service(data["service"], data["discovery"], active=False)
    pid = service["MainPID"]
    _require(pid.isdigit() and (int(pid) == 0 or int(pid) > 1),
             "Refusing service control with an invalid MainPID")
    if int(pid):
        _require(_proc_link("/proc/{}/exe".format(pid)) == service["executable"],
                 "Refusing service control: current MainPID is a foreign executable")
    return _checked(service[action])


def _health(commands, discovery):
    _require(isinstance(commands, (list, tuple)) and 0 < len(commands) <= 16,
             "Explicit external application health argv policy is required")
    commands = [_argv(command) for command in commands]
    excluded = {os.path.realpath(path) for path in
                discovery["family"]["fpm_binaries"] + [discovery["runtime"]["executable"]]}
    for command in commands:
        _require(os.path.realpath(command[0]) not in excluded and os.path.basename(os.path.realpath(command[0])) not in (
            "sh", "bash", "dash", "zsh", "ksh", "csh", "fish", "env", "sudo", "rpm", "systemctl"),
            "Health policy must invoke external application probes directly, not shells or service/PHP checks")
    return commands


def _proposal(manifest_path, fresh_path, recovery_dir, service, health_commands, replacement_trust):
    meta.verify_manifest(manifest_path)
    manifest = meta.load(manifest_path)
    _require(manifest["kind"] == "isolated-rpm-build", "A verified local replacement RPM manifest is required")
    root = os.path.dirname(os.path.abspath(manifest_path))
    php_tests = _php_tests(manifest, root)
    manifest_sha256 = meta.digest(manifest_path)
    replacement_trust = _replacement_policy(replacement_trust, manifest_sha256)
    discovery = meta.load(fresh_path)
    original = manifest["discovery"]
    _require(discovery["family"]["source_name"] == original["family"]["source_name"]
             and discovery.get("environment_fingerprint") == original.get("environment_fingerprint")
             and discovery.get("compatibility", {}).get("sha256") == original.get("compatibility", {}).get("sha256"),
             "Manifest family/host fingerprint differs from fresh discovery")
    before = _fresh(discovery)
    service = _service(service, discovery)
    health_commands = _health(health_commands, discovery)
    core = [item for item in original["family"]["packages"] if item["role"] == "core"]
    selected = {_key(item): _nevra(item) for item in core}
    _require(0 < len(selected) == len(core) <= MAX_PACKAGES, "Empty, duplicate or excessive core selection")
    listed = {item["path"]: item for item in manifest["files"]}
    replacements, configurations = {}, set()
    for record in manifest["build"]["replacements"]:
        relative = record["path"]
        _require(relative in listed, "Replacement RPM is not covered by manifest")
        path = _path(os.path.join(root, relative))
        _require(path.startswith(root + os.sep) and relative.endswith(".rpm")
                 and meta.digest(path) == record["sha256"] == listed[relative]["sha256"],
                 "Replacement RPM digest/path mismatch")
        item = _package(path)
        key = _key(item)
        _require(key in selected and key not in replacements
                 and all(item[field] == record[field] for field in ("name", "version", "release", "architecture"))
                 and item["epoch"] == selected[key]["epoch"]
                 and item["version"] == manifest["release"]["version"]
                 and meta.source_name(item["source_reference"]) == original["family"]["source_name"]
                 and meta.version_tuple(item["version"]) >= meta.version_tuple(selected[key]["version"])
                 and ".".join(item["version"].split(".")[:2]) == original["runtime"]["branch"],
                 "Replacement changes family, branch, architecture, epoch or selected identity")
        flags = _file_flags(path)
        configs = {name for name, value in flags.items() if value & 1}
        old = next(package for package in original["family"]["packages"] if _key(package) == key)
        _require(set(old["configuration_files"]).issubset(configs), "Replacement drops original configurations")
        required = {name for name in old["files"] if name == discovery["runtime"]["executable"]
                    or name in discovery["family"]["fpm_binaries"]
                    or os.path.basename(name) == service["name"] or name.endswith(".so")}
        _require(required.issubset(flags), "Replacement drops selected runtime/module/service paths")
        configurations.update(configs)
        authentication = _replacement_authentication(path, replacement_trust)
        replacements[key] = dict(identity=item, path=path, sha256=record["sha256"],
                                 authentication=authentication)
    _require(set(replacements) == set(selected), "Require replacements for the complete selected core family")
    _path(recovery_dir)
    _require(os.path.isdir(recovery_dir), "Local recovery directory is required")
    names = sorted(os.listdir(recovery_dir))
    _require(len(names) <= 1024, "Recovery directory is too large")
    recovery = {}
    for name in names:
        if not name.endswith(".rpm"):
            continue
        path = os.path.join(recovery_dir, name)
        item = _package(path)
        key = _key(item)
        if key in selected and item == selected[key]:
            _require(key not in recovery, "Duplicate exact recovery RPM")
            _signature(path)
            recovery[key] = dict(identity=item, path=path, sha256=meta.digest(path))
    _require(set(recovery) == set(selected), "Missing signed exact-NEVRA recovery RPMs for complete core family")
    return dict(schema_version=1, status="planned", manifest_path=os.path.abspath(manifest_path),
                manifest_sha256=manifest_sha256, discovery=discovery, before=before,
                replacements=replacements, recovery=recovery, service=service,
                php_tests=php_tests, replacement_trust=replacement_trust,
                health_commands=health_commands, configuration_paths=sorted(configurations),
                host=_host(), changed=False, boundary="Local trusted RPMs; no dependency resolution or external-data recovery")


def _host():
    machine = "/etc/machine-id"
    return dict(machine=meta.read(machine).strip() if os.path.isfile(machine) else os.uname()[1],
                architecture=os.uname()[4])


def _private(directory):
    _path(directory)
    _trusted_parents(directory)
    if not os.path.exists(directory):
        os.mkdir(directory, 0o700)
    status = os.lstat(directory)
    _require(stat.S_ISDIR(status.st_mode) and status.st_uid == os.geteuid()
             and not status.st_mode & 0o077, "State directory must be private and owned by caller")
    for path in meta.descendants(directory):
        status = os.lstat(path)
        relative = os.path.relpath(path, directory)
        payload = re.match(r"snapshots/(?:[0-9]+|\.shift8-config-[0-9a-f]{32}/content)\Z", relative)
        regular = stat.S_ISREG(status.st_mode) and status.st_nlink == 1
        private = status.st_uid == os.geteuid() and not status.st_mode & 0o077
        _require(not stat.S_ISLNK(status.st_mode)
                 and ((payload and regular) or (private and (stat.S_ISDIR(status.st_mode) or regular))),
                 "Untrusted state entry: " + path)


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic(path, content, mode=0o600, uid=None, gid=None, times=None):
    _path(path)
    _trusted_parents(path)
    pending = path + ".shift8-" + uuid.uuid4().hex
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            if times is not None:
                os.utime(pending, times)
            os.fsync(stream.fileno())
        os.rename(pending, path)
        _sync(os.path.dirname(path))
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def _journal(directory, data, status=None):
    if status:
        data["status"] = status
    data["updated_at"] = time.time()
    _atomic(os.path.join(directory, "journal.json"),
            json.dumps(data, sort_keys=True, indent=2).encode("utf-8"))


@contextlib.contextmanager
def _lock():
    _path(LOCK_PATH)
    _trusted_parents(LOCK_PATH)
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        status = os.fstat(descriptor)
        _require(status.st_uid == os.geteuid() and stat.S_ISREG(status.st_mode)
                 and status.st_nlink == 1 and not status.st_mode & 0o077, "Unsafe deployment lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            raise DeploymentError("Another managed deployment/recovery holds the host lock")
        yield
    finally:
        os.close(descriptor)


def _copy(source, destination, digest):
    _path(source)
    with open(source, "rb") as stream:
        content = stream.read(MAX_RPM_BYTES + 1)
    _require(len(content) <= MAX_RPM_BYTES, "Local RPM/snapshot size limit exceeded")
    _require(hashlib.sha256(content).hexdigest() == digest, "Staged input changed: " + source)
    _atomic(destination, content)
    return destination


def _native(name, *arguments):
    executable = meta.which(name)
    _require(executable is not None, "Verified configuration preservation requires native " + name)
    return _checked([os.path.abspath(executable)] + list(arguments))


def _config_digest(path):
    _path(path)
    _require(hasattr(os, "O_NOATIME"), "O_NOATIME is required to verify configuration timestamps safely")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    result, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        _require(stat.S_ISREG(status.st_mode) and status.st_nlink == 1,
                 "Configuration must be regular and non-hardlinked")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            _require(size <= MAX_CONFIG_BYTES, "Configuration size limit exceeded")
            result.update(chunk)
    return result.hexdigest()


def _xattr_error(code):
    unsupported = (errno.ENOSYS, getattr(errno, "ENOTSUP", 95), getattr(errno, "EOPNOTSUPP", 95))
    reason = "filesystem unsupported" if code in unsupported else os.strerror(code or errno.EIO)
    raise DeploymentError("Cannot verify configuration xattrs: " + reason)


def _xattr_api():
    return getattr(os, "listxattr", None), getattr(os, "getxattr", None)


def _xattr_libc():
    try:
        library = ctypes.CDLL(None, use_errno=True)
        library.flistxattr.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        library.fgetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t]
        library.flistxattr.restype = library.fgetxattr.restype = ctypes.c_ssize_t
        return library
    except (AttributeError, OSError) as error:
        raise DeploymentError("libc descriptor xattr APIs are unavailable: " + str(error))


def _xattr_buffer(function, arguments):
    for unused in range(3):
        size = function(*(arguments + (None, 0)))
        if size < 0:
            _xattr_error(ctypes.get_errno())
        _require(size <= MAX_XATTR_VALUE, "Configuration xattr buffer limit exceeded")
        if size == 0:
            return b""
        buffer = ctypes.create_string_buffer(size)
        count = function(*(arguments + (buffer, size)))
        if count >= 0:
            _require(count <= size, "Invalid libc xattr response length")
            return buffer.raw[:count]
        if ctypes.get_errno() != errno.ERANGE:
            _xattr_error(ctypes.get_errno())
    raise DeploymentError("Configuration xattrs changed repeatedly during capture")


def _libc_xattrs(descriptor):
    library = _xattr_libc()
    raw = _xattr_buffer(library.flistxattr, (descriptor,))
    _require(not raw or raw.endswith(b"\x00"), "Malformed libc xattr name list")
    names = raw.split(b"\x00")[:-1] if raw else []
    _require(len(names) <= MAX_XATTR_NAMES and all(names) and len(set(names)) == len(names),
             "Excessive/duplicate/empty configuration xattr names")
    return ((name, _xattr_buffer(library.fgetxattr, (descriptor, name))) for name in names)


def _xattr_name_bytes(name):
    if isinstance(name, bytes):
        return name
    if hasattr(os, "fsencode"):
        return os.fsencode(name)
    return name.encode(sys.getfilesystemencoding() or "utf-8")


def _xattrs(path):
    """Capture exact xattr bytes from a pinned, non-symlink file descriptor."""
    _path(path)
    _require(hasattr(os, "O_NOATIME"), "O_NOATIME is required for xattr verification")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        status = os.fstat(descriptor)
        _require(stat.S_ISREG(status.st_mode) and status.st_nlink == 1,
                 "Xattrs require a regular, non-hardlinked configuration")
        listing, getting = _xattr_api()
        if callable(listing) and callable(getting):
            names = listing(descriptor)
            _require(len(names) <= MAX_XATTR_NAMES, "Configuration xattr name count exceeded")
            pairs = ((name, getting(descriptor, name)) for name in names)
        else:
            pairs = _libc_xattrs(descriptor)
        result, names, total = [], set(), 0
        for name, value in pairs:
            name = _xattr_name_bytes(name)
            _require(name and b"\x00" not in name and name not in names
                     and isinstance(value, bytes) and len(value) <= MAX_XATTR_VALUE,
                     "Malformed/duplicate configuration xattr")
            names.add(name)
            total += len(name) + len(value)
            _require(total <= MAX_XATTR_BYTES, "Configuration xattr total size exceeded")
            result.append("0x{}=0x{}".format(binascii.hexlify(name).decode("ascii"),
                                            binascii.hexlify(value).decode("ascii")))
        return sorted(result)
    except (OSError, IOError) as error:
        _xattr_error(error.errno)
    finally:
        os.close(descriptor)


def _config_metadata(path):
    _path(path)
    status = os.lstat(path)
    _require(stat.S_ISREG(status.st_mode) and status.st_nlink == 1,
             "Only regular, non-hardlinked configurations can be preserved")
    facts = _native("stat", "--printf=%f\\t%u\\t%g\\t%s\\t%.9X\\t%.9Y\\n", "--", path)["stdout"]
    acl = _native("getfacl", "--absolute-names", "--numeric", "--omit-header", "--access", "--", path)["stdout"]
    xattrs = _xattrs(path)
    _require("\ufffd" not in facts + acl, "Security metadata encoding cannot be verified")
    _require(len(facts.rstrip("\n").split("\t")) == 6 and bool(acl.strip())
             and all(re.match(r"0x[0-9a-f]+=0x[0-9a-f]*\Z", line) for line in xattrs),
             "Native configuration metadata is malformed or incomplete")
    return dict(stat=facts, acl=acl, xattrs=xattrs)


def _preserve_config(source, destination, digest, security):
    """Verify GNU cp rather than trusting --preserve=all's warning-only failures."""
    _path(source)
    _path(destination)
    _trusted_parents(source)
    _trusted_parents(destination)
    _require(_config_digest(source) == digest and _config_metadata(source) == security,
             "Configuration bytes/security metadata changed before preservation")
    parent = os.path.dirname(destination)
    staging = os.path.join(parent, ".shift8-config-" + uuid.uuid4().hex)
    os.mkdir(staging, 0o700)
    pending = os.path.join(staging, "content")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        try:
            _native("cp", "--preserve=all", "--reflink=never", "--no-dereference", "--", source, pending)
        finally:
            # Preserve the source snapshot even when cp partially fails or loses metadata.
            times = security["stat"].rstrip("\n").split("\t")
            for kind, value in (("--access", times[4]), ("--modify", times[5])):
                _path(source)
                _native("touch", "--no-dereference", kind, "--date=@" + value, "--", source)
        _require(_config_digest(pending) == digest and _config_metadata(pending) == security,
                 "Native copy did not preserve all configuration security metadata")
        _require(_config_digest(source) == digest and _config_metadata(source) == security,
                 "Source configuration metadata changed during native backup/restore")
        descriptor = os.open(pending, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(pending, destination)
        _sync(parent)
        _require(_config_digest(destination) == digest and _config_metadata(destination) == security,
                 "Restored configuration security metadata could not be verified")
        return destination
    finally:
        if os.path.lexists(pending):
            os.unlink(pending)
        os.rmdir(staging)
        _sync(parent)


def _stage(directory, data):
    packages = os.path.join(directory, "packages")
    snapshots = os.path.join(directory, "snapshots")
    os.mkdir(packages, 0o700)
    os.mkdir(snapshots, 0o700)
    for kind in ("recovery", "replacements"):
        for index, key in enumerate(sorted(data[kind])):
            record = data[kind][key]
            record["path"] = _copy(record["path"], os.path.join(packages, "{}-{}.rpm".format(kind, index)),
                                   record["sha256"])
    configurations = set(data["configuration_paths"])
    configurations.update(path + suffix for path in list(configurations) for suffix in (".rpmnew", ".rpmsave"))
    _require(len(configurations) <= 3072, "Configuration snapshot count exceeded")
    data["snapshots"] = []
    total = 0
    for index, path in enumerate(sorted(configurations)):
        _path(path)
        _trusted_parents(path)
        _require(not (path == directory or path.startswith(directory + os.sep)),
                 "Configuration overlaps deployment state")
        record = dict(path=path, exists=os.path.exists(path))
        if record["exists"]:
            status = os.stat(path)
            _require(stat.S_ISREG(status.st_mode) and status.st_nlink == 1,
                     "Only regular, non-hardlinked configuration files are supported")
            total += status.st_size
            _require(total <= MAX_CONFIG_BYTES, "Configuration snapshot size exceeded")
            record.update(mode=stat.S_IMODE(status.st_mode), uid=status.st_uid, gid=status.st_gid,
                          atime=status.st_atime, mtime=status.st_mtime, sha256=_config_digest(path),
                          security_metadata=_config_metadata(path))
            record["snapshot"] = _preserve_config(path, os.path.join(snapshots, str(index)),
                                                   record["sha256"], record["security_metadata"])
        data["snapshots"].append(record)
    _sync(packages)
    _sync(snapshots)
    _journal(directory, data, "prepared")


def _validate_recovery(directory, data):
    _require(data.get("schema_version") == 1 and data.get("status") in RECOVERABLE
             and data.get("host") == _host(), "Journal is not recoverable on this host")
    _require(set(data["recovery"]) == set(data["replacements"]) and data["recovery"],
             "Incomplete journal recovery set")
    for key, record in data["recovery"].items():
        _require(record["path"].startswith(directory + "/packages/")
                 and meta.digest(_path(record["path"])) == record["sha256"]
                 and _package(record["path"]) == record["identity"] == data["before"].get(key),
                 "Recovery RPM/journal tampering detected")
        _signature(record["path"])
    for record in data["snapshots"]:
        _path(record["path"])
        if record["exists"]:
            _require(record["snapshot"].startswith(directory + "/snapshots/")
                     and _config_digest(_path(record["snapshot"])) == record["sha256"]
                     and _config_metadata(record["snapshot"]) == record.get("security_metadata"),
                     "Configuration snapshot bytes/security metadata tampering detected")
    _service(data["service"], data["discovery"], active=False)
    _health(data["health_commands"], data["discovery"])


def _restore(data):
    for record in data["snapshots"]:
        path = _path(record["path"])
        _trusted_parents(path)
        if record["exists"]:
            _preserve_config(record["snapshot"], path, record["sha256"], record["security_metadata"])
        elif os.path.exists(path):
            _require(stat.S_ISREG(os.lstat(path).st_mode), "Refusing to remove non-file configuration: " + path)
            os.unlink(path)
            _sync(os.path.dirname(path))


def _transaction(paths, rollback_only=False, test=False):
    executable = meta.which("rpm")
    _require(executable is not None, "rpm executable is required")
    command = [os.path.abspath(executable), "-Uvh"]
    if test:
        command.append("--test")
    if rollback_only:
        command.append("--oldpackage")
    return _checked(command + paths, timeout=300)


def _assert_after(data, inventory, restored=False):
    expected = dict(data["before"])
    if not restored:
        expected.update({key: record["identity"] for key, record in data["replacements"].items()})
    _require(inventory == expected, "Actual package inventory differs; unrelated packages must remain unchanged")


def _root():
    _require(os.geteuid() == 0, "Applying deployment/rollback requires root")


def _recover(directory, data):
    _validate_recovery(directory, data)
    _journal(directory, data, "rolling_back")
    try:
        _control(data, "stop")
        current = _inventory()
        paths = [record["path"] for key, record in sorted(data["recovery"].items())
                 if current.get(key) != record["identity"]]
        if paths:
            _transaction(paths, rollback_only=True, test=True)
            _transaction(paths, rollback_only=True)
        _restore(data)
        _control(data, "restart")
        data["rollback_service"] = _service(data["service"], data["discovery"])
        data["rollback_health"] = [_checked(command) for command in data["health_commands"]]
        data["postrollback"] = _inventory()
        _assert_after(data, data["postrollback"], restored=True)
        _journal(directory, data, "rolled_back")
    except BaseException as error:
        data["rollback_error"] = "{}: {}".format(type(error).__name__, error)
        data["rollback_failure_result"] = getattr(error, "result", None)
        _journal(directory, data, "rollback_failed")
        raise


def plan(manifest_path, fresh_discovery_path, recovery_dir, state_dir,
         service, health_commands, apply=False, replacement_trust=None):
    """Inspect, or explicitly apply, one complete core replacement transaction.

    apply=True requires root, a new private state directory and application
    probes passing before and after the update. An existing journal is never
    overwritten. Failures raise DeploymentError; consult journal.json for the
    original error and whether recovery completed. No retries are implicit.
    Unsigned local replacements additionally require replacement_trust containing
    reviewed=True, allow_unsigned=True, manifest_sha256 and a nonempty reason.
    """
    _require(isinstance(apply, bool), "apply must be an explicit boolean")
    if apply:
        _root()
    try:
        data = _proposal(manifest_path, fresh_discovery_path, recovery_dir, service, health_commands,
                         replacement_trust)
    except DeploymentError:
        raise
    except (ValueError, KeyError, TypeError, OSError, IOError) as error:
        raise DeploymentError("Deployment input inspection failed: " + str(error))
    if not apply:
        return data
    directory = _path(os.path.abspath(state_dir))
    with _lock():
        _private(directory)
        _require(not os.listdir(directory), "State directory is not empty; use rollback for an existing journal")
        _stage(directory, data)
        try:
            data["original_service"] = _service(data["service"], data["discovery"])
            data["original_health"] = [_checked(command) for command in data["health_commands"]]
            paths = [record["path"] for key, record in sorted(data["replacements"].items())]
            _transaction(paths, test=True)
            meta.verify_manifest(manifest_path)
            _require(meta.digest(manifest_path) == data["manifest_sha256"], "Input manifest changed before update")
            _require(_fresh(data["discovery"]) == data["before"], "Host changed during preflight")
            _validate_recovery(directory, data)
            _service(data["service"], data["discovery"])
            for record in data["replacements"].values():
                _require(meta.digest(record["path"]) == record["sha256"], "Staged replacement changed")
                _require(_replacement_authentication(record["path"], data["replacement_trust"])
                         == record["authentication"], "Staged replacement authentication changed")
            _journal(directory, data, "stopping")
            _control(data, "stop")
            _journal(directory, data, "updating")
            data["update_result"] = _transaction(paths)
            data["changed"] = True
            _restore(data)
            _journal(directory, data, "restarting")
            _control(data, "restart")
            data["service_postactual"] = _service(data["service"], data["discovery"])
            _journal(directory, data, "checking")
            data["health"] = [_checked(command) for command in data["health_commands"]]
            data["postactual"] = _inventory()
            _assert_after(data, data["postactual"])
            _journal(directory, data, "applied")
            return data
        except BaseException as error:
            data["error"] = "{}: {}".format(type(error).__name__, error)
            data["failure_result"] = getattr(error, "result", None)
            if data["status"] == "prepared":
                _journal(directory, data, "preflight_failed")
                raise DeploymentError("Preflight failed; no service/package changes: " + data["error"])
            try:
                data["postactual"] = _inventory()
            except Exception as inspection_error:
                data["postactual_error"] = str(inspection_error)
            try:
                _recover(directory, data)
            except BaseException as recovery_error:
                data["rollback_error"] = "{}: {}".format(type(recovery_error).__name__, recovery_error)
                _journal(directory, data, "rollback_failed")
                raise DeploymentError("Deployment failed; rollback incomplete; preserve state: " + directory)
            raise DeploymentError("Deployment failed; original packages/configuration recovered: " + data["error"])


def rollback(state_dir, apply=False):
    """Inspect or explicitly resume rollback using only captured local recovery."""
    _require(isinstance(apply, bool), "apply must be an explicit boolean")
    if apply:
        _root()
    directory = _path(os.path.abspath(state_dir))
    _require(os.path.isdir(directory), "Existing recovery state directory is required")
    if not apply:
        _private(directory)
        try:
            data = meta.load(os.path.join(directory, "journal.json"))
            _validate_recovery(directory, data)
            return data
        except (ValueError, KeyError, TypeError, OSError, IOError) as error:
            raise DeploymentError("Recovery inspection failed: " + str(error))
    with _lock():
        _private(directory)
        try:
            data = meta.load(os.path.join(directory, "journal.json"))
            _recover(directory, data)
        except BaseException as error:
            raise DeploymentError("Rollback failed; preserve state {}: {}".format(directory, error))
        return data


def main(arguments=None):
    """Optional local CLI; policies are explicit JSON files, never shell text."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "rollback"))
    parser.add_argument("--state-dir", required=True)
    for name in ("manifest", "fresh-discovery", "recovery-dir", "service-policy", "health-policy",
                 "replacement-trust"):
        parser.add_argument("--" + name)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(arguments)
    try:
        if args.operation == "rollback":
            if any(getattr(args, name) for name in (
                    "manifest", "fresh_discovery", "recovery_dir", "service_policy", "health_policy",
                    "replacement_trust")):
                parser.error("rollback uses captured journal policies; plan flags are not accepted")
            result = rollback(args.state_dir, apply=args.apply)
        else:
            required = ("manifest", "fresh_discovery", "recovery_dir", "service_policy", "health_policy")
            if not all(getattr(args, name) for name in required):
                parser.error("plan requires manifest, fresh-discovery, recovery-dir, service-policy and health-policy")
            result = plan(args.manifest, args.fresh_discovery, args.recovery_dir, args.state_dir,
                          meta.load(args.service_policy), meta.load(args.health_policy), apply=args.apply,
                          replacement_trust=meta.load(args.replacement_trust) if args.replacement_trust else None)
        meta.dump(result)
        return 0
    except (ValueError, OSError, IOError) as error:
        parser.exit(1, "deployment: {}\n".format(error))


if __name__ == "__main__":
    main()
