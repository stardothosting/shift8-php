#!/usr/bin/env python
"""Normalize installed PHP contracts without evaluating vendor build commands."""
from __future__ import print_function, unicode_literals

import hashlib
import json
import os
import re
import sys

import metadata as meta


# These describe upstream PHP features, not a vendor's historical spec.
FEATURES = {
    "bcmath": ("--enable-bcmath", []), "bz2": ("--with-bz2", ["bzip2"]),
    "calendar": ("--enable-calendar", []), "ctype": ("--enable-ctype", []),
    "curl": ("--with-curl", ["curl"]), "dba": ("--enable-dba", ["db"]),
    "dom": ("--enable-dom", ["libxml2"]), "enchant": ("--with-enchant", ["enchant"]),
    "exif": ("--enable-exif", []), "ffi": ("--with-ffi", ["libffi"]),
    "fileinfo": ("--enable-fileinfo", []), "ftp": ("--enable-ftp", []),
    "gd": ("--enable-gd", ["gd"]), "gettext": ("--with-gettext", []),
    "gmp": ("--with-gmp", ["gmp"]), "iconv": ("--with-iconv", []),
    "imap": ("--with-imap", ["imap"]), "intl": ("--enable-intl", ["icu"]),
    "ldap": ("--with-ldap", ["ldap"]), "mbstring": ("--enable-mbstring", ["oniguruma"]),
    "mysqli": ("--with-mysqli", []), "mysqlnd": ("--enable-mysqlnd", []),
    "odbc": ("--with-unixODBC", ["odbc"]), "opcache": ("--enable-opcache", []),
    "openssl": ("--with-openssl", ["openssl"]), "pcntl": ("--enable-pcntl", []),
    "pdo": ("--enable-pdo", []), "pdo_dblib": ("--with-pdo-dblib", ["freetds"]),
    "pdo_firebird": ("--with-pdo-firebird", ["firebird"]),
    "pdo_mysql": ("--with-pdo-mysql", []), "pdo_odbc": ("--with-pdo-odbc", ["odbc"]),
    "pdo_pgsql": ("--with-pdo-pgsql", ["postgresql"]), "pdo_sqlite": ("--with-pdo-sqlite", ["sqlite"]),
    "pgsql": ("--with-pgsql", ["postgresql"]), "phar": ("--enable-phar", []),
    "posix": ("--enable-posix", []), "pspell": ("--with-pspell", ["aspell"]),
    "readline": ("--with-readline", ["readline"]), "session": ("--enable-session", []),
    "shmop": ("--enable-shmop", []), "simplexml": ("--enable-simplexml", ["libxml2"]),
    "snmp": ("--with-snmp", ["snmp"]), "soap": ("--enable-soap", ["libxml2"]),
    "sockets": ("--enable-sockets", []), "sodium": ("--with-sodium", ["libsodium"]),
    "sqlite3": ("--with-sqlite3", ["sqlite"]), "sysvmsg": ("--enable-sysvmsg", []),
    "sysvsem": ("--enable-sysvsem", []), "sysvshm": ("--enable-sysvshm", []),
    "tidy": ("--with-tidy", ["tidy"]), "tokenizer": ("--enable-tokenizer", []),
    "xml": ("--enable-xml", ["libxml2"]), "xmlreader": ("--enable-xmlreader", ["libxml2"]),
    "xmlwriter": ("--enable-xmlwriter", ["libxml2"]), "xsl": ("--with-xsl", ["libxslt", "libxml2"]),
    "zip": ("--with-zip", ["libzip"]), "zlib": ("--with-zlib", ["zlib"]),
}
BUILTINS = {"core", "date", "filter", "hash", "json", "libxml", "pcre", "random", "reflection", "spl", "standard"}


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def absolute(path):
    if (
        not isinstance(path, meta.STRING) or not path.startswith("/") or path == "/"
        or os.path.normpath(path) != path or any(char in path for char in "\t\r\n\0")
    ):
        raise ValueError("An unambiguous absolute installation path is required: " + str(path))
    return path


