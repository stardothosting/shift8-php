import copy
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("metadata", str(ROOT / "lib/metadata.py"))
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)
FIXTURE = ROOT / "tests/fixtures/remi-el7.tsv"


def discovery():
    return dict(
        package_manager="rpm", architecture="x86_64", environment_fingerprint="a" * 64,
        runtime=dict(version="8.2.20", branch="8.2", owner="php-cli"),
        family=dict(
            source_name="php", source_version="php-8.2.20-1.el7.remi",
            source_reference="php-8.2.20-1.el7.remi.src.rpm",
        ),
    )


def releases():
    return {
        "8.2.35": dict(tags=["security"], source=[dict(filename="php-8.2.35.tar.xz", sha256="a" * 64)]),
        "8.2.34": dict(tags=["security"], source=[dict(filename="php-8.2.34.tar.xz", sha256="b" * 64)]),
        "8.3.36": dict(tags=["security"], source=[dict(filename="php-8.3.36.tar.xz", sha256="c" * 64)]),
    }


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shift8-tests.")
        self.directory = pathlib.Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, content):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class MetadataTests(TemporaryTest):
    def select(self, target="", support=None, versions=None, installed=None):
        return metadata.select(
            installed or discovery(), support or dict(supported_versions=["8.2", "8.3"]),
            versions or releases(), target,
        )

    def test_exact_real_world_inventory(self):
        data = metadata.inventory("rpm", "php-cli", str(FIXTURE))
        self.assertEqual(len(data["core"]), 12)
        self.assertEqual(len(data["candidates"]), 16)
        self.assertEqual(data["requested"]["source_reference"], "php-8.2.20-1.el7.remi.src.rpm")

    def test_parallel_family_does_not_include_standard_php(self):
        parallel = metadata.read(FIXTURE).replace("php-", "php83-php-").replace("8.2.20", "8.3.8")
        path = self.write("packages.tsv", metadata.read(FIXTURE) + parallel)
        data = metadata.inventory("rpm", "php83-php-cli", str(path))
        self.assertTrue(all(item["name"].startswith("php83-php-") for item in data["candidates"]))

    def test_debian_versions_sources_and_epochs(self):
        path = self.write(
            "deb.tsv",
            "php8.2-cli\t2:8.2.20-1+ubuntu1\tamd64\tphp8.2\t2:8.2.20-1+ubuntu1\tinstalled\tUbuntu\n"
            "php8.2-fpm\t2:8.2.20-1+ubuntu1\tamd64\tphp8.2\t2:8.2.20-1+ubuntu1\tinstalled\tUbuntu\n"
            "php8.3-cli\t8.3.8-1\tamd64\tphp8.3\t8.3.8-1\tinstalled\tDebian\n",
        )
        data = metadata.inventory("deb", "php8.2-cli", str(path))
        self.assertEqual(len(data["core"]), 2)
        self.assertEqual(data["requested"]["epoch"], "2")
        self.assertNotIn("php8.3-cli", [item["name"] for item in data["candidates"]])

    def test_multiarch_ambiguity_is_explicit(self):
        path = self.write("packages.tsv", metadata.read(FIXTURE) + metadata.read(FIXTURE).replace("x86_64", "i686"))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            metadata.inventory("rpm", "php-cli", str(path))
        self.assertEqual(metadata.inventory("rpm", "php-cli:i686", str(path))["requested"]["architecture"], "i686")

    def test_latest_is_branch_preserving_and_dynamic(self):
        self.assertEqual(self.select()["version"], "8.2.35")
        versions = releases()
        versions["8.2.99"] = dict(source=[dict(filename="php-8.2.99.tar.xz", sha256="f" * 64)])
        self.assertEqual(self.select(versions=versions)["version"], "8.2.99")

    def test_eol_never_claimed_secure(self):
        with self.assertRaisesRegex(ValueError, "No current upstream security patch target"):
            self.select(support=dict(supported_versions=["8.3"]))

    def test_cross_branch_and_invented_targets_rejected(self):
        for target in ("8.3.36", "8.2.999", "8.2.36RC1"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.select(target=target)

    def test_target_before_latest_security_release_rejected(self):
        with self.assertRaisesRegex(ValueError, "predates"):
            self.select(target="8.2.34")

    def test_downgrade_rejected(self):
        installed = discovery()
        installed["runtime"]["version"] = "8.2.99"
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.select(installed=installed)

    def test_checksum_required_from_authority(self):
        versions = releases()
        versions["8.2.35"]["source"][0]["sha256"] = ""
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.select(versions=versions)

    def test_malformed_authoritative_release_entries_fail_explicitly(self):
        for entry in (None, "not-json-metadata", dict(tags="security", source=[]), dict(source=[None])):
            versions = releases()
            versions["8.2.35"] = entry
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "Invalid authoritative"):
                self.select(versions=versions)

    def test_phpinfo_filters_secrets_and_validates_abi(self):
        text = (
            "PHP Version => 8.2.20\nPHP API => 20220829\nPHP Extension => 20220829\n"
            "Zend Extension => 420220829\nThread Safety => disabled\nextension_dir => /unusual/modules => /unusual/modules\n"
            "AWS_SECRET_ACCESS_KEY => secret\n_ENV => secret\n"
        )
        info = metadata.phpinfo(text)
        self.assertEqual(info["extension_dir"], "/unusual/modules")
        self.assertNotIn("secret", json.dumps(info))
        with self.assertRaisesRegex(ValueError, "ABI"):
            metadata.phpinfo(text.replace("20220829", "invalid"))

    def test_os_release_is_parsed_not_sourced(self):
        marker = self.directory / "executed"
        self.write("etc/os-release", 'ID=centos\nVERSION_ID="7.9.2009"\nPRETTY_NAME="$(touch {})"\n'.format(marker))
        data = metadata.distribution(str(self.directory))
        self.assertEqual(data["id"], "centos")
        self.assertEqual(data["support"], "experimental")
        self.assertFalse(marker.exists())

    def test_legacy_release_fallback(self):
        self.write("etc/redhat-release", "CentOS Linux release 7.9.2009 (Core)\n")
        self.assertEqual(metadata.distribution(str(self.directory))["id"], "rhel-like")

    def test_rpm_repository_configuration_honors_global_trust(self):
        self.write("etc/yum.conf", "[main]\ngpgcheck=1\n")
        self.write("etc/yum.repos.d/remi.repo", "[remi-php82]\nenabled=1\nbaseurl=https://example.test/\n[disabled]\nenabled=0\n")
        self.write("etc/yum.repos.d/broken.repo", "[broken\n")
        data = metadata.repository_config("rpm", str(self.directory))
        self.assertEqual(len(data["diagnostics"]), 1)
        self.assertTrue(data["configured"][0]["gpgcheck"])
        self.assertFalse(data["configured"][1]["enabled"])

    def test_apt_trust_bypass_and_deb_src_are_recorded(self):
        self.write("etc/apt/sources.list", "deb-src [trusted=yes] https://example.test stable main\n")
        data = metadata.repository_config("deb", str(self.directory))
        self.assertTrue(data["configured"][0]["explicit_trust_bypass"])
        self.assertTrue(data["configured"][0]["source_entries_present"])

    def test_frozen_newer_backported_and_broken_repositories_distinguished(self):
        metadata.dump(discovery(), self.directory / "discovery.json")
        metadata.dump(self.select(), self.directory / "release.json")
        metadata.dump(dict(configured=[], diagnostics=[]), self.directory / "repository-config.json")
        rows = [
            ("frozen", "available", "php-cli\t0\t8.2.20\t1.el7.remi\tx86_64\tphp-8.2.20-1.el7.remi.src.rpm"),
            ("newer", "available", "php-cli\t0\t8.2.35\t1.el7.remi\tx86_64\tphp-8.2.35-1.el7.remi.src.rpm"),
            ("broken", "unavailable", ""),
            ("foreign", "available", "php-cli\t0\t8.3.36\t1.el7.remi\tx86_64\tphp-8.3.36-1.el7.remi.src.rpm"),
        ]
        for index, (name, state, output) in enumerate(rows):
            self.write("repository-results/{}.state".format(index), name + "\t" + state)
            self.write("repository-results/{}.out".format(index), output)
            self.write("repository-results/{}.err".format(index), "HTTP 404" if state == "unavailable" else "")
        result = metadata.repositories(str(self.directory), "queried")
        self.assertEqual(result["preferred_route"], "native-package-review")
        candidates = result["candidates"]
        self.assertEqual(sum(item["reaches_upstream_target"] for item in candidates), 1)
        self.assertTrue(all(item["security_status"] == "signature-and-advisory-review-required" for item in candidates))
        self.assertEqual(result["repositories"][2]["availability"], "unavailable")

    def test_cache_corruption_staleness_and_future_dates_rejected(self):
        destination = str(self.directory / "metadata.json")
        temporary = self.write("temporary", "{}")
        metadata.cache_write(str(temporary), destination, "https://example.test/data", "")
        metadata.cache_check(destination, "https://example.test/data", "")
        receipt = metadata.load(destination + ".receipt.json")
        receipt["fetched_at"] = int(time.time()) - 86401
        metadata.dump(receipt, destination + ".receipt.json")
        with self.assertRaisesRegex(ValueError, "stale"):
            metadata.cache_check(destination, "https://example.test/data", "")
        receipt["fetched_at"] = int(time.time()) + 3600
        metadata.dump(receipt, destination + ".receipt.json")
        with self.assertRaisesRegex(ValueError, "future-dated"):
            metadata.cache_check(destination, "https://example.test/data", "")
        self.write("metadata.json", "tampered")
        with self.assertRaisesRegex(ValueError, "mismatch"):
            metadata.cache_check(destination, "https://example.test/data", "")

    def test_expected_source_hash_can_be_used_offline_after_metadata_age(self):
        temporary = self.write("temporary", "source")
        destination = str(self.directory / "source")
        expected = metadata.digest(temporary)
        metadata.cache_write(str(temporary), destination, "https://example.test/source", expected)
        receipt = metadata.load(destination + ".receipt.json")
        receipt["fetched_at"] = 1
        metadata.dump(receipt, destination + ".receipt.json")
        metadata.cache_check(destination, "https://example.test/source", expected)

    def test_https_downgrades_credentials_and_control_characters_rejected(self):
        for url in ("http://example.test/", "https://u:p@example.test/", "https://example.test/\nflag", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                metadata.https(url)

    def test_invalid_checksum_never_published(self):
        temporary = self.write("partial", "bad")
        destination = str(self.directory / "complete")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            metadata.cache_write(str(temporary), destination, "https://example.test/source", "a" * 64)
        self.assertFalse(pathlib.Path(destination).exists())

    def test_source_map_requires_exact_source_review_and_unique_safe_files(self):
        mapping = dict(
            installed_source=discovery()["family"]["source_reference"], reviewed=True,
            files=[dict(name="php82.spec", url="https://example.test/php82.spec", sha256="a" * 64)],
        )
        path = self.directory / "source-map.json"
        metadata.dump(mapping, path)
        self.assertEqual(len(list(metadata.source_map(str(path), discovery()))), 1)
        for mutate in (
            lambda item: item.update(reviewed=False),
            lambda item: item.update(installed_source="different.src.rpm"),
            lambda item: item["files"][0].update(name="../../escape"),
        ):
            invalid = copy.deepcopy(mapping)
            mutate(invalid)
            metadata.dump(invalid, path)
            with self.assertRaises(ValueError):
                list(metadata.source_map(str(path), discovery()))

    def test_rpm_digest_is_not_a_signature(self):
        for text in ("MD5 digest: OK", "Header V4 RSA/SHA256 Signature, key ID a: NOKEY", "BAD signature"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                metadata.rpm_signature(text)
        metadata.rpm_signature("Header V4 RSA/SHA256 Signature, key ID 1234: OK")

    def test_signed_dsc_members_need_exact_identity_and_sha256(self):
        text = "Source: php8.2\nVersion: 8.2.20-1\nChecksums-Sha256:\n {} 4 php8.2.orig.tar.xz\n".format("a" * 64)
        records = list(metadata.dsc(text, "php8.2", "8.2.20-1", "https://example.test/pool/source.dsc"))
        self.assertEqual(records[0][1], "https://example.test/pool/php8.2.orig.tar.xz")
        for invalid in (text.replace("8.2.20-1", "8.2.19-1"), text.replace("php8.2.orig.tar.xz", "../escape")):
            with self.assertRaises(ValueError):
                list(metadata.dsc(invalid, "php8.2", "8.2.20-1", "https://example.test/pool/source.dsc"))

    def test_apt_uri_selection_is_exact_and_https_only(self):
        uri = "'https://example.test/source.dsc' source.dsc 123 SHA256:abc\n"
        self.assertEqual(metadata.dsc_url(uri), "https://example.test/source.dsc")
        for invalid in (uri + uri, uri.replace("https:", "http:"), "No source package available"):
            with self.assertRaises(ValueError):
                metadata.dsc_url(invalid)

    def test_artifact_store_rejects_shared_permissions_and_symlinks(self):
        store = self.directory / "artifacts"
        metadata.store(str(store))
        store.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            metadata.store(str(store))
        store.chmod(0o700)
        (store / "cache/redirect").symlink_to(self.directory)
        with self.assertRaisesRegex(ValueError, "Untrusted"):
            metadata.store(str(store))


MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys, urllib.parse
root = pathlib.Path(os.environ["MOCK_ROOT"])
args = sys.argv[1:]
command = pathlib.Path(sys.argv[0]).name
with open(root / "calls", "a") as output:
    output.write(json.dumps([command] + args) + "\n")
if command in ("dpkg-query", "systemctl"):
    sys.exit(1)
if command == "rpm":
    rows = (root / "packages.tsv").read_text().splitlines()
    names = [row.split("\t")[0] for row in rows]
    if args[0] == "-qa":
        print("\n".join(rows))
    elif args[0] == "-qf":
        print(os.environ.get("MOCK_OWNER", names[0]))
    elif args[0] == "-ql":
        name = args[-1]
        if name.endswith("-cli"):
            print(root / "runtime/bin/php")
        elif name.endswith("-fpm"):
            print(root / "runtime/sbin/php-fpm")
            print("/usr/lib/systemd/system/" + name + ".service")
        else:
            print(root / "runtime/lib/modules" / (name + ".so"))
    elif args[0] == "-q":
        name = args[-1]
        if name not in names:
            sys.exit(1)
        if "--qf" in args:
            print(name)
        elif "--requires" in args:
            print("php(api) = 20220829")
        elif "--provides" in args:
            print("php(api) = 20220829")
        elif "--configfiles" in args:
            print("/etc/php.ini")
        elif "--scripts" in args:
            print("postinstall scriptlet: (recorded, never executed)")
        else:
            sys.exit(2)
    elif args[0] == "--checksig":
        print(os.environ.get("MOCK_SIGNATURE", "Header V4 RSA/SHA256 Signature, key ID 1234: OK"))
    elif args[0] == "-qp":
        print(os.environ.get("MOCK_SRPM_IDENTITY", "php-8.2.20-1.el7.remi.src.rpm\t1"))
    elif args[0] == "-qpl":
        print("php82.spec\nphp.ini\npatch.diff")
    else:
        sys.exit(2)
elif command == "php":
    version = os.environ.get("MOCK_VERSION", "8.2.20")
    if args == ["-i"]:
        print("PHP Version => " + version)
        print("PHP API => 20220829\nPHP Extension => 20220829\nZend Extension => 420220829")
        print("Thread Safety => disabled")
        print("extension_dir => " + str(root / "runtime/lib/modules"))
        print("Configure Command => '/opt/rh/devtoolset-10/root/usr/bin/gcc'")
        print("SECRET_TOKEN => never-store-this")
    elif args == ["-m"]:
        print("[PHP Modules]\nCore\nredis\nigbinary\nmsgpack\nzip\n[Zend Modules]\nZend OPcache")
    elif args == ["--ini"]:
        print("Loaded Configuration File: /etc/php.ini")
    else:
        sys.exit(2)
elif command == "curl":
    if os.environ.get("MOCK_NETWORK_FAIL"):
        sys.exit(int(os.environ["MOCK_NETWORK_FAIL"]))
    if os.environ.get("MOCK_IPV6_FAIL") and "-4" not in args:
        sys.exit(7)
    url = args[-1]
    if url.startswith("https://unavailable.example.test"):
        sys.exit(60)
    if "version=8&" in url or url.endswith("version=8"):
        filename = "support.json"
    elif "releases/index.php" in url:
        filename = "releases.json"
    elif url.endswith(".tar.xz"):
        filename = "php.tar.xz"
    elif url.endswith(".spec"):
        filename = "vendor.spec"
    elif url.endswith(".rpm"):
        filename = "source.rpm"
    elif url.endswith(".dsc"):
        filename = "source.dsc"
    else:
        sys.exit(22)
    destination = pathlib.Path(args[args.index("--output") + 1])
    if "--continue-at" in args:
        offset = destination.stat().st_size
        with destination.open("ab") as stream:
            stream.write((root / filename).read_bytes()[offset:])
    else:
        shutil.copyfile(root / filename, destination)
elif command in ("repoquery", "dnf"):
    if "--location" in args:
        if os.environ.get("MOCK_SOURCE_FAIL_FIRST"):
            print("https://unavailable.example.test/source.rpm")
        print("https://example.test/php-8.2.20-1.el7.remi.src.rpm")
    else:
        print("php-cli\t0\t8.2.20\t1.el7.remi\tx86_64\tphp-8.2.20-1.el7.remi.src.rpm")
elif command == "apt-get":
    print("'https://example.test/pool/source.dsc' source.dsc 100 SHA256:unused")
else:
    sys.exit(99)
'''


class CommandTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.host = self.directory / "host"
        self.host.mkdir()
        self.bin = self.host / "bin"
        self.bin.mkdir()
        for command in ("rpm", "dpkg-query", "curl", "systemctl", "repoquery"):
            path = self.bin / command
            path.write_text(MOCK, encoding="utf-8")
            path.chmod(0o755)
        runtime = self.host / "runtime/bin/php"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(MOCK, encoding="utf-8")
        runtime.chmod(0o755)
        (self.bin / "php").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        (self.bin / "php").chmod(0o755)
        shutil.copyfile(str(FIXTURE), str(self.host / "packages.tsv"))
        metadata.dump(dict(supported_versions=["8.2", "8.3"]), self.host / "support.json")
        self.make_archive("8.2.35")
        versions = releases()
        versions["8.2.35"]["source"][0]["sha256"] = metadata.digest(self.host / "php.tar.xz")
        metadata.dump(versions, self.host / "releases.json")
        (self.host / "vendor.spec").write_text("Name: php\n# reviewed fixture, never executed\n", encoding="utf-8")
        (self.host / "source.rpm").write_text("mock-signed-srpm", encoding="utf-8")
        self.mapping = self.host / "source-map.json"
        metadata.dump(dict(
            reviewed=True, installed_source="php-8.2.20-1.el7.remi.src.rpm",
            files=[dict(
                name="php82.spec", url="https://example.test/vendor.spec",
                sha256=metadata.digest(self.host / "vendor.spec"),
            )],
        ), self.mapping)
        self.artifacts = self.directory / "artifacts"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], MOCK_ROOT=str(self.host))

    def make_archive(self, version, member=None):
        with tarfile.open(str(self.host / "php.tar.xz"), "w:xz") as archive:
            content = ('#define PHP_VERSION "{}"\n'.format(version)).encode()
            item = tarfile.TarInfo(member or "php-" + version + "/main/php_version.h")
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))

    def cli(self, *args, **environment):
        return subprocess.run(
            ["bash", str(ROOT / "shift8-php")] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(self.env, **environment), timeout=60, check=False,
        )

    def prepare(self, *extra, **environment):
        return self.cli(
            "prepare", "php-cli", "--artifact-dir=" + str(self.artifacts),
            "--source-map=" + str(self.mapping), *extra, **environment
        )

    def calls(self):
        path = self.host / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_discovery_uses_package_binary_and_preserves_all_extensions(self):
        result = self.cli("discover", "php-cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["runtime"]["executable"], str(self.host / "runtime/bin/php"))
        self.assertEqual(len(data["family"]["packages"]), 16)
        self.assertEqual(data["runtime"]["module_api"], "20220829")
        self.assertEqual(data["runtime"]["zend_extension_api"], "420220829")
        self.assertFalse(data["runtime"]["zts"])
        self.assertIn("redis", data["runtime"]["loaded_extensions"])
        self.assertNotIn("never-store-this", result.stdout)
        self.assertFalse(self.artifacts.exists())
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_parallel_discovery_does_not_inspect_standard_runtime(self):
        parallel = metadata.read(FIXTURE).replace("php-", "php83-php-").replace("8.2.20", "8.3.8")
        (self.host / "packages.tsv").write_text(metadata.read(FIXTURE) + parallel, encoding="utf-8")
        result = self.cli("discover", "php83-php-cli", MOCK_VERSION="8.3.8", MOCK_OWNER="php83-php-cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(all(name.startswith("php83-php-") for name in data["family"]["names"]))
        queried = [call[-1] for call in self.calls() if call[0] == "rpm" and call[1] == "-ql"]
        self.assertTrue(all(name.startswith("php83-php-") for name in queried))

    def test_version_or_ownership_drift_stops_preparation(self):
        for environment in (dict(MOCK_OWNER="unrelated-php"), dict(MOCK_VERSION="8.3.1")):
            with self.subTest(environment=environment):
                result = self.prepare(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not modified", result.stderr)
                self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_prepare_produces_verified_non_deployable_manifest(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest_path = pathlib.Path(result.stdout.strip())
        data = metadata.load(manifest_path)
        self.assertFalse(data["deployable"])
        self.assertEqual(data["kind"], "source-preparation")
        self.assertEqual(data["packaging_authentication"], "operator-reviewed-checksum-map")
        self.assertTrue(any(item["path"].endswith("php82.spec") for item in data["files"]))
        metadata.verify_manifest(str(manifest_path))
        calls = self.calls()
        self.assertFalse(any(call[0] in ("yum", "apt-get", "make", "rpmbuild") for call in calls))
        self.assertFalse(any(call[0] == "systemctl" and "restart" in call for call in calls))
        source = manifest_path.parent / "sources/php-8.2.35.tar.xz"
        source.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "integrity"):
            metadata.verify_manifest(str(manifest_path))

    def test_offline_reuses_verified_cache_without_network(self):
        first = self.prepare()
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.host / "calls").write_text("", encoding="utf-8")
        result = self.prepare("--offline", MOCK_NETWORK_FAIL="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_offline_missing_and_stale_metadata_fail(self):
        result = self.prepare("--offline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stderr)
        self.assertEqual(self.prepare().returncode, 0)
        receipt_path = self.artifacts / "cache/support-8.json.receipt.json"
        receipt = metadata.load(receipt_path)
        receipt["fetched_at"] = 0
        metadata.dump(receipt, receipt_path)
        result = self.prepare("--offline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale", result.stderr)

    def test_dry_run_never_persists_or_downloads_sources(self):
        result = self.prepare("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.artifacts.exists())
        self.assertFalse(json.loads(result.stdout)["persistent_changes"])
        downloads = [call[-1] for call in self.calls() if call[0] == "curl"]
        self.assertTrue(all("releases/index.php" in url for url in downloads))

    def test_ipv6_failure_retries_with_ipv4(self):
        result = self.prepare(MOCK_IPV6_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = [call for call in self.calls() if call[0] == "curl"]
        self.assertNotIn("-4", requests[0])
        self.assertIn("-4", requests[1])
        self.assertTrue(all("--proto" in call and "--proto-redir" in call for call in requests))
        self.assertFalse(any("-k" in call or "--insecure" in call for call in requests))

    def test_explicit_ipv4_applies_to_every_download(self):
        result = self.prepare("--ipv4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all("-4" in call for call in self.calls() if call[0] == "curl"))

    def test_checksum_pinned_partial_source_is_resumed_then_verified(self):
        self.artifacts.mkdir(mode=0o700)
        (self.artifacts / "cache").mkdir(mode=0o700)
        expected = metadata.digest(self.host / "php.tar.xz")
        partial = self.artifacts / "cache" / ("php-8.2.35.tar.xz.part-" + expected)
        partial.write_bytes((self.host / "php.tar.xz").read_bytes()[:10])
        partial.chmod(0o600)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = [call for call in self.calls() if call[0] == "curl" and call[-1].endswith(".tar.xz")]
        self.assertIn("--continue-at", requests[0])
        self.assertFalse(partial.exists())

    def test_tls_failure_is_not_retried_with_insecure_options(self):
        result = self.prepare(MOCK_NETWORK_FAIL="60")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("will not be disabled", result.stderr)
        self.assertEqual(sum(call[0] == "curl" for call in self.calls()), 1)

    def test_mismatched_upstream_hash_leaves_no_manifest(self):
        (self.host / "php.tar.xz").write_bytes(b"bad archive")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.artifacts.glob("builds/*/manifest.json")))
        self.assertFalse(list(self.artifacts.glob("cache/*.part.*")))

    def test_archive_has_expected_version_and_safe_paths(self):
        metadata.archive(str(self.host / "php.tar.xz"), "8.2.35")
        self.make_archive("8.2.34")
        with self.assertRaises(ValueError):
            metadata.archive(str(self.host / "php.tar.xz"), "8.2.35")
        self.make_archive("8.2.35", member="../escape")
        with self.assertRaisesRegex(ValueError, "Unexpected path"):
            metadata.archive(str(self.host / "php.tar.xz"), "8.2.35")

    def test_deploy_rollback_and_future_flags_fail_before_inspection(self):
        for command in ("deploy", "rollback"):
            result = self.cli(command, "php-cli", "--yes")
            self.assertEqual(result.returncode, 3)
            self.assertIn("disabled", result.stderr)
        for flag in ("--build-only", "--force-rebuild", "--allow-prerelease"):
            result = self.cli("prepare", "php-cli", flag)
            self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_invalid_arguments_are_rejected(self):
        for arguments in (
            ("prepare", "../php-cli"), ("prepare", "php-cli", "php-fpm"),
            ("prepare", "php-cli", "--target=8.2.35;id"),
            ("prepare", "php-cli", "--target="),
            ("prepare", "php-cli", "--source-map="),
            ("prepare", "php-cli", "--source-keyring="),
            ("prepare", "php-cli", "--unknown"), ("prepare",),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.cli(*arguments).returncode, 2)

    def test_rpm_query_avoids_unsupported_yum_options(self):
        script = (
            'set -Eeuo pipefail; source "$1/lib/repositories.sh"; '
            'WORK=$2; IPV4=1; REPO_TOOL=repoquery; '
            'rpm_query remi-php82 candidates php-cli'
        )
        result = subprocess.run(
            ["bash", "-c", script, "_", str(ROOT), str(self.directory)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[-1]
        self.assertNotIn("--refresh", args)
        self.assertFalse(any("--repofrompath" in part for part in args))
        self.assertIn("--setopt=ip_resolve=4", args)

    def test_manifest_cannot_escape_its_directory(self):
        path = self.directory / "manifest.json"
        metadata.dump(dict(
            schema_version=1, kind="source-preparation", deployable=False,
            files=[dict(path="../escape", sha256="a" * 64, size=1)],
        ), path)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            metadata.verify_manifest(str(path))

    def source_context(self, data=None):
        metadata.store(str(self.artifacts))
        work = self.directory / "source-work"
        work.mkdir()
        build = self.directory / "source-build"
        build.mkdir()
        (build / "sources").mkdir()
        metadata.dump(data or discovery(), work / "discovery.json")
        metadata.dump(dict(configured=[dict(id="remi-source", enabled=False)]), work / "repository-config.json")
        return dict(
            self.env, BASE_DIR=str(ROOT), WORK=str(work), BUILD_DIR=str(build),
            ARTIFACT_DIR=str(self.artifacts), LOG_FILE=str(self.directory / "source.log"),
            OPERATION="source-acquisition", OFFLINE="0", IPV4="0", VERBOSE="0",
            REPO_TOOL="repoquery", SOURCE_KEYRING="",
        )

    def source_function(self, function, environment):
        return subprocess.run(
            ["bash", "-c",
             'set -Eeuo pipefail; source "$BASE_DIR/lib/common.sh"; '
             'source "$BASE_DIR/lib/networking.sh"; source "$BASE_DIR/lib/repositories.sh"; '
             'source "$BASE_DIR/lib/sources.sh"; ' + function],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60, check=False,
        )

    def test_exact_srpm_queried_from_configured_disabled_source_repo_and_verified(self):
        environment = self.source_context()
        result = self.source_function("acquire_srpm", environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((pathlib.Path(environment["BUILD_DIR"]) / "sources/php-8.2.20-1.el7.remi.src.rpm").is_file())
        source_queries = [call for call in self.calls() if call[0] == "repoquery"]
        self.assertIn("--archlist=src", source_queries[0])
        self.assertIn("--enablerepo=remi-source", source_queries[0])
        self.assertEqual(source_queries[0][-1], "php-8.2.20-1.el7.remi")
        self.assertTrue(any(call[1] == "--checksig" for call in self.calls() if call[0] == "rpm"))

    def test_srpm_unknown_signature_and_identity_mismatch_fail(self):
        environment = self.source_context()
        environment["MOCK_SIGNATURE"] = "Header V4 RSA/SHA256 Signature, key ID 1234: NOKEY"
        result = self.source_function("acquire_srpm", environment)
        self.assertNotEqual(result.returncode, 0)
        environment["MOCK_SIGNATURE"] = "Header V4 RSA/SHA256 Signature, key ID 1234: OK"
        environment["MOCK_SRPM_IDENTITY"] = "foreign-8.2.20-1.el9.src.rpm\t1"
        result = self.source_function("acquire_srpm", environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((pathlib.Path(environment["BUILD_DIR"]) / "sources/packaging-trust").exists())

    def test_srpm_failed_location_does_not_prevent_other_metadata_locations(self):
        environment = self.source_context()
        environment["MOCK_SOURCE_FAIL_FIRST"] = "1"
        result = self.source_function("acquire_srpm", environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Source transfer failed", result.stderr)
        transfers = [call[-1] for call in self.calls() if call[0] == "curl"]
        self.assertEqual(len(transfers), 2)
        self.assertTrue(transfers[-1].startswith("https://example.test/"))

    def test_missing_srpm_in_offline_cache_fails_without_network(self):
        environment = self.source_context()
        environment["OFFLINE"] = "1"
        result = self.source_function("acquire_srpm", environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not cached", result.stderr)
        self.assertFalse(any(call[0] in ("curl", "repoquery") for call in self.calls()))

    @unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgv"), "GnuPG is required for real source-signature tests")
    def test_real_debian_signature_and_members_are_verified(self):
        data = discovery()
        data["package_manager"] = "deb"
        data["family"].update(source_name="php8.2", source_version="8.2.20-1", source_reference="php8.2=8.2.20-1")
        environment = self.source_context(data)
        gpg_home = self.directory / "gnupg"
        gpg_home.mkdir(mode=0o700)
        generated = subprocess.run(
            ["gpg", "--homedir", str(gpg_home), "--batch", "--pinentry-mode", "loopback",
             "--passphrase", "", "--quick-generate-key", "shift8 test fixture", "ed25519", "sign", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr.decode())
        control = self.directory / "source.control"
        control.write_text(
            "Source: php8.2\nVersion: 8.2.20-1\nChecksums-Sha256:\n {} {} php8.2.orig.tar.xz\n".format(
                metadata.digest(self.host / "php.tar.xz"), (self.host / "php.tar.xz").stat().st_size,
            ), encoding="utf-8",
        )
        signed = subprocess.run(
            ["gpg", "--homedir", str(gpg_home), "--batch", "--pinentry-mode", "loopback",
             "--passphrase", "", "--output", str(self.host / "source.dsc"), "--clearsign", str(control)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(signed.returncode, 0, signed.stderr.decode())
        keyring = self.directory / "source-keyring.gpg"
        with keyring.open("wb") as output:
            exported = subprocess.run(
                ["gpg", "--homedir", str(gpg_home), "--batch", "--export"],
                stdout=output, stderr=subprocess.PIPE, check=False,
            )
        self.assertEqual(exported.returncode, 0, exported.stderr.decode())
        environment["SOURCE_KEYRING"] = str(keyring)
        apt = self.bin / "apt-get"
        apt.write_text(MOCK, encoding="utf-8")
        apt.chmod(0o755)
        result = self.source_function("acquire_debian", environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        build = pathlib.Path(environment["BUILD_DIR"])
        self.assertTrue((build / "sources/debian/php8.2.orig.tar.xz").is_file())
        self.assertEqual(
            (build / "sources/packaging-trust").read_text().strip(), "dsc-signature-and-sha256-verified",
        )
        apt_calls = [call for call in self.calls() if call[0] == "apt-get"]
        self.assertIn("--print-uris", apt_calls[0])
        self.assertEqual(apt_calls[0][-1], "php8.2=8.2.20-1")
        self.assertFalse(any("install" in call for call in apt_calls))

        (pathlib.Path(environment["WORK"]) / "source.control").unlink()
        shutil.rmtree(str(build / "sources/debian"))
        (build / "sources/packaging-trust").unlink()
        (self.host / "source.dsc").write_text("invalid signature", encoding="utf-8")
        cached = self.artifacts / "cache/php8.2-8.2.20-1.dsc"
        cached.unlink()
        pathlib.Path(str(cached) + ".receipt.json").unlink()
        result = self.source_function("acquire_debian", environment)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((build / "sources/packaging-trust").exists())

    def test_native_review_manifest_never_claims_sources_or_deployment(self):
        directory = self.directory / "native"
        directory.mkdir()
        metadata.dump(discovery(), directory / "discovery.json")
        metadata.dump(releases(), directory / "release.json")
        metadata.dump(dict(candidates=[]), directory / "repositories.json")
        metadata.manifest(str(directory), "native-update-review")
        metadata.verify_manifest(str(directory / "manifest.json"))
        result = metadata.load(directory / "manifest.json")
        self.assertFalse(result["deployable"])
        self.assertIn("not-acquired", result["packaging_authentication"])


if __name__ == "__main__":
    unittest.main()
