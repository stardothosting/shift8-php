#!/usr/bin/env python
"""Strict data handling for the Bash orchestrator; never evaluates packaging code."""

from __future__ import print_function, unicode_literals

import glob
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from collections import namedtuple

try:
    import configparser
    from urllib.parse import urlsplit, urljoin, parse_qs
    from html.parser import HTMLParser
except ImportError:
    import ConfigParser as configparser
    from urlparse import urlsplit, urljoin, parse_qs
    from HTMLParser import HTMLParser

try:
    STRING = basestring
except NameError:
    STRING = str

Result = namedtuple("Result", "returncode stdout stderr")


PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+:_-]*\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)\Z")
CACHE_TTL = 86400


def load(path):
    with io.open(str(path), encoding="utf-8") as stream:
        return json.load(stream)


def dump(data, path=None):
    if path is None:
        json.dump(data, sys.stdout, indent=2, sort_keys=True)
        print()
    else:
        with io.open(str(path), "w", encoding="utf-8") as stream:
            text = json.dumps(data, indent=2, sort_keys=True)
            stream.write(text if isinstance(text, type("")) else text.decode("ascii"))
            stream.write("\n")


def read(path):
    with io.open(str(path), encoding="utf-8") as stream:
        return stream.read()


def which(command):
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        path = os.path.join(directory, command)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def descendants(directory):
    for root, directories, files in os.walk(str(directory), followlinks=False):
        for name in sorted(directories + files):
            yield os.path.join(root, name)


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def run(arguments):
    # Polling files avoids pipe deadlocks and works on Python 2.7.
    import tempfile
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(arguments, stdout=output, stderr=errors)
            deadline = time.time() + 60
            while process.poll() is None:
                if time.time() > deadline:
                    process.kill()
                    process.wait()
                    raise ValueError("Inspection timed out: " + arguments[0])
                time.sleep(0.02)
            output.seek(0)
            errors.seek(0)
            return Result(process.returncode, output.read().decode("utf-8", "replace"), errors.read().decode("utf-8", "replace"))
    except OSError as error:
        raise ValueError("Cannot inspect {}: {}".format(arguments[0], error))


def https(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.fragment
        or any(ord(character) < 33 for character in url)
    ):
        raise ValueError("Only credential-free HTTPS input URLs are permitted: " + url)
    return url


