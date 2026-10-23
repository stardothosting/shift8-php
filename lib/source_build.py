#!/usr/bin/env python
"""Generate a small source-first RPM spec from verified installation facts."""
from __future__ import print_function, unicode_literals

import io
import json
import os
import re
import shutil
import sys
import tarfile
import posixpath

import metadata as meta
import source_metadata as source
import build_metadata as build


COMPILED_PATHS_PHP = (
    'foreach (["PHP_PREFIX","PHP_BINDIR","PHP_LIBDIR","PHP_DATADIR","PHP_SYSCONFDIR","PHP_EXTENSION_DIR",'
    '"PHP_CONFIG_FILE_PATH","PHP_CONFIG_FILE_SCAN_DIR","PHP_MANDIR","PHP_LOCALSTATEDIR"] as $c) '
    'echo $c, "=", defined($c) ? constant($c) : "-", "\\n";'
)


def initialize(manifest_path, package, destination):
    meta.verify_manifest(manifest_path)
    manifest = meta.load(manifest_path)
    if (
        manifest["kind"] != "source-preparation" or not manifest.get("source_contract")
        or manifest["discovery"]["requested_package"] != package
    ):
        raise ValueError("Build manifest is not a source-first preparation for the selected package")
    origin = os.path.dirname(os.path.abspath(manifest_path))
    os.mkdir(destination, 0o700)
    for item in manifest["files"]:
        path = os.path.join(destination, item["path"])
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent, 0o700)
        shutil.copyfile(os.path.join(origin, item["path"]), path)
    meta.manifest(destination, "source-preparation")


def safe(value):
    if not isinstance(value, meta.STRING) or not re.match(r"[A-Za-z0-9_./:+,=@ -]+\Z", value):
        raise ValueError("Unsafe RPM/build input: " + str(value))
    return value


def archive(path, prefix):
    with tarfile.open(path, "r:gz") as stream:
        for item in stream:
            name = item.name.rstrip("/")
            if name.startswith("/") or ".." in name.split("/") or not (name == prefix or name.startswith(prefix + "/")):
                raise ValueError("Source archive path escapes its expected root: " + name)
            if not (item.isfile() or item.isdir() or item.issym() or item.islnk()):
                raise ValueError("Special files are not allowed in source archives")
            if item.issym() or item.islnk():
                target = item.linkname if item.islnk() else posixpath.join(posixpath.dirname(name), item.linkname)
                resolved = posixpath.normpath(target)
                if item.linkname.startswith("/") or not (resolved == prefix or resolved.startswith(prefix + "/")):
                    raise ValueError("Source archive link escapes its expected root: " + name)


def options(discovery):
    runtime = discovery["runtime"]
    prefix = os.path.dirname(os.path.dirname(runtime["executable"]))
    info = runtime["build"]
    plan = source.contract(discovery, dict(
        version=runtime["version"], branch=runtime["branch"], source={},
    ))
    if plan["blockers"]:
        raise ValueError("Source build contract blocked: " + json.dumps(plan["blockers"]))
    if runtime["branch"] != "8.3":
        raise ValueError("Source-first binary builds are currently gated to PHP 8.3")
    compiled = plan["layout"]["paths"]["compiled"]
    result = [
        "--disable-all", "--enable-cli", "--enable-cgi", "--disable-phpdbg", "--without-pear",
        "--prefix=" + prefix, "--exec-prefix=" + prefix, "--bindir=" + prefix + "/bin",
        "--sbindir=" + prefix + "/sbin", "--libdir=" + compiled["PHP_LIBDIR"], "--with-libdir=lib64",
        "--datadir=" + compiled["PHP_DATADIR"], "--mandir=" + compiled["PHP_MANDIR"],
        "--localstatedir=" + compiled["PHP_LOCALSTATEDIR"], "--sysconfdir=" + compiled["PHP_SYSCONFDIR"],
        "--with-config-file-path=" + info["Configuration File (php.ini) Path"],
        "--with-config-file-scan-dir=" + info["Scan this dir for additional .ini files"],
        "--enable-filter", "--with-libxml",
    ]
    if discovery["family"]["fpm_binaries"]:
        result.extend(["--enable-fpm", "--with-fpm-systemd", "--with-fpm-acl"])
    if plan.get("apache_module"):
        result.append("--with-apxs2=/usr/bin/apxs")
    if runtime["zts"]:
        result.append("--enable-zts")
    for feature in plan["features"]:
        name, option = feature["name"], feature["configure_option"]
        suffix = "=shared" if feature["linkage"] == "shared" else ""
        if name == "gd":
            option = "--enable-gd"
        if name in ("mysqli", "pdo_mysql"):
            suffix = "=shared,mysqlnd" if suffix else "=mysqlnd"
        if name == "imap":
            result.extend(["--with-imap-ssl", "--with-kerberos"])
        if name == "pdo_odbc":
            raise ValueError("PDO ODBC needs a reviewed provider adapter")
        result.append(option + suffix)
    for capability in plan["capabilities"]:
        if capability["feature"] == "gd" and capability["required"] is True:
            flag = {"jpeg": "--with-jpeg", "freetype": "--with-freetype", "webp": "--with-webp",
                    "avif": "--with-avif"}.get(capability["name"])
            if flag:
                result.append(flag)
    if any(item["name"] == "opcache" for item in plan["features"]):
        # Preserve JIT only if known; its availability is checked separately.
        result.append("--disable-opcache-jit")
    if len(set(result)) != len(result) or "--" in result:
        raise ValueError("Malformed or duplicate source configure arguments")
    return result, plan


