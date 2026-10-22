import json
import sys
import tarfile
import io
import copy
import pathlib

from test_source_contract import snapshot, release
from test_milestones import ROOT, TemporaryTest, metadata

sys.path.insert(0, str(ROOT / "lib"))
import source_build


class SourceBuildTests(TemporaryTest):
    def test_resolved_arguments_preserve_paths_linkage_and_filter(self):
        arguments, plan = source_build.options(snapshot(parallel=True))
        self.assertIn("--prefix=/opt/remi/php83/root/usr", arguments)
        self.assertIn("--with-config-file-path=/etc/opt/remi/php83", arguments)
        self.assertIn("--with-config-file-scan-dir=/etc/opt/remi/php83/php.d", arguments)
        self.assertIn("--enable-mbstring=shared", arguments)
        self.assertIn("--with-openssl", arguments)
        self.assertIn("--enable-filter", arguments)
        self.assertIn("--enable-cli", arguments)
        self.assertNotIn("--", arguments)
        self.assertEqual(len(set(arguments)), len(arguments))
        self.assertFalse(plan["deployable"])

    def test_mysqli_preserves_mysqlnd_selection(self):
        data = snapshot()
        data["runtime"]["loaded_extensions"].extend(["mysqli", "pdo_mysql", "mysqlnd"])
        data["family"]["packages"][0]["files"].extend([
            "/usr/lib64/php/modules/mysqli.so", "/usr/lib64/php/modules/pdo_mysql.so",
        ])
        arguments, _ = source_build.options(data)
        self.assertIn("--with-mysqli=shared,mysqlnd", arguments)
        self.assertIn("--with-pdo-mysql=shared,mysqlnd", arguments)

    def test_unsupported_contract_or_branch_fails_before_build(self):
        data = snapshot()
        data["runtime"]["branch"] = "8.4"
        with self.assertRaisesRegex(ValueError, "PHP 8.3"):
            source_build.options(data)
        data = snapshot()
        data["installation_state"]["package_runtime_drift"] = True
        with self.assertRaisesRegex(ValueError, "contract blocked"):
            source_build.options(data)

    def test_unsafe_spec_or_shell_input_is_refused(self):
        for value in ("x'; touch /tmp/exploit; '", "%{execute}", "x\nName: foreign", "$(command)"):
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                source_build.safe(value)

    def test_capability_rewrite_changes_only_exact_core_version_relationships(self):
        data = snapshot()
        self.assertEqual(
            source_build.revision(data, "php-common = 8.3.36-1.el7", "8.3.37"),
            "php-common = 8.3.37-1.el7.shift8",
        )
        self.assertEqual(source_build.revision(data, "php(api) = 20230831", "8.3.37"),
                         "php(api) = 20230831")

    def test_source_archive_rejects_paths_symlinks_and_devices_outside_root(self):
        path = self.directory / "openssl.tar.gz"
        for name, kind, link in (
            ("openssl-3.5.9/../escape", tarfile.REGTYPE, ""),
            ("openssl-3.5.9/link", tarfile.SYMTYPE, "../../etc/passwd"),
            ("openssl-3.5.9/dev", tarfile.CHRTYPE, ""),
        ):
            with tarfile.open(str(path), "w:gz") as stream:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.linkname = link
                stream.addfile(item)
            with self.assertRaises(ValueError):
                source_build.archive(str(path), "openssl-3.5.9")

    def test_source_archive_allows_only_internal_links(self):
        path = self.directory / "openssl.tar.gz"
        with tarfile.open(str(path), "w:gz") as stream:
            item = tarfile.TarInfo("openssl-3.5.9/a")
            stream.addfile(item)
            item = tarfile.TarInfo("openssl-3.5.9/b")
            item.type = tarfile.SYMTYPE
            item.linkname = "a"
            stream.addfile(item)
        source_build.archive(str(path), "openssl-3.5.9")

    def test_generate_produces_preserving_spec_and_complete_pinned_inputs(self):
        data = snapshot(parallel=True)
        data["requested_package"] = "php83-php-cli"
        data["family"]["source_reference"] = "php83-php-8.3.8-1.el7.remi.src.rpm"
        data["compatibility"].update(complete=True)
        for item in data["family"]["packages"]:
            item.update(epoch="0", architecture="x86_64", provides=["php83-php(api) = 20230831"],
                        requires=["php83-php-common = 8.3.8-1.el7.remi"])
            if item["role"] == "core":
                item["release"] = "1.el7.remi"
                item["files"].extend(item["configuration_files"])
        data["runtime"]["loaded_extensions"].extend(["gd", "imap"])
        data["runtime"]["build"].update({"JPEG Support": "enabled", "WebP Support": "enabled"})
        target = release()
        root = self.directory / "prepared"
        (root / "sources").mkdir(parents=True)
        metadata.dump(data, root / "discovery.json")
        metadata.dump(target, root / "release.json")
        metadata.dump({"candidates": []}, root / "repositories.json")
        metadata.dump({"strategy": "source-first-managed-replacement"}, root / "source-plan.json")
        (root / "sources/packaging-trust").write_text("source-first")
        metadata.manifest(str(root), "source-preparation")
        config = self.write("mock.cfg", "# trusted fixture")
        key = self.write("key", "trusted fixture")
        openssl = self.directory / "openssl.tar.gz"
        with tarfile.open(str(openssl), "w:gz") as stream:
            stream.addfile(tarfile.TarInfo("openssl-3.5.9"))
        environment = dict(
            reviewed=True, mock_config=dict(path=str(config), sha256=metadata.digest(config)),
            trust_files=[dict(path=str(key), sha256=metadata.digest(key))],
            compiler=dict(cc="/usr/bin/gcc", cxx="/usr/bin/g++", major=4),
            jobs=2, configuration_closure_reviewed=True, signature_policy_reviewed=True,
            openssl=dict(version="3.5.9", path=str(openssl), sha256=metadata.digest(openssl),
                         url="https://example.test/openssl.tar.gz", maintenance_reviewed=True),
        )
        env = self.directory / "environment.json"
        metadata.dump(environment, env)
        destination = self.directory / "generated"
        source_build.generate(str(root / "manifest.json"), str(env), str(destination))
        profile = metadata.load(destination / "profile.json")
        for item in profile["inputs"]:
            self.assertEqual(metadata.digest(item["path"]), item["sha256"])
        spec = (destination / "shift8-source.spec").read_text()
        self.assertIn("BuildRequires: pkgconfig(libwebp)", spec)
        self.assertIn("BuildRequires: pkgconfig(krb5)", spec)
        self.assertIn("%ghost %config(noreplace) /etc/opt/remi/php83/php.ini", spec)
        self.assertIn("/usr/bin/python %{SOURCE3} %{SOURCE2}", spec)
        self.assertIn("/usr/bin/python %{SOURCE4} %{buildroot} %{SOURCE5}", spec)
        self.assertNotIn("sed -i", spec)
        self.assertIn("HARNESS_JOBS=2", spec)
        self.assertIn("/opt/shift8-php/deps/openssl-3.5.9/", spec)