def filename(name):
    if not re.match(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*\Z", name):
        raise ValueError("Unsafe source filename: " + name)
    return name


def version_tuple(version):
    match = VERSION.match(version)
    if not match:
        raise ValueError("Not a stable upstream PHP version: " + version)
    return tuple(int(part) for part in match.groups())


def upstream_version(version):
    match = re.match(r"^(?:\d+:)?(\d+\.\d+\.\d+)(?:$|[-+~])", version)
    if not match:
        raise ValueError("Cannot establish stable PHP version from " + version)
    return match.group(1)


def manager(package):
    matches = []
    if which("rpm"):
        result = run(["rpm", "-q", "--qf", "%{NAME}\n", "--", package])
        if result.returncode == 0:
            matches.append("rpm")
    if which("dpkg-query"):
        result = run(["dpkg-query", "-W", "-f=${db:Status-Status}", package])
        if result.returncode == 0 and result.stdout.strip() == "installed":
            matches.append("deb")
    if len(matches) != 1:
        raise ValueError(
            "Expected one installed package-manager owner for {!r}; found {}. "
            "Use an exact installed package name, including architecture if ambiguous."
            .format(package, matches)
        )
    return matches[0]


def source_name(srpm):
    if not srpm.endswith(".src.rpm"):
        raise ValueError("Installed package has no usable source RPM reference: " + srpm)
    parts = srpm[:-8].rsplit("-", 2)
    if len(parts) != 3:
        raise ValueError("Malformed source RPM reference: " + srpm)
    return parts[0]


def inventory(kind, requested, path):
    packages = []
    for line in read(path).splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            raise ValueError("Malformed {} package metadata record".format(kind))
        if kind == "rpm":
            name, epoch, version, release, architecture, source, vendor = parts
            item = dict(
                name=name, epoch="0" if epoch in ("(none)", "(null)") else epoch,
                version=version, release=release, architecture=architecture,
                source_reference=source, vendor=vendor,
            )
            item["source_name"] = source_name(source) if source.endswith(".src.rpm") else ""
            item["source_version"] = source[:-8] if item["source_name"] else ""
        else:
            name, version, architecture, source, source_version, status, vendor = parts
            if status != "installed":
                continue
            item = dict(
                name=name, version=version, architecture=architecture,
                source_name=source or name, source_version=source_version or version,
                source_reference="{}={}".format(source or name, source_version or version),
                vendor=vendor, epoch=version.split(":")[0] if ":" in version else "0",
                release="",
            )
        if not PACKAGE_NAME.match(item["name"]):
            raise ValueError("Unsafe package identifier in installed metadata")
        packages.append(item)
    name, separator, architecture = requested.partition(":")
    targets = [
        item for item in packages
        if item["name"] == name and (not separator or item["architecture"] == architecture)
    ]
    if len(targets) != 1:
        choices = ["{}:{}".format(item["name"], item["architecture"]) for item in targets]
        raise ValueError("Installed package is missing or ambiguous. Exact choices: " + ", ".join(choices))
    target = targets[0]
    if not target["source_name"]:
        raise ValueError("Requested package has no source package provenance")
    architectures = (target["architecture"], "noarch", "all")
    packages = [item for item in packages if item["architecture"] in architectures]
    core = [item for item in packages if item["source_name"] == target["source_name"]]
    if kind == "rpm":
        prefix = target["source_name"] + "-"
        candidates = [item for item in packages if item in core or item["name"].startswith(prefix)]
    else:
        candidates = [
            item for item in packages
            if item in core or item["name"].startswith(("php-", target["source_name"] + "-"))
        ]
    return dict(manager=kind, requested=target, core=core, candidates=candidates)


def binary(data, directory):
    choices = []
    for item in data["core"]:
        for path in read(os.path.join(str(directory), item["name"] + ".files")).splitlines():
            if re.match(r"php(?:\d+\.\d+)?\Z", os.path.basename(path)):
                if "/bin/" in path:
                    choices.append((item["name"], path))
    if len(choices) != 1:
        raise ValueError(
            "Cannot unambiguously resolve package-owned CLI in this source family. "
            "Candidates: " + repr(choices) + ". Supply the runtime's CLI package, not a PECL/metapackage."
        )
    return choices[0]


def phpinfo(text):
    keys = {
        "PHP Version", "PHP API", "PHP Extension", "Zend Extension",
        "Thread Safety", "Configure Command", "Compiler", "extension_dir",
        "Loaded Configuration File", "Scan this dir for additional .ini files",
        "Additional .ini files parsed", "Server API", "PHP Extension Build",
        "Zend Extension Build", "Configuration File (php.ini) Path",
    }
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition(" => ")
        if separator and key in keys and key not in result:
            result[key] = value.split(" => ")[0].strip()
    for key in ("PHP Version", "PHP API", "PHP Extension", "Zend Extension", "Thread Safety", "extension_dir"):
        if not result.get(key):
            raise ValueError("Runtime inspection did not report " + key)
    for key in ("PHP API", "PHP Extension", "Zend Extension"):
        if not result[key].isdigit():
            raise ValueError("Invalid PHP ABI field: " + key)
    if result["Thread Safety"] not in ("enabled", "disabled"):
        raise ValueError("Cannot establish runtime ZTS/NTS configuration")
    return result


def distribution(root="/"):
    for relative in ("etc/os-release", "usr/lib/os-release"):
        path = os.path.join(str(root), relative)
        if os.path.isfile(path):
            data = {}
            for line in read(path).splitlines():
                if not line or line.startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if not separator or not re.match(r"[A-Z_]+\Z", key):
                    continue
                tokens = shlex.split(value, comments=False)
                data[key] = " ".join(tokens)
            return dict(
                id=data.get("ID", "unknown"), version=data.get("VERSION_ID", "unknown"),
                id_like=data.get("ID_LIKE", ""), description=data.get("PRETTY_NAME", ""),
                evidence="/" + relative, support="experimental",
            )
    for relative, family in (("etc/redhat-release", "rhel-like"), ("etc/debian_version", "debian-like")):
        if os.path.isfile(os.path.join(str(root), relative)):
            return dict(
                id=family, version=read(os.path.join(str(root), relative)).strip(),
                evidence="/" + relative, support="experimental",
            )
    if which("lsb_release"):
        result = run(["lsb_release", "-a"])
        if result.returncode == 0:
            return dict(id="unknown", evidence="lsb_release", description=result.stdout, support="experimental")
    raise ValueError("Linux distribution cannot be established from local release metadata")


def snapshot(directory, requested):
    directory = str(directory)
    data = load(os.path.join(directory, "inventory.json"))
    info = load(os.path.join(directory, "phpinfo.json"))
    owner, executable = read(os.path.join(directory, "binary")).strip().split("\t")
    runtime_version = upstream_version(info["PHP Version"])
    core = data["core"]
    cli = next(item for item in core if item["name"] == owner)
    if upstream_version(cli["version"]) != runtime_version:
        raise ValueError("Package version does not match its runtime; repair installation before preparation")
    extension_dir = info["extension_dir"]
    if not os.path.isabs(extension_dir):
        raise ValueError("Runtime extension directory is not absolute")
    family = []
    rejected = []
    for item in data["candidates"]:
        base = os.path.join(directory, "package-details", item["name"])
        files = read(str(base) + ".files").splitlines()
        requires = read(str(base) + ".requires").splitlines()
        provides = read(str(base) + ".provides").splitlines()
        configs = read(str(base) + ".config").splitlines()
        is_core = item in core
        owns_extension = any(path.startswith(extension_dir.rstrip("/") + "/") and path.endswith(".so") for path in files)
        if not is_core and not owns_extension:
            rejected.append(item["name"])
            continue
        details = dict(item)
        details.update(
            role="core" if is_core else "separate-extension",
            requires=requires, provides=provides,
            configuration_files=configs, files=files,
            scriptlets=read(str(base) + ".scripts") if data["manager"] == "rpm" else "not-inspected",
            compatibility="not-runtime-validated",
        )
        family.append(details)
    modules = [
        line.strip() for line in read(os.path.join(directory, "modules.txt")).splitlines()
        if line.strip() and not line.startswith("[")
    ]
    compiler = info.get("Compiler", "")
    configure = info.get("Configure Command", "")
    tools = {}
    for command in ("gcc", "make", "rpmbuild", "mock", "dpkg-buildpackage", "sbuild", "pbuilder", "php-config", "phpize"):
        tools[command] = which(command)
    toolsets = sorted(glob.glob("/opt/rh/*/root/usr/bin/gcc"))
    warnings = [
        "No build, runtime compatibility, service/socket or application health validation has been performed.",
        "Repository availability is not evidence of ongoing security support.",
        "PHP info output is allow-listed; environment and application secrets are not stored.",
        "Unpackaged or externally located extensions require operator review before future deployment.",
    ]
    if len({item["source_reference"] for item in core}) > 1:
        warnings.append("Installed core packages have different source revisions; build drift requires review.")
    fpm = sorted({
        path for item in family for path in item["files"]
        if re.match(r"php-fpm(?:\d+\.\d+)?\Z", os.path.basename(path)) and "/sbin/" in path
    })
    return dict(
        schema_version=1, requested_package=requested, package_manager=data["manager"],
        distribution=distribution(), architecture=cli["architecture"],
        runtime=dict(
            owner=owner, executable=executable, version=runtime_version,
            branch=".".join(runtime_version.split(".")[:2]),
            php_api=info["PHP API"], module_api=info["PHP Extension"],
            zend_extension_api=info["Zend Extension"], zts=info["Thread Safety"] == "enabled",
            extension_dir=extension_dir, loaded_extensions=sorted(set(modules)),
            ini_report=read(os.path.join(directory, "ini.txt")).splitlines(), build=info,
        ),
        family=dict(
            source_name=cli["source_name"], source_version=cli["source_version"],
            source_reference=cli["source_reference"], names=[item["name"] for item in family],
            packages=family, excluded_candidates=rejected, fpm_binaries=fpm,
        ),
        environment=dict(
            commands=tools, software_collection_compilers=toolsets,
            original_compiler=compiler, original_configure_command=configure,
            free_bytes=os.statvfs(directory).f_bavail * os.statvfs(directory).f_frsize,
            memory_kib=memory_kib(), kernel_architecture=os.uname()[4],
        ),
        warnings=warnings,
    )


def memory_kib():
    path = "/proc/meminfo"
    if not os.path.isfile(path):
        return None
    match = re.search(r"^MemTotal:\s+(\d+)", read(path), re.MULTILINE)
    return int(match.group(1)) if match else None


def finish_snapshot(path, services):
    data = load(path)
    units = []
    for service in sorted(set(read(services).splitlines())):
        item = dict(path=service, state="not-inspected")
        if service.endswith(".service") and which("systemctl"):
            result = run([
                "systemctl", "show", "--no-pager", "--property=ActiveState",
                "--property=UnitFileState", "--property=FragmentPath", "--property=DropInPaths",
                os.path.basename(service),
            ])
            item["state"] = "inspected" if result.returncode == 0 else "unavailable"
            item["details"] = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
        units.append(item)
    data["services"] = units
    data["web_integration"] = "not-verified; CLI discovery does not establish Nginx/Apache routing"
    data["environment_fingerprint"] = hashlib.sha256(
        json.dumps(dict(runtime=data["runtime"], family=data["family"], services=units), sort_keys=True).encode()
    ).hexdigest()
    data["compatibility"] = compatibility(data)
    dump(data, path)


def runtime_libraries(data):
    paths = {data["runtime"]["executable"]}
    paths.update(data["family"]["fpm_binaries"])
    paths.update(
        path for item in data["family"]["packages"] for path in item["files"]
        if path.endswith(".so") and os.path.isfile(path)
    )
    libraries, failures = {}, []
    for path in sorted(paths):
        if not os.path.isfile(path):
            failures.append("Missing runtime file: " + path)
            continue
        with open(path, "rb") as stream:
            if stream.read(4) != b"\x7fELF":
                if path == data["runtime"]["executable"]:
                    failures.append("CLI is not a directly inspectable ELF binary")
                continue
        result = run(["ldd", path])
        if result.returncode or "not found" in result.stdout:
            failures.append("Runtime library inspection failed: " + path)
            continue
        for line in result.stdout.splitlines():
            match = re.search(r"(?:=>\s+|^\s*)(/[^\s]+)", line)
            if match:
                libraries[os.path.realpath(match.group(1))] = None
    for path in sorted(libraries):
        if data["package_manager"] == "rpm":
            result = run(["rpm", "-qf", "--qf", "%{NAME}\t%{EPOCH}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n", "--", path])
        else:
            ownership = run(["dpkg-query", "-S", path])
            if ownership.returncode:
                failures.append("Library has no package owner: " + path)
                continue
            owner = ownership.stdout.split(": /", 1)[0]
            if "\n" in owner or "," in owner:
                failures.append("Ambiguous library ownership: " + path)
                continue
            result = run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\n", owner])
        if result.returncode or len(result.stdout.strip().splitlines()) != 1:
            failures.append("Cannot establish library package version: " + path)
            continue
        libraries[path] = dict(package=result.stdout.strip(), sha256=digest(path))
    return dict(
        complete=bool(libraries) and not failures and all(libraries.values()),
        libraries=libraries, diagnostics=failures,
    )


def compatibility(data):
    paths = set()
    for item in data["family"]["packages"]:
        for record in item["configuration_files"]:
            fields = record.split()
            if fields and fields[0].startswith("/"):
                paths.add(fields[0])
    for service in data.get("services", []):
        paths.add(service["path"])
        for match in re.findall(r"^(?:DropInPaths|FragmentPath)=(.*)$", service.get("details", ""), re.MULTILINE):
            paths.update(match.split())
    for item in data["family"]["packages"]:
        for path in item["files"]:
            if os.path.isdir(path) and re.search(r"/(?:php[^/]*\.d|fpm[^/]*\.d)/?$", path):
                paths.update(entry for entry in descendants(path) if os.path.isfile(entry))
    configurations = {}
    for path in sorted(paths):
        configurations[path] = digest(path) if os.path.isfile(path) else "missing"
    libraries = data.get("runtime_libraries", dict(complete=False, diagnostics=["Library inventory missing"], libraries={}))
    runtime_paths = {data["runtime"]["executable"]}
    runtime_paths.update(data["family"]["fpm_binaries"])
    runtime_paths.update(
        path for item in data["family"]["packages"] for path in item["files"]
        if path.endswith(".so")
    )
    inputs = dict(
        distribution={key: data["distribution"].get(key) for key in ("id", "version")},
        architecture=data["architecture"], package_manager=data["package_manager"],
        runtime={key: data["runtime"][key] for key in (
            "executable", "version", "php_api", "module_api", "zend_extension_api",
            "zts", "extension_dir", "loaded_extensions", "build",
        )},
        packages=sorted(
            ({key: item[key] for key in ("name", "epoch", "version", "release", "architecture", "source_reference", "requires", "provides", "role")}
             for item in data["family"]["packages"]), key=lambda item: item["name"],
        ),
        runtime_libraries=libraries["libraries"], configurations=configurations,
        runtime_files={path: digest(path) if os.path.isfile(path) else "missing" for path in sorted(runtime_paths)},
    )
    return dict(
        schema_version=1, inputs=inputs, complete=libraries["complete"],
        sha256=hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        diagnostics=libraries["diagnostics"],
        claim="Exact conservative pre-deployment input match only; not runtime or application validation.",
    )


def store(path):
    path = os.path.abspath(str(path))
    if path == "/" or not os.path.isdir(os.path.dirname(path)):
        raise ValueError("Artifact store requires an existing parent directory and must not be /")
    if os.path.islink(path):
        raise ValueError("Artifact store must not be a symlink")
    if not os.path.exists(path):
        os.mkdir(path, 0o700)
    for directory in [path] + [os.path.join(path, name) for name in ("builds", "cache", "packages", "manifests", "logs", "backups")]:
        if not os.path.exists(directory):
            os.mkdir(directory, 0o700)
        status = os.lstat(directory)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid() or status.st_mode & 0o077:
            raise ValueError("Artifact directories must be owned by the invoking user and mode 0700: " + str(directory))
    for existing in descendants(path):
        status = os.lstat(existing)
        if (
            stat.S_ISLNK(status.st_mode) or status.st_uid != os.geteuid()
            or status.st_mode & 0o022 or (stat.S_ISREG(status.st_mode) and status.st_nlink != 1)
        ):
            raise ValueError("Untrusted artifact-store entry: " + str(existing))
    return str(path)


def cache_write(temporary, destination, url, expected):
    https(url)
    actual = digest(temporary)
    if expected and (not SHA256.match(expected) or actual != expected):
        raise ValueError("SHA-256 does not match trusted input metadata")
    os.rename(temporary, destination)
    dump(dict(
        url=url, sha256=actual, fetched_at=int(time.time()),
        transport="HTTPS with system CA verification",
        expected_sha256=expected or None,
    ), destination + ".receipt.json")


def cache_check(path, url, expected):
    receipt = load(path + ".receipt.json")
    if receipt["url"] != url or digest(path) != receipt["sha256"]:
        raise ValueError("Cache URL/checksum mismatch: " + path)
    if expected:
        if not SHA256.match(expected) or digest(path) != expected:
            raise ValueError("Cache does not match independently trusted SHA-256: " + path)
    elif not 0 <= time.time() - receipt["fetched_at"] <= CACHE_TTL:
        raise ValueError("Metadata cache is stale/future-dated; refresh online (maximum age: 24h)")


def select(discovery, support, releases, target):
    current = discovery["runtime"]["version"]
    branch = discovery["runtime"]["branch"]
    if not isinstance(support, dict) or not isinstance(releases, dict):
        raise ValueError("Invalid authoritative PHP support/release metadata shape")
    supported = support.get("supported_versions")
    if not isinstance(supported, list) or branch not in supported:
        raise ValueError(
            "No current upstream security patch target can be established for PHP {}. "
            "Migrate or use a verified extended-support vendor; compiling the final EOL release is not a security fix."
            .format(branch)
        )
    versions = [value for value in releases if VERSION.match(value) and value.startswith(branch + ".")]
    if not versions:
        raise ValueError("No stable authoritative release found for installed branch")
    for value in versions:
        entry = releases[value]
        if (
            not isinstance(entry, dict) or not isinstance(entry.get("tags", []), list)
            or not all(isinstance(tag, STRING) for tag in entry.get("tags", []))
            or not isinstance(entry.get("source"), list)
            or not all(isinstance(item, dict) for item in entry["source"])
        ):
            raise ValueError("Invalid authoritative release entry: " + value)
    latest = max(versions, key=version_tuple)
    chosen = target or latest
    if chosen not in versions or not chosen.startswith(branch + "."):
        raise ValueError("Target must be an officially published stable release in installed branch " + branch)
    if version_tuple(chosen) < version_tuple(current):
        raise ValueError("Refusing a PHP patch-level downgrade")
    security_versions = [
        value for value in versions
        if "security" in releases[value].get("tags", []) and version_tuple(value) <= version_tuple(latest)
    ]
    security_floor = max(security_versions, key=version_tuple) if security_versions else None
    if security_floor and version_tuple(chosen) < version_tuple(security_floor):
        raise ValueError("Requested target predates the latest authoritative security release: " + security_floor)
    entry = releases[chosen]
    archive_name = "php-{}.tar.xz".format(chosen)
    archives = [item for item in entry.get("source", []) if item.get("filename") == archive_name]
    if len(archives) != 1 or not SHA256.match(archives[0].get("sha256", "")):
        raise ValueError("Authoritative release does not provide a unique tar.xz SHA-256")
    return dict(
        version=chosen, installed_version=current, branch=branch,
        latest_official=latest, latest_security_release=security_floor,
        tags=entry.get("tags", []), date=entry.get("date"),
        source=dict(
            url="https://www.php.net/distributions/" + archive_name,
            sha256=archives[0]["sha256"],
            checksum_authority="https://www.php.net/releases/index.php?json&version={}&max=100".format(branch),
        ),
        security_claim="Upstream published release only; no individual CVE or downstream backport assertion.",
    )


def repository_config(kind, root="/"):
    root = str(root)
    repositories = []
    diagnostics = []
    if kind == "rpm":
        defaults = {}
        main = os.path.join(root, "etc/yum.conf")
        if os.path.isfile(main):
            parser = configparser.RawConfigParser()
            parser.read(str(main))
            defaults = dict(parser.items("main")) if parser.has_section("main") else {}
        for path in sorted(glob.glob(os.path.join(root, "etc/yum.repos.d/*.repo"))):
            parser = configparser.RawConfigParser()
            try:
                parser.read(str(path))
            except configparser.Error as error:
                diagnostics.append("{}: {}".format(path, error))
                continue
            for section in parser.sections():
                if not re.match(r"[A-Za-z0-9_.:-]+\Z", section):
                    diagnostics.append("Skipped unsafe repository ID: " + section)
                    continue
                options = dict(defaults)
                options.update(dict(parser.items(section)))
                urls = []
                for key in ("baseurl", "mirrorlist", "metalink"):
                    for value in options.get(key, "").split():
                        parsed = urlsplit(value)
                        if parsed.scheme in ("http", "https") and parsed.hostname:
                            # Keep routing evidence, not repository credentials or query tokens.
                            urls.append(dict(kind=key, host=parsed.hostname.lower(), path=parsed.path))
                source_repository = bool(
                    re.search(r"(?:^|[-_])(?:sources?|srpms?)(?:$|[-_])", section, re.I)
                    or any(re.search(r"/(?:SRPMS|Sources?)(?:/|$)", item["path"], re.I) for item in urls)
                )
                signing_keys = []
                for value in options.get("gpgkey", "").split():
                    parsed = urlsplit(value)
                    if parsed.scheme in ("http", "https", "file") and not parsed.username and not parsed.password:
                        signing_keys.append(parsed.scheme + "://" + (parsed.hostname or "") + parsed.path)
                repositories.append(dict(
                    id=section, enabled=options.get("enabled", "1") == "1",
                    gpgcheck=options.get("gpgcheck", "0") == "1",
                    repo_gpgcheck=options.get("repo_gpgcheck", "0") == "1",
                    configuration_file=str(path),
                    endpoints=urls, source_repository=source_repository,
                    signing_keys=signing_keys,
                    availability="not-queried", security_support="unknown",
                ))
    else:
        paths = [os.path.join(root, "etc/apt/sources.list")]
        paths.extend(sorted(glob.glob(os.path.join(root, "etc/apt/sources.list.d/*.list"))))
        paths.extend(sorted(glob.glob(os.path.join(root, "etc/apt/sources.list.d/*.sources"))))
        for path in paths:
            if not os.path.isfile(path):
                continue
            content = read(path)
            repositories.append(dict(
                id=os.path.basename(path), configuration_file=str(path),
                source_entries_present=bool(re.search(r"(^deb-src\s|^Types:.*\bdeb-src\b)", content, re.MULTILINE)),
                explicit_trust_bypass=bool(re.search(r"trusted\s*=\s*yes|^Trusted:\s*yes", content, re.MULTILINE | re.IGNORECASE)),
                availability="cached-index-only", security_support="unknown",
            ))
    return dict(configured=repositories, diagnostics=diagnostics)


def source_repositories(discovery, configuration, repository_report):
    repositories = configuration["configured"]
    exact = {
        item["repository"] for item in repository_report.get("candidates", [])
        if item.get("source") == discovery["family"]["source_reference"]
    }
    provider_hosts = {
        endpoint["host"] for item in repositories if item["id"] in exact
        for endpoint in item.get("endpoints", [])
    }
    provider_files = {item.get("configuration_file") for item in repositories if item["id"] in exact} - {None}
    provider_keys = {
        key for item in repositories if item["id"] in exact for key in item.get("signing_keys", [])
    }
    vendor = " ".join(item.get("vendor", "") for item in discovery["family"].get("packages", []))
    provider_hosts.update(
        urlsplit(url.rstrip(">,)")).hostname.lower()
        for url in re.findall(r"https?://[^\s<]+", vendor)
        if urlsplit(url.rstrip(">,)")).hostname
    )
    source_ids, mixed_ids, excluded = [], [], []
    for item in repositories:
        identifier = item["id"]
        is_source = item.get("source_repository", bool(re.search(r"(?:^|[-_])(?:sources?|srpms?)(?:$|[-_])", identifier, re.I)))
        hosts = {endpoint["host"] for endpoint in item.get("endpoints", [])}
        related = (
            not (provider_hosts or provider_files or provider_keys)
            or bool(provider_hosts & hosts)
            or item.get("configuration_file") in provider_files
            or bool(provider_keys & set(item.get("signing_keys", [])))
        )
        if is_source and related:
            source_ids.append(identifier)
        elif identifier in exact:
            # An exact binary match may live in a mixed binary/source index.
            mixed_ids.append(identifier)
        else:
            excluded.append(identifier)
    enabled = {item["id"] for item in repositories if item.get("enabled")}
    ordering = lambda identifier: (identifier not in enabled, identifier)
    return dict(
        source_repositories=sorted(set(source_ids), key=ordering),
        mixed_repositories=sorted(set(mixed_ids), key=ordering),
        excluded_repositories=sorted(set(excluded)),
        provider_hosts=sorted(provider_hosts),
        explanation="Query source-labelled repositories belonging to the package provider, then exact-match binary indexes that may contain sources.",
    )


def source_location(location, source):
    filename(source)
    parsed = urlsplit(location)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Source location is not a credential-free HTTP(S) artifact URL")
    if any(ord(character) < 33 for character in location) or os.path.basename(parsed.path) != source:
        raise ValueError("Source location does not name the exact installed SRPM")
    # HTTP metadata can identify an artifact; only HTTPS may carry its bytes.
    return https("https:" + location[len(parsed.scheme) + 1:])


def provider_source_index(discovery, plan):
    # Remi publishes source packages separately from its installed binary .repo files.
    if "rpms.remirepo.net" in plan["provider_hosts"]:
        name = discovery["family"]["source_name"]
        filename(name)
        return "https://rpms.remirepo.net/SRPMS/repoview/" + name + ".html"
    return ""


def provider_packaging_tree(discovery, plan):
    if "rpms.remirepo.net" in plan["provider_hosts"]:
        version = discovery["runtime"]["version"]
        if not VERSION.match(version):
            raise ValueError("Cannot derive provider packaging tag from installed PHP version")
        name = discovery["family"]["source_name"]
        repo = name[:-4] if name.endswith("-php") else name
        filename(repo)
        return "https://git.remirepo.net/cgit/rpms/php/{}.git/tree/?h=v{}".format(repo, version)
    return ""


def provider_source_locations(path, url, source):
    if os.stat(path).st_size > 524288:
        raise ValueError("Provider package metadata exceeds 512 KiB")
    https(url)
    locations = set()

    class SourceLinks(HTMLParser):
        def handle_starttag(self, tag, attributes):
            if tag != "a":
                return
            for name, value in attributes:
                if name != "href" or not value:
                    continue
                location = urljoin(url, value)
                parsed = urlsplit(location)
                if parsed.hostname == urlsplit(url).hostname and os.path.basename(parsed.path) == source:
                    locations.add(source_location(location, source))

    parser = SourceLinks()
    parser.feed(read(path))
    parser.close()
    return sorted(locations)


def provider_packaging_files(path, url):
    if os.stat(path).st_size > 1048576:
        raise ValueError("Provider packaging metadata exceeds 1 MiB")
    https(url)
    page = urlsplit(url)
    tag = parse_qs(page.query).get("h", [""])[0]
    if not re.match(r"v\d+\.\d+\.\d+\Z", tag):
        raise ValueError("Provider packaging metadata needs an exact PHP packaging tag")
    files = {}

    class PackagingLinks(HTMLParser):
        def handle_starttag(self, tag_name, attributes):
            if tag_name != "a":
                return
            for name, value in attributes:
                if name != "href" or not value:
                    continue
                location = urljoin(url, value)
                parsed = urlsplit(location)
                if parsed.hostname != page.hostname or "/plain/" not in parsed.path:
                    continue
                if parse_qs(parsed.query).get("h", [""])[0] != tag:
                    continue
                relative = parsed.path.split("/plain/", 1)[1].strip("/")
                if "/" in relative or not re.match(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*\Z", relative):
                    continue
                files[relative] = https(location)

    parser = PackagingLinks()
    parser.feed(read(path))
    parser.close()
    if len(files) > 1000:
        raise ValueError("Provider packaging metadata declares too many files")
    return sorted(files.items())


def repositories(directory, status):
    directory = str(directory)
    discovery = load(os.path.join(directory, "discovery.json"))
    release = load(os.path.join(directory, "release.json"))
    config = load(os.path.join(directory, "repository-config.json"))
    reports = []
    candidates = []
    for state_file in sorted(glob.glob(os.path.join(directory, "repository-results/*.state"))):
        identifier, state = read(state_file).strip().split("\t")
        output = read(state_file[:-6] + ".out")
        error = read(state_file[:-6] + ".err")
        reports.append(dict(
            id=identifier, availability=state, diagnostics=error.strip(),
            metadata_output=output, security_support="unknown",
        ))
        if state != "available":
            continue
        if discovery["package_manager"] == "rpm":
            for line in output.splitlines():
                parts = line.split("\t")
                if len(parts) != 6 or parts[0] != discovery["runtime"]["owner"]:
                    continue
                name, epoch, version, revision, architecture, source = parts
                if architecture != discovery["architecture"]:
                    continue
                if not source.endswith(".src.rpm") or source_name(source) != discovery["family"]["source_name"]:
                    continue
                candidates.append(dict(
                    name=name, version=version, release=revision, epoch=epoch,
                    repository=identifier, architecture=architecture, source=source,
                ))
        else:
            versions = re.findall(r"^\s*(?:\*\*\*\s+)?(\S+)\s+\d+\s*$", output, re.MULTILINE)
            for version in versions:
                candidates.append(dict(name=discovery["runtime"]["owner"], version=version, repository=identifier))
    for candidate in candidates:
        try:
            version = upstream_version(candidate["version"])
            same_branch = version.startswith(release["branch"] + ".")
            reaches = same_branch and version_tuple(version) >= version_tuple(release["version"])
        except ValueError:
            reaches = False
        candidate["reaches_upstream_target"] = reaches
        candidate["security_status"] = "signature-and-advisory-review-required"
        candidate["transaction_validated"] = False
    preferred = [item for item in candidates if item["reaches_upstream_target"]]
    for report in reports:
        related = [item for item in candidates if item["repository"] == report["id"]]
        report["update_availability"] = (
            "upstream-target-candidate" if any(item["reaches_upstream_target"] for item in related)
            else "no-upstream-target-candidate" if report["availability"] == "available"
            else "unknown"
        )
    return dict(
        status=status, configuration=config, repositories=reports, candidates=candidates,
        preferred_route="native-package-review" if preferred else "authenticated-source-preparation",
        notes=[
            "APT uses existing package indices; freshness and per-mirror availability are not established.",
            "A reachable or numerically newer package is not proof of a security fix.",
            "Older downstream versions may contain backports; vendor advisory review is required.",
            "No native transaction is prepared or authorized in milestones 1-3.",
        ],
    )


def source_map(path, discovery):
    mapping = load(path)
    if mapping.get("installed_source") != discovery["family"]["source_reference"] or mapping.get("reviewed") is not True:
        raise ValueError("Source map must be explicitly reviewed and match the exact installed source reference")
    files = mapping.get("files")
    if not isinstance(files, list) or not files or len(files) > 1000:
        raise ValueError("Source map requires a bounded nonempty files list")
    names = set()
    for item in files:
        name = filename(item["name"])
        if name == "source-map.json" or name in names:
            raise ValueError("Duplicate/reserved source-map filename: " + name)
        names.add(name)
        if not SHA256.match(item["sha256"]):
            raise ValueError("Source map requires independently verified SHA-256 values")
        yield name, https(item["url"]), item["sha256"]


def rpm_signature(text):
    if re.search(r"NOKEY|NOT OK|NOTTRUSTED|BAD|UNSIGNED", text, re.IGNORECASE):
        raise ValueError("Source RPM signature is missing, invalid or not trusted")
    if not re.search(r"(?:RSA|DSA|PGP).*(?:signature|Signature).*:\s*OK", text, re.IGNORECASE):
        raise ValueError("A digest alone is not authentication: source RPM needs a valid signature against installed keys")


def control(text):
    fields = {}
    key = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line.strip()
        elif line:
            key, separator, value = line.partition(":")
            if not separator or key in fields:
                raise ValueError("Malformed/duplicate Debian source control field")
            fields[key] = value.strip()
    return fields


def dsc(text, source, version, url):
    fields = control(text)
    if fields.get("Source") != source or fields.get("Version") != version:
        raise ValueError("Signed .dsc does not match exact installed source name/version")
    if not fields.get("Checksums-Sha256"):
        raise ValueError("Signed .dsc lacks SHA-256 source checksums")
    names = set()
    for line in fields["Checksums-Sha256"].splitlines():
        if not line.strip():
            continue
        checksum, size, name = line.split()
        filename(name)
        if name in names or not SHA256.match(checksum) or not size.isdigit():
            raise ValueError("Invalid/duplicate signed .dsc member")
        names.add(name)
        yield name, https(urljoin(url, name)), checksum
    if not names:
        raise ValueError("Signed .dsc has no declared sources")


def dsc_url(text):
    urls = []
    for line in text.splitlines():
        if not line.startswith("'"):
            continue
        tokens = shlex.split(line)
        if tokens and urlsplit(tokens[0]).path.endswith(".dsc"):
            urls.append(https(tokens[0]))
    if len(urls) != 1:
        raise ValueError("APT did not identify one exact HTTPS .dsc; enable a compatible deb-src entry without trust bypasses")
    return urls[0]


def archive(path, version):
    prefix = "php-" + version
    listing = run(["tar", "-tJf", str(path)])
    if listing.returncode:
        raise ValueError("System tar cannot validate XZ archive: " + listing.stderr)
    members = listing.stdout.splitlines()
    header = prefix + "/main/php_version.h"
    for member in members:
        parts = member.rstrip("/").split("/")
        if not parts or parts[0] != prefix or ".." in parts or member.startswith("/"):
            raise ValueError("Unexpected path in authenticated PHP archive: " + member)
    if members.count(header) != 1:
        raise ValueError("PHP archive is missing its version header")
    result = run(["tar", "-xOJf", str(path), header])
    if result.returncode or not re.search(r'^#define PHP_VERSION "' + re.escape(version) + r'"$', result.stdout, re.MULTILINE):
        raise ValueError("PHP archive content version differs from release metadata")


def manifest(directory, kind):
    directory = str(directory)
    entries = []
    for path in sorted(descendants(directory)):
        if os.path.islink(path):
            raise ValueError("Symlinks are not permitted in prepared artifacts")
        if os.path.isfile(path) and path not in (os.path.join(directory, "manifest.json"), os.path.join(directory, "preparation.log")):
            entries.append(dict(
                path=os.path.relpath(path, directory), sha256=digest(path),
                size=os.stat(path).st_size,
            ))
    result = dict(
        schema_version=1, kind=kind, deployable=False,
        prepared_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        discovery=load(os.path.join(directory, "discovery.json")),
        release=load(os.path.join(directory, "release.json")),
        repositories=load(os.path.join(directory, "repositories.json")),
        packaging_authentication=(
            read(os.path.join(directory, "sources/packaging-trust")).strip()
            if kind in ("source-preparation", "isolated-rpm-build") else "not-acquired; native-candidate-review-only"
        ),
        files=entries,
        build=load(os.path.join(directory, "artifacts.json")) if kind == "isolated-rpm-build" else None,
        limitations=[
            "Local manifest checksums provide integrity, not a signature or independent authentication.",
            "Independent byte-for-byte build reproducibility has not been established.",
            (
                "Static RPM inspection only; runtime/extension/FPM/application health, native transaction, signing and rollback remain unvalidated."
                if kind == "isolated-rpm-build" else
                "No replacement binaries, build compatibility tests, dependency transaction, extension runtime tests or rollback artifacts exist."
            ),
        ],
    )
    dump(result, os.path.join(directory, "manifest.json"))


def verify_manifest(path):
    path = os.path.abspath(str(path))
    data = load(path)
    if (
        data.get("schema_version") != 1
        or data.get("kind") not in ("source-preparation", "native-update-review", "isolated-rpm-build")
        or data.get("deployable") is not False
    ):
        raise ValueError("Not a supported non-deployable source manifest")
    root = os.path.realpath(os.path.dirname(path))
    seen = set()
    for item in data["files"]:
        relative = item["path"]
        if os.path.isabs(relative) or ".." in relative.split("/") or relative in seen:
            raise ValueError("Unsafe/duplicate manifest path")
        seen.add(item["path"])
        target = os.path.join(root, relative)
        if os.path.islink(target) or not os.path.realpath(target).startswith(root + os.sep):
            raise ValueError("Manifest path escapes artifact directory")
        if not SHA256.match(item["sha256"]) or digest(target) != item["sha256"] or os.stat(target).st_size != item["size"]:
            raise ValueError("Prepared artifact integrity check failed: " + str(relative))
    if not seen:
        raise ValueError("Empty source manifest")
    for key in ("discovery", "release", "repositories"):
        relative = key + ".json"
        if relative not in seen or load(os.path.join(root, relative)) != data[key]:
            raise ValueError("Manifest embedded metadata differs from its checked file: " + relative)
    if data["kind"] == "isolated-rpm-build":
        if "artifacts.json" not in seen or data["build"] != load(os.path.join(root, "artifacts.json")):
            raise ValueError("Build artifact selection differs from the verified manifest")


def main(arguments):
    command, args = arguments[0], arguments[1:]
    if command == "manager":
        print(manager(*args))
    elif command == "inventory":
        dump(inventory(*args))
    elif command == "family-names":
        print("\n".join(sorted({item["name"] for item in load(args[0])["candidates"]})))
    elif command == "binary":
        print("\t".join(binary(load(args[0]), args[1])))
    elif command == "phpinfo":
        dump(phpinfo(sys.stdin.read()))
    elif command == "snapshot":
        dump(snapshot(*args))
    elif command == "finish-snapshot":
        finish_snapshot(*args)
    elif command == "libraries":
        dump(runtime_libraries(load(args[0])))
    elif command == "attach-libraries":
        data = load(args[0])
        data["runtime_libraries"] = load(args[1])
        dump(data, args[0])
    elif command == "compare":
        verify_manifest(args[1])
        actual = load(args[0])["compatibility"]
        expected = load(args[1])["discovery"].get("compatibility", {})
        matched = actual["complete"] and expected.get("complete") and actual["sha256"] == expected.get("sha256")
        dump(dict(compatible=bool(matched), actual=actual["sha256"], expected=expected.get("sha256"), deployable=False))
        if not matched:
            raise ValueError("Compatibility fingerprint differs or is incomplete; rebuild/review separately. Deployment remains disabled.")
    elif command == "store":
        print(store(*args))
    elif command == "https":
        https(*args)
    elif command == "filename":
        filename(*args)
    elif command == "partial":
        path, expected = args
        if not SHA256.match(expected):
            raise ValueError("Resuming requires an independently trusted SHA-256")
        if os.path.lexists(path):
            status = os.lstat(path)
            if (
                not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid()
                or status.st_nlink != 1 or status.st_mode & 0o022
                or status.st_size > 536870912
            ):
                raise ValueError("Untrusted or oversized partial download: " + path)
    elif command == "spec-name":
        print(next(item["name"] for item in load(args[0])["inputs"] if item["role"] == "spec"))
    elif command == "flatten-rpms":
        root = args[0]
        paths = [path for path in descendants(root) if path.endswith(".rpm") and os.path.isfile(path)]
        for path in paths:
            destination = os.path.join(root, os.path.basename(path))
            if os.path.islink(path):
                raise ValueError("Symlinked build artifact")
            if path != destination:
                if os.path.lexists(destination):
                    raise ValueError("Duplicate generated RPM filename")
                os.rename(path, destination)
    elif command == "cache-write":
        cache_write(*args)
    elif command == "cache-check":
        cache_check(*args)
    elif command == "select":
        dump(select(load(args[0]), load(args[1]), load(args[2]), args[3]))
    elif command == "repository-config":
        dump(repository_config(*args))
    elif command == "source-repositories":
        dump(source_repositories(load(args[0]), load(args[1]), load(args[2])))
    elif command == "source-location":
        print(source_location(*args))
    elif command == "provider-source-index":
        print(provider_source_index(load(args[0]), load(args[1])))
    elif command == "provider-packaging-tree":
        print(provider_packaging_tree(load(args[0]), load(args[1])))
    elif command == "provider-source-locations":
        for location in provider_source_locations(*args):
            print(location)
    elif command == "provider-packaging-files":
        for record in provider_packaging_files(*args):
            print("\t".join(record))
    elif command == "repository-ids":
        identifiers = sorted({
            item["id"] for item in load(args[0])["configured"]
            if item.get("enabled") or args[1:] == ["all"]
        })
        if identifiers:
            print("\n".join(identifiers))
    elif command == "repositories":
        dump(repositories(*args))
    elif command == "source-map":
        for record in source_map(args[0], load(args[1])):
            print("\t".join(record))
    elif command == "rpm-signature":
        rpm_signature(read(args[0]))
    elif command == "dsc-url":
        print(dsc_url(read(args[0])))
    elif command == "dsc":
        for record in dsc(read(args[0]), *args[1:]):
            print("\t".join(record))
    elif command == "archive":
        archive(*args)
    elif command == "manifest":
        manifest(*args)
    elif command == "verify-manifest":
        verify_manifest(*args)
    elif command == "build-id":
        discovery, release = (load(path) for path in args)
        print("{}-{}-{}".format(
            release["version"], discovery["architecture"], discovery["environment_fingerprint"][:12],
        ))
    elif command == "plan":
        discovery, release, repos = (load(path) for path in args)
        dump(dict(
            kind="source-preparation-plan", deployable=False, discovery=discovery,
            release=release, repositories=repos, persistent_changes=False,
        ))
    elif command == "get":
        value = load(args[0])
        for key in args[1].split("."):
            value = value[key]
        if isinstance(value, list):
            print("\n".join(str(item) for item in value))
        elif isinstance(value, (dict, bool)):
            dump(value)
        else:
            print(value)
    else:
        raise ValueError("Unknown metadata operation: " + command)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, IndexError, OSError, configparser.Error) as failure:
        print("[shift8-php] metadata error: " + str(failure), file=sys.stderr)
        sys.exit(5)