def revision(discovery, value, target):
    for item in discovery["family"]["packages"]:
        if item["role"] == "core":
            value = value.replace(item["version"] + "-" + item["release"], target + "-1.el7.shift8")
    return value


def generate(manifest_path, environment_path, destination):
    meta.verify_manifest(manifest_path)
    original = os.path.dirname(os.path.abspath(manifest_path))
    manifest = meta.load(manifest_path)
    if manifest["kind"] != "source-preparation" or not manifest.get("source_contract"):
        raise ValueError("Source builds require prepare --source-first")
    discovery, release = manifest["discovery"], manifest["release"]
    arguments, plan = options(discovery)
    environment = meta.load(environment_path)
    if environment.get("reviewed") is not True:
        raise ValueError("Source build environment requires explicit review")
    for item in [environment["mock_config"]] + environment.get("trust_files", []):
        build.pinned(item)
    compiler = environment["compiler"]
    jobs = environment["jobs"]
    if not isinstance(jobs, int) or not 1 <= jobs <= 64:
        raise ValueError("Explicit jobs must be between 1 and 64")
    openssl = environment["openssl"]
    if not re.match(r"3\.5\.\d+\Z", openssl["version"]) or not openssl.get("maintenance_reviewed"):
        raise ValueError("A reviewed maintained OpenSSL 3.5 LTS source is required")
    build.pinned(openssl)
    meta.https(openssl["url"])
    archive(openssl["path"], "openssl-" + openssl["version"])
    prefix = "/opt/shift8-php/deps/openssl-" + openssl["version"]
    source_name = discovery["family"]["source_name"]
    packages = [item for item in discovery["family"]["packages"] if item["role"] == "core"]
    if not packages or not all(item["architecture"] == discovery["architecture"] for item in packages):
        raise ValueError("Cannot preserve the exact installed core package architecture set")
    for value in arguments:
        safe(value)
    for item in packages:
        safe(item["name"])
        for path in item["files"]:
            if "%" in path or any(c in path for c in "\n\r\t"):
                raise ValueError("Unsafe installed file name: " + path)
    destination = os.path.abspath(destination)
    os.mkdir(destination, 0o700)
    profile = dict(
        schema_version=1, reviewed=True, configuration_closure_reviewed=environment["configuration_closure_reviewed"],
        signature_policy_reviewed=environment["signature_policy_reviewed"],
        compatibility_sha256=discovery["compatibility"]["sha256"],
        installed_source=discovery["family"]["source_reference"], target=release["version"],
        compiler=compiler, jobs=jobs, mock_config=environment["mock_config"],
        trust_files=environment["trust_files"], inputs=[],
    )
    source_files = [
        ("openssl-" + openssl["version"] + ".tar.gz", openssl["path"], "source", "Reviewed private OpenSSL source and digest"),
        ("source-test-portability.patch", os.path.join(os.path.dirname(os.path.dirname(__file__)), "patches/php-test-portability.patch"),
         "patch", "Explicit UTF-8 test locale and OpenSSL library/reason assertions; no test skips"),
        ("configure-source.py", os.path.join(os.path.dirname(__file__), "configure_source.py"),
         "source", "Array-based configure runner verifies target support and exact config.nice arguments"),
        ("stage-source.py", os.path.join(os.path.dirname(__file__), "stage_source.py"),
         "source", "Restrict INSTALL_ROOT payload to exact installed runtime files and private dependencies"),
    ]
    for name, path, role, review in source_files:
        target = os.path.join(destination, name)
        shutil.copyfile(path, target)
        profile["inputs"].append(dict(name=name, path=target, sha256=meta.digest(target), role=role, review=review))
    configuration = os.path.join(destination, "configure-arguments.json")
    meta.dump(arguments, configuration)
    profile["inputs"].append(dict(name="configure-arguments.json", path=configuration, sha256=meta.digest(configuration),
                                  role="source", review="Exact normalized configuration argv; no historical command replay"))
    updated_files = set()
    for item in packages:
        for path in item["files"]:
            basename = os.path.basename(path)
            if (
                path.startswith(discovery["runtime"]["extension_dir"] + "/") and path.endswith(".so")
                or path in (discovery["runtime"]["executable"],) + tuple(discovery["family"]["fpm_binaries"])
                or plan.get("apache_module") and path == plan["apache_module"]["path"]
                or path.startswith(plan["layout"]["paths"]["prefix"] + "/bin/") and basename in ("php-cgi", "phar", "phar.phar", "phpize", "php-config")
            ):
                updated_files.add(path)
    payload = os.path.join(destination, "payload.json")
    meta.dump(dict(files=sorted(updated_files), private_prefix=prefix), payload)
    profile["inputs"].append(dict(name="payload.json", path=payload, sha256=meta.digest(payload), role="source"))
    # BuildRequires are capabilities, not guessed repository package names.
    requires = ["gcc", "gcc-c++", "make", "binutils", "chrpath", "diffutils", "perl", "perl(Time::Piece)", "perl(IPC::Cmd)", "perl(Test::More)",
                "perl(Test::Harness)", "perl(Math::BigInt)", "perl(Digest::SHA)",
                "pkgconfig", "systemd-devel", "libacl-devel",
                "pkgconfig(libxml-2.0)", "pkgconfig(zlib)"]
    capabilities = {
        "bzip2": "bzip2-devel", "curl": "pkgconfig(libcurl)", "gd": "libjpeg-turbo-devel",
        "ldap": "openldap-devel", "libsodium": "pkgconfig(libsodium)",
        "libxslt": "pkgconfig(libxslt)", "oniguruma": "pkgconfig(oniguruma)",
        "readline": "readline-devel", "snmp": "net-snmp-devel", "sqlite": "pkgconfig(sqlite3)",
        "aspell": "aspell-devel", "freetds": "freetds-devel", "imap": "libc-client-devel",
        "icu": "pkgconfig(icu-uc)", "gmp": "gmp-devel", "libffi": "pkgconfig(libffi)",
        "postgresql": "postgresql-devel", "tidy": "libtidy-devel", "libzip": "pkgconfig(libzip)",
    }
    for item in plan["dependencies"]:
        if item["name"] == "openssl":
            continue
        if item["name"] not in capabilities and item["name"] not in ("libxml2", "zlib"):
            raise ValueError("No reviewed source dependency adapter: " + item["name"])
        if item["name"] in capabilities:
            requires.append(capabilities[item["name"]])
    for item in plan["capabilities"]:
        if item["feature"] == "gd" and item["required"] is True:
            capability = {"freetype": "pkgconfig(freetype2)", "webp": "pkgconfig(libwebp)",
                          "avif": "pkgconfig(libavif)", "jpeg": "libjpeg-turbo-devel",
                          "png": "libpng-devel"}.get(item["name"])
            if capability:
                requires.append(capability)
    requires.append("libpng-devel")
    if plan.get("apache_module"):
        requires.extend(["httpd-devel", "httpd"])
    if any(item["name"] == "imap" for item in plan["dependencies"]):
        requires.append("pkgconfig(krb5)")
    lines = [
        "Name: " + source_name, "Version: " + release["version"], "Release: 1.el7.shift8",
        "Summary: Source-first PHP runtime preserving installed paths", "License: PHP",
        "Source0: php-" + release["version"] + ".tar.xz",
        "Source1: openssl-" + openssl["version"] + ".tar.gz",
        "Source2: configure-arguments.json", "Source3: configure-source.py", "Patch0: source-test-portability.patch",
        "Source4: stage-source.py", "Source5: payload.json",
        "%global debug_package %{nil}", "%global __brp_mangle_shebangs %{nil}",
    ]
    lines.extend("BuildRequires: " + value for value in sorted(set(requires)))
    for item in packages:
        if item["name"] != source_name:
            lines.extend(["", "%package -n " + item["name"], "Summary: Preserved PHP package " + item["name"]])
        if item["epoch"] not in ("0", "(none)", ""):
            lines.append("Epoch: " + safe(item["epoch"]))
        for provide in item["provides"]:
            if "%" in provide or any(char in provide for char in "\r\n"):
                raise ValueError("Unsafe installed RPM provide")
            lines.append("Provides: " + revision(discovery, provide, release["version"]))
        for requirement in item["requires"]:
            if "%" in requirement or any(char in requirement for char in "\r\n"):
                raise ValueError("Unsafe installed RPM requirement")
            # Keep original ABI/capability requirements, regenerate ELF requirements from payload.
            if requirement.startswith(("rpmlib(", "lib", "rtld(")):
                continue
            lines.append("Requires: " + revision(discovery, requirement, release["version"]))
    for item in packages:
        lines.extend(["", "%description" + (" -n " + item["name"] if item["name"] != source_name else ""),
                      "Same-branch source update. Existing configuration and service files are preserved."])
    if source_name not in [item["name"] for item in packages]:
        lines.extend(["", "%description", "Source package for the installed PHP replacement subpackages."])
    lines.extend([
        "", "%prep", "%setup -q -n php-%{version}", "%patch -P0 -p1 -b .shift8-test",
        "", "%build",
        "tar -xzf %{SOURCE1} -C ..",
        "pushd ../openssl-" + openssl["version"],
        "./Configure linux-x86_64 --prefix=" + prefix + " --openssldir=/etc/pki/tls --libdir=lib64 shared -Wl,-rpath=" + prefix + "/lib64",
        "make -j" + str(jobs), "make test HARNESS_JOBS=" + str(jobs), "make install_sw DESTDIR=%{_builddir}/openssl-stage",
        "popd",
        "export PKG_CONFIG_PATH=%{_builddir}/openssl-stage" + prefix + "/lib64/pkgconfig",
        "export OPENSSL_CFLAGS=-I%{_builddir}/openssl-stage" + prefix + "/include",
        "export OPENSSL_LIBS='-L%{_builddir}/openssl-stage" + prefix + "/lib64 -lssl -lcrypto -Wl,-rpath," + prefix + "/lib64'",
        "export LD_LIBRARY_PATH=%{_builddir}/openssl-stage" + prefix + "/lib64",
        "export EXTENSION_DIR=" + discovery["runtime"]["extension_dir"],
        "/usr/bin/python %{SOURCE3} %{SOURCE2}",
        "make -j" + str(jobs), "",
        "%install", "rm -rf %{buildroot}",
    ])
    if plan.get("apache_module"):
        # apxs -a edits an httpd.conf under INSTALL_ROOT; give it a throwaway one (the stage step discards it).
        lines.extend([
            'conf="$(/usr/bin/apxs -q SYSCONFDIR)"', 'mkdir -p "%{buildroot}$conf"',
            "printf '\\nLoadModule placeholder_module modules/mod_placeholder.so\\n' > \"%{buildroot}$conf/httpd.conf\"",
        ])
    lines.append("make install INSTALL_ROOT=%{buildroot}")
    if plan.get("apache_module"):
        module = plan["apache_module"]["path"]
        lines.extend([
            "module=$(find %{buildroot} -type f -name 'libphp*.so' | head -n 1)", 'test -n "$module"',
            "mkdir -p %{buildroot}" + os.path.dirname(module),
            '[ "$module" = "%{buildroot}' + module + '" ] || cp "$module" "%{buildroot}' + module + '"',
        ])
    lines.extend([
        "mkdir -p %{buildroot}" + prefix,
        "cp -a %{_builddir}/openssl-stage" + prefix + "/. %{buildroot}" + prefix + "/",
        "mkdir -p %{buildroot}" + discovery["runtime"]["extension_dir"],
        "find %{buildroot} -type f -name '*.so' -path '*/extensions/*' -exec cp {} %{buildroot}" +
        discovery["runtime"]["extension_dir"] + "/ \\;",
        # PHP records the build-time staging copy of OpenSSL as RPATH; point every binary at the final private prefix.
        "for file in $(find %{buildroot} -type f); do",
        '  readelf -h "$file" >/dev/null 2>&1 || continue',
        "  stale=$(readelf -d \"$file\" 2>/dev/null | grep -c 'R\\(UN\\)\\?PATH.*%{_builddir}/openssl-stage' || true)",
        '  if [ "$stale" != "0" ]; then chrpath -r "' + prefix + '/lib64" "$file"; fi',
        "done",
        "/usr/bin/python %{SOURCE4} %{buildroot} %{SOURCE5}",
        # The staged binary must report exactly the compiled directory contract of the installed runtime.
        "LD_LIBRARY_PATH=%{_builddir}/openssl-stage" + prefix + "/lib64 %{buildroot}" + discovery["runtime"]["executable"] +
        " -n -r '" + COMPILED_PATHS_PHP + "' > %{_builddir}/compiled-paths.actual",
        "printf '%%s\\n' " + " ".join("'" + key + "=" + safe(value) + "'" for key, value in sorted(
            plan["layout"]["paths"]["compiled"].items())) + " > %{_builddir}/compiled-paths.expected",
        "diff %{_builddir}/compiled-paths.expected %{_builddir}/compiled-paths.actual",
        "",
        "%check", "export LC_CTYPE=${LC_CTYPE:?Measured UTF-8 locale required}",
        "export NO_INTERACTION=1 REPORT_EXIT_STATUS=1 SKIP_ONLINE_TESTS=1 SKIP_IO_CAPTURE_TESTS=1",
        "export LD_LIBRARY_PATH=%{_builddir}/openssl-stage" + prefix + "/lib64",
        # The Makefile loads shared modules alphabetically; mysqli/pdo_* need their providers loaded first.
        'extensions=""',
        "for first in mysqlnd pdo dom; do",
        '  if [ -f modules/$first.so ]; then extensions="$extensions -d extension=$first.so"; fi',
        "done",
        "for module in modules/*.so; do",
        "  name=$(basename $module)",
        "  case $name in",
        "    mysqlnd.so|pdo.so|dom.so|dl_test.so) ;;",
        '    opcache.so) extensions="$extensions -d zend_extension=$PWD/modules/opcache.so" ;;',
        '    *) extensions="$extensions -d extension=$name" ;;',
        "  esac",
        "done",
        "make test PHP_TEST_SHARED_EXTENSIONS=\"$extensions\" TESTS='-j" + str(jobs) + "'",
    ])
    # Package only the exact runtime/update surface; preserve unrelated owned paths as ghost entries.
    for item in packages:
        lines.extend(["", "%files" + (" -n " + item["name"] if item["name"] != source_name else "")])
        for path in sorted(set(item["files"])):
            if path in updated_files:
                lines.append(path)
            else:
                lines.append("%ghost " + ("%config(noreplace) " if path in item["configuration_files"] else "") + path)
        if item["name"].endswith("-common"):
            lines.append(prefix + "/")
    spec = os.path.join(destination, "shift8-source.spec")
    with io.open(spec, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    profile["inputs"].append(dict(name="shift8-source.spec", path=spec, role="spec", sha256=meta.digest(spec),
                                  review="Generated from installed package/ABI/path contract; original config/service bytes not included."))
    meta.dump(dict(arguments=arguments, openssl=openssl, contract=plan, dependency_capabilities=sorted(set(requires)),
                   limitations=["Private OpenSSL is selected; other EL7 dependencies still need lifecycle review.",
                                "Source-built replacement packages must be signed and isolated-tested before deployment."]),
              os.path.join(destination, "source-build-plan.json"))
    path = os.path.join(destination, "profile.json")
    meta.dump(profile, path)
    build.profile(path, discovery, release)
    print(path)


if __name__ == "__main__":
    try:
        if sys.argv[1] == "initialize":
            initialize(*sys.argv[2:])
        else:
            generate(*sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("[shift8-php] source build error: " + str(error), file=sys.stderr)
        sys.exit(7)
