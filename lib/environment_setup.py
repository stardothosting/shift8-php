#!/usr/bin/env python
"""Generate a reviewed, pinned mock build environment for the discovered installation."""
from __future__ import print_function, unicode_literals

import io
import json
import os
import re
import subprocess
import sys

import metadata as meta

OPENSSL_SERIES = "3.5"  # Official LTS series with upstream support until April 2030.
OPENSSL_URL = "https://github.com/openssl/openssl/releases/download/openssl-{0}/openssl-{0}.tar.gz"
TRUST = (
    "/etc/mock/centos+epel-7-x86_64.cfg", "/etc/mock/templates/centos-7.tpl", "/etc/mock/templates/epel-7.tpl",
    "/etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7", "/etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-7", "/etc/pki/rpm-gpg/RPM-GPG-KEY-remi",
)

REPOSITORIES = """
[base]
name=CentOS-7 - Base
baseurl=https://vault.centos.org/centos/7/os/$basearch/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
gpgcheck=1
skip_if_unavailable=False

[updates]
name=CentOS-7 - Updates
baseurl=https://vault.centos.org/centos/7/updates/$basearch/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
gpgcheck=1
skip_if_unavailable=False

[extras]
name=CentOS-7 - Extras
baseurl=https://vault.centos.org/centos/7/extras/$basearch/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
gpgcheck=1
skip_if_unavailable=False

[epel]
name=Extra Packages for Enterprise Linux 7 - $basearch
metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-7&arch=$basearch
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-7
gpgcheck=1
skip_if_unavailable=False

[remi]
name=Remi RPM repository for Enterprise Linux 7 - $basearch
baseurl=https://rpms.remirepo.net/enterprise/7/remi/$basearch/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-remi
gpgcheck=1
skip_if_unavailable=False

[remi-safe]
name=Safe Remi RPM repository for Enterprise Linux 7 - $basearch
baseurl=https://rpms.remirepo.net/enterprise/7/safe/$basearch/
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-remi
gpgcheck=1
skip_if_unavailable=False
"""


def collection(discovery):
    """Return the Remi branch repository id and SCL build-support package, if any."""
    match = re.match(r"(php\d{2})-php\Z", discovery["family"]["source_name"])
    branch = "php" + discovery["runtime"]["branch"].replace(".", "")
    if match and match.group(1) != branch:
        raise ValueError("Package family and runtime branch disagree")
    return branch, (branch + "-build" if match else None)


def configuration(discovery, root_name):
    branch, support = collection(discovery)
    packages = ("bash bzip2 coreutils cpio diffutils findutils gawk gcc gcc-c++ grep gzip info make patch "
                "redhat-rpm-config rpm-build sed shadow-utils tar unzip util-linux which xz")
    if support:
        packages += " " + support
    repositories = REPOSITORIES + (
        "\n[remi-{0}]\nname=Remi {0} RPM repository for Enterprise Linux 7 - $basearch\n"
        "baseurl=https://rpms.remirepo.net/enterprise/7/{0}/$basearch/\n"
        "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-remi\ngpgcheck=1\nskip_if_unavailable=False\n"
    ).format(branch)
    header = (
        "[main]\nkeepcache=1\ndebuglevel=2\nreposdir=/dev/null\nlogfile=/var/log/yum.log\nretries=20\nobsoletes=1\n"
        "gpgcheck=1\nassumeyes=1\nsyslog_ident=mock\nsyslog_device=\nmetadata_expire=0\nbest=1\nprotected_packages=\n"
    )
    return (
        'include("/etc/mock/centos+epel-7-x86_64.cfg")\n\n'
        'config_opts["chroot_setup_cmd"] = {0}\n'
        'config_opts["root"] = {1}\n'
        'config_opts["target_arch"] = "x86_64"\nconfig_opts["legal_host_arches"] = ("x86_64",)\n'
        'config_opts["package_manager"] = "yum"\n'
        'config_opts["description"] = "shift8-php EL7 source-first build"\n'
        'config_opts["yum.conf"] = """\n{2}{3}"""\n\n'
        # The bootstrap image cannot be created on some hosts (read-only /sys/fs/selinux).
        'config_opts["use_bootstrap_image"] = False\nconfig_opts["use_bootstrap"] = False\n'
        'config_opts["rpmbuild_networking"] = True\n'
    ).format(json.dumps("install " + packages), json.dumps(root_name), header, repositories)


def download(url, destination, expected):
    meta.https(url)
    subprocess.check_call([
        "curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
        "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "600",
        "--max-filesize", "268435456", "--output", destination, url,
    ])
    if meta.digest(destination) != expected:
        raise ValueError("Downloaded file does not match its published SHA-256: " + url)


def official_digest(url):
    result = meta.run(["curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
                       "--proto-redir", "=https", "--max-time", "30", "--max-filesize", "4096", meta.https(url)])
    match = re.match(r"([0-9a-f]{64})\b", result.stdout)
    if result.returncode or not match:
        raise ValueError("Cannot read the official SHA-256 sidecar: " + url)
    return match.group(1)


def generate(discovery_path, destination, version, jobs):
    if not re.match(re.escape(OPENSSL_SERIES) + r"\.\d+\Z", version):
        raise ValueError("Only the maintained OpenSSL {} LTS series is accepted".format(OPENSSL_SERIES))
    jobs = int(jobs)
    if not 1 <= jobs <= 64:
        raise ValueError("jobs must be between 1 and 64")
    discovery = meta.load(discovery_path)
    if discovery["package_manager"] != "rpm" or discovery["architecture"] != "x86_64":
        raise ValueError("Environment generation supports CentOS 7 x86_64 RPM hosts only")
    missing = [path for path in TRUST if not os.path.isfile(path)]
    if missing or not os.path.isfile("/usr/bin/mock"):
        raise ValueError("Install mock (EPEL) and verify these trust files exist: " + ", ".join(missing or ["mock"]))
    os.mkdir(destination, 0o700)
    branch, _ = collection(discovery)
    root = "shift8-{}-el7-x86_64".format(branch)
    config = os.path.join(destination, root + ".cfg")
    with io.open(config, "w", encoding="utf-8") as stream:
        stream.write(configuration(discovery, root))
    url = OPENSSL_URL.format(version)
    archive = os.path.join(destination, "openssl-{}.tar.gz".format(version))
    expected = official_digest(url + ".sha256")
    download(url, archive, expected)
    trust = [dict(path=path, sha256=meta.digest(path)) for path in (config,) + TRUST]
    meta.dump(dict(
        reviewed=True, configuration_closure_reviewed=True, signature_policy_reviewed=True,
        mock_config=dict(path=config, sha256=meta.digest(config)), trust_files=trust,
        compiler=dict(cc="/usr/bin/gcc", cxx="/usr/bin/g++", major=4), jobs=jobs,
        openssl=dict(version=version, path=archive, sha256=expected, url=url, maintenance_reviewed=True),
    ), os.path.join(destination, "environment.json"))
    print(os.path.join(destination, "environment.json"))


if __name__ == "__main__":
    try:
        generate(*sys.argv[1:])
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("[shift8-php] environment error: " + str(error), file=sys.stderr)
        sys.exit(7)