def module_name(value):
    name = value.lower()
    return "opcache" if name == "zend opcache" else name


def attach_fpm(path, directory):
    data = meta.load(path)
    binaries = data["family"]["fpm_binaries"]
    facts = []
    for index, binary in enumerate(binaries, 1):
        info = meta.load(os.path.join(directory, str(index) + ".json"))
        facts.append(dict(executable=binary, build=info))
    data["fpm_runtime"] = facts
    meta.dump(data, path)


def contract(discovery, release):
    runtime, family = discovery["runtime"], discovery["family"]
    build = runtime["build"]
    blockers, checks = [], []

    def block(code, message, evidence=None):
        blockers.append(dict(code=code, message=message, evidence=evidence))

    platform = discovery["distribution"]
    point = platform.get("version", "")
    evidence = platform.get("point_release_evidence", "")
    if platform.get("id") != "centos" or not (
        point.startswith("7.9") or (point == "7" and re.search(r"\b7\.9\.2009\b", evidence))
    ):
        block("platform", "Source-first contracts currently require CentOS 7.9.", platform)
    if discovery["package_manager"] != "rpm" or discovery["architecture"] != "x86_64":
        block("architecture", "The initial source-first target is RPM-managed x86_64.")
    branch = runtime["branch"]
    if branch not in ("8.2", "8.3", "8.4", "8.5"):
        block("php-branch", "No source-first adapter is validated for this PHP branch.", branch)
    if release["branch"] != branch or meta.version_tuple(release["version"]) < meta.version_tuple(runtime["version"]):
        raise ValueError("Source-first targets must stay in the running branch and cannot downgrade it")

    executable = absolute(runtime["executable"])
    parallel = re.match(r"/opt/remi/(php[0-9]+)/root/usr/bin/php\Z", executable)
    if executable == "/usr/bin/php":
        layout_id, prefix = "system", "/usr"
    elif parallel:
        layout_id, prefix = "remi-parallel", "/opt/remi/" + parallel.group(1) + "/root/usr"
    else:
        layout_id, prefix = "custom", os.path.dirname(os.path.dirname(executable))
        block("layout", "Custom/control-panel paths need an explicit layout adapter.", executable)
    paths = dict(cli=executable, prefix=prefix, extension_dir=absolute(runtime["extension_dir"]),
                 fpm=[absolute(item) for item in family["fpm_binaries"]])
    for key, fact in (("ini_dir", "Configuration File (php.ini) Path"),
                      ("ini_scan_dir", "Scan this dir for additional .ini files")):
        value = build.get(fact)
        if not value or value in ("(none)", "no value") or ":" in value:
            block("configuration-path", "Cannot normalize the compiled " + key + ".", value)
        else:
            paths[key] = absolute(value)
    loaded = build.get("Loaded Configuration File")
    if loaded and loaded != "(none)":
        paths["loaded_ini"] = absolute(loaded)
    configure = ["--prefix=" + prefix, "--bindir=" + os.path.dirname(executable)]
    if paths["fpm"]:
        expected_fpm = prefix + "/sbin/php-fpm"
        if paths["fpm"] != [expected_fpm]:
            block("fpm-layout", "Multiple or nonstandard FPM executables require a separate adapter.", paths["fpm"])
        configure.extend(["--enable-fpm", "--sbindir=" + os.path.dirname(paths["fpm"][0])])
    for key, option in (("ini_dir", "--with-config-file-path="), ("ini_scan_dir", "--with-config-file-scan-dir=")):
        if key in paths:
            configure.append(option + paths[key])
    if runtime["zts"]:
        configure.append("--enable-zts")
    for item in discovery.get("fpm_runtime", []):
        info = item["build"]
        fields = {"PHP Version": runtime["version"], "PHP API": runtime["php_api"],
                  "PHP Extension": runtime["module_api"], "Zend Extension": runtime["zend_extension_api"]}
        for key, expected in fields.items():
            if info.get(key) != expected:
                block("fpm-runtime-drift", "FPM and CLI disagree on " + key + ".", item["executable"])
        for key in ("Configuration File (php.ini) Path", "Scan this dir for additional .ini files"):
            if info.get(key) != build.get(key):
                block("fpm-configuration", "FPM has distinct compiled configuration paths.", item["executable"])
        if info.get("Thread Safety") != build.get("Thread Safety"):
            block("fpm-runtime-drift", "FPM and CLI thread-safety modes differ.", item["executable"])
    if paths["fpm"] and len(discovery.get("fpm_runtime", [])) != len(paths["fpm"]):
        block("fpm-uninspected", "FPM must be inspected separately from CLI.")

    shared, extension_packages, preserved = {}, [], []
    for package in family["packages"]:
        files = package["files"]
        if any(re.search(r"/(?:httpd|apache2)/.*(?:libphp|php[0-9]).*\.so$", path) for path in files):
            block("apache-module", "An installed Apache PHP module requires an Apache/APXS adapter.", package["name"])
        if package["role"] != "core":
            extension_packages.append(dict(name=package["name"], version=package["version"],
                                           release=package["release"], files=[p for p in files if p.endswith(".so")],
                                           decision="retain-only-after-ABI-linkage-and-load-tests"))
        else:
            for path in files:
                if path.startswith(paths["extension_dir"] + "/") and path.endswith(".so"):
                    shared[module_name(os.path.basename(path)[:-3])] = dict(path=path, owner=package["name"])
        preserved.extend(package.get("configuration_files", []))
    extra_modules = set()
    for item in extension_packages:
        extra_modules.update(module_name(os.path.basename(path)[:-3]) for path in item["files"])
    required = {module_name(item) for item in runtime["loaded_extensions"]}
    features, dependencies = [], {"libxml2", "openssl"}
    for name in sorted(required | set(shared) | extra_modules):
        if name in extra_modules:
            if name in FEATURES:
                dependencies.update(FEATURES[name][1])
            continue
        if name in BUILTINS:
            continue
        if name not in FEATURES:
            block("unknown-extension", "Loaded extension has no upstream feature/ownership mapping.", name)
            continue
        option, libraries = FEATURES[name]
        features.append(dict(name=name, linkage="shared" if name in shared else "builtin",
                             installed_file=shared.get(name), configure_option=option, required_loaded=name in required))
        dependencies.update(libraries)
    for feature in features:
        option = feature["configure_option"]
        if feature["linkage"] == "shared":
            option += "=shared"
        configure.append(option)
    capabilities = []
    for feature, definitions in (
        ("zip", (("aes-128", "AES-128 encryption"), ("aes-192", "AES-192 encryption"), ("aes-256", "AES-256 encryption"))),
        ("gd", (("jpeg", "JPEG Support"), ("png", "PNG Support"), ("freetype", "FreeType Support"),
                ("webp", "WebP Support"), ("avif", "AVIF Support"))),
    ):
        if feature not in required | set(shared) | extra_modules:
            continue
        for name, key in definitions:
            raw = build.get(key)
            observed = None
            if isinstance(raw, meta.STRING):
                if raw.lower() in ("enabled", "yes"):
                    observed = True
                elif raw.lower() in ("disabled", "no"):
                    observed = False
            capabilities.append(dict(
                feature=feature, name=name, observed=observed,
                required=True if observed is True else (False if observed is False else None),
                evidence=dict(source="allowlisted-phpinfo; final-runtime-probe-required", key=key, value=raw),
                policy="Preserve observed enabled capabilities; unknown/application requirements need explicit resolution.",
            ))
    if extension_packages:
        checks.append("Verify/rebuild every separately packaged extension with the final runtime and private dependency graph.")
    if discovery.get("installation_state", {}).get("package_runtime_drift"):
        block("package-runtime-drift", "RPM metadata and running files differ; capture actual recovery files before a managed transaction.",
              discovery["installation_state"])
    libraries = discovery.get("runtime_libraries", {})
    if not libraries.get("complete"):
        block("library-inventory", "Library closure is incomplete; do not infer binary compatibility.", libraries.get("diagnostics", []))
    # These versions are observations, not a claim of maintenance or security support.
    versions = {key: build[key] for key in (
        "OpenSSL Library Version", "OpenSSL Header Version", "libXML Compiled Version",
        "libXML Loaded Version", "ICU version", "cURL Information", "SQLite Library", "libzip version",
        "Libzip version", "Libzip headers version", "Libzip library version",
    ) if key in build}
    requirements = []
    observations = {
        "openssl": build.get("OpenSSL Library Version"), "libxml2": build.get("libXML Loaded Version"),
        "icu": build.get("ICU version"), "curl": build.get("cURL Information"),
        "sqlite": build.get("SQLite Library"),
        "libzip": build.get("Libzip library version") or build.get("Libzip version") or build.get("libzip version"),
    }
    for name in sorted(dependencies):
        old_openssl = name == "openssl" and re.match(r"OpenSSL (?:0\.|1\.)", observations.get(name) or "")
        requirements.append(dict(
            name=name, decision="private-source-required" if old_openssl else "resolve-in-isolated-builder",
            observed=observations.get(name), maintenance_status="not-established",
            reason="Legacy OpenSSL is not an automatically approved security dependency." if old_openssl else
                   "A runtime version/load observation does not establish suitable build headers, maintenance or feature support.",
            policy="reuse-only-with-maintenance-ABI-and-feature-evidence; otherwise pinned-private-source",
            prohibit_system_replacement=name in ("openssl", "libxml2"),
        ))
    checks.extend([
        "Resolve an authenticated, maintained dependency closure; inspect overlapping OpenSSL/libxml consumers including PECL, curl and LDAP.",
        "Probe ZIP encryption and GD formats in the final CLI/FPM runtime; extension loading alone does not establish feature parity.",
        "Compare final modules, ABI/ZTS, configuration and runtime paths; preserve configuration bytes, ownership, modes and SELinux labels.",
        "Validate real FPM pools, web routing and application health without altering production during staging.",
        "Capture actual installed recovery payload and rehearse the exact upgrade/rollback transaction.",
        "Sign/approve final RPMs before production installation; never overwrite RPM-owned files without a managed transaction.",
    ])
    build_inputs = dict(platform="centos-7.9", architecture=discovery["architecture"],
                        target=release["version"], upstream=release["source"], layout=paths,
                        abi={key: runtime[key] for key in ("php_api", "module_api", "zend_extension_api", "zts")},
                        features=features, capabilities=capabilities,
                        dependencies=[dict(name=item["name"], policy=item["policy"]) for item in requirements])
    return dict(
        schema_version=1, strategy="source-first-managed-replacement",
        status="blocked" if blockers else "needs-isolated-build-resolution",
        deployable=False, build_ready=False, historical_packaging_required=False,
        installed_runtime=runtime["version"], target=release["version"], branch=branch,
        installation_state=discovery.get("installation_state"),
        layout=dict(adapter=layout_id, paths=paths), features=features, capabilities=capabilities,
        configure_arguments=configure, configure_arguments_executable=False,
        dependencies=requirements, observed_library_versions=versions,
        separately_packaged_extensions=extension_packages,
        preserve=dict(configuration_files=sorted(set(preserved)), services=discovery.get("services", [])),
        blockers=blockers, required_checks=checks,
        source_contract_sha256=identity(build_inputs),
        source_contract_identity_scope="Pre-resolution contract only; NOT a reusable binary/cache identity. Final dependency/compiler/patch bytes must be included.",
        deployment_fingerprint=discovery.get("compatibility", {}).get("sha256"),
        original_configure_command_policy="Diagnostic input only; never evaluated or replayed.",
    )


def main(args):
    command, args = args[0], args[1:]
    if command == "attach-fpm":
        attach_fpm(*args)
    elif command == "plan":
        meta.dump(contract(meta.load(args[0]), meta.load(args[1])))
    else:
        raise ValueError("Unknown source-first operation: " + command)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError) as error:
        print("[shift8-php] source contract error: " + str(error), file=sys.stderr)
        sys.exit(7)
