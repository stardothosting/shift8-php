import copy
import importlib.util
import json
import pathlib
import sys
from unittest import mock

from test_milestones import CommandFixture, MOCK, ROOT, TemporaryTest, metadata

sys.path.insert(0, str(ROOT / "lib"))
SPEC = importlib.util.spec_from_file_location("source_metadata", ROOT / "lib/source_metadata.py")
source = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source)


def snapshot(parallel=False):
    prefix = "/opt/remi/php83/root/usr" if parallel else "/usr"
    configuration = "/etc/opt/remi/php83" if parallel else "/etc"
    name = "php83-php" if parallel else "php"
    info = {
        "PHP Version": "8.3.36", "PHP API": "20230831", "PHP Extension": "20230831",
        "Zend Extension": "420230831", "Thread Safety": "disabled",
        "Configuration File (php.ini) Path": configuration,
        "Loaded Configuration File": configuration + "/php.ini",
        "Scan this dir for additional .ini files": configuration + "/php.d",
        "OpenSSL Library Version": "OpenSSL 1.0.2k-fips 26 Jan 2017",
        "libXML Loaded Version": "20901",
    }
    return dict(
        distribution=dict(id="centos", version="7", point_release_evidence="CentOS Linux release 7.9.2009 (Core)"),
        package_manager="rpm", architecture="x86_64",
        runtime=dict(executable=prefix + "/bin/php", version="8.3.36", branch="8.3",
                     php_api="20230831", module_api="20230831", zend_extension_api="420230831", zts=False,
                     extension_dir=prefix + "/lib64/php/modules",
                     loaded_extensions=["Core", "PDO", "openssl", "libxml", "mbstring", "igbinary", "Zend OPcache"],
                     build=info),
        family=dict(source_name=name, fpm_binaries=[prefix + "/sbin/php-fpm"], packages=[
            dict(name=name + "-common", version="8.3.36", release="1.el7", role="core",
                 files=[prefix + "/lib64/php/modules/mbstring.so", prefix + "/lib64/php/modules/opcache.so"],
                 configuration_files=[configuration + "/php.ini"]),
            dict(name=name + "-pecl-igbinary", version="3.2.15", release="1.el7", role="separate-extension",
                 files=[prefix + "/lib64/php/modules/igbinary.so"], configuration_files=[]),
        ]),
        fpm_runtime=[dict(executable=prefix + "/sbin/php-fpm", build=copy.deepcopy(info))],
        services=[dict(path="/usr/lib/systemd/system/" + name + "-fpm.service", state="inspected")],
        runtime_libraries=dict(complete=True, diagnostics=[], libraries={}),
        compatibility=dict(sha256="a" * 64),
        installation_state=dict(package_runtime_drift=False, package_version="8.3.36", runtime_version="8.3.36"),
    )


def release():
    return dict(version="8.3.36", branch="8.3", source=dict(
        url="https://www.php.net/distributions/php-8.3.36.tar.xz", sha256="b" * 64,
    ))


class SourceContractTests(TemporaryTest):
    def test_system_and_parallel_layouts_preserve_exact_external_paths(self):
        for parallel in (False, True):
            data = snapshot(parallel)
            plan = source.contract(data, release())
            self.assertEqual(plan["layout"]["adapter"], "remi-parallel" if parallel else "system")
            self.assertEqual(plan["layout"]["paths"]["cli"], data["runtime"]["executable"])
            self.assertEqual(plan["layout"]["paths"]["extension_dir"], data["runtime"]["extension_dir"])
            self.assertFalse(plan["historical_packaging_required"])
            self.assertFalse(plan["deployable"])
            self.assertFalse(plan["build_ready"])
            self.assertFalse(plan["configure_arguments_executable"])
            self.assertEqual(plan["blockers"], [])

    def test_original_configure_text_is_not_replayed_or_executed(self):
        data = snapshot()
        data["runtime"]["build"]["Configure Command"] = "'./configure' '$(touch /tmp/should-not-exist)' --prefix=/wrong"
        with mock.patch.object(source.meta, "run") as run:
            plan = source.contract(data, release())
        run.assert_not_called()
        self.assertNotIn("touch", json.dumps(plan))
        self.assertIn("--prefix=/usr", plan["configure_arguments"])

    def test_runtime_drift_is_visible_and_does_not_select_the_old_package_version(self):
        data = snapshot()
        data["installation_state"].update(package_runtime_drift=True, package_version="8.3.8")
        plan = source.contract(data, release())
        self.assertIn("package-runtime-drift", [item["code"] for item in plan["blockers"]])
        self.assertEqual(plan["installed_runtime"], "8.3.36")
        target = release()
        target["version"] = "8.3.8"
        with self.assertRaisesRegex(ValueError, "cannot downgrade"):
            source.contract(data, target)

    def test_fpm_version_paths_thread_safety_and_missing_inspection_block(self):
        for key, value, code in (
            ("PHP Version", "8.3.8", "fpm-runtime-drift"),
            ("PHP Extension", "20220829", "fpm-runtime-drift"),
            ("Thread Safety", "enabled", "fpm-runtime-drift"),
            ("Configuration File (php.ini) Path", "/other", "fpm-configuration"),
        ):
            data = snapshot()
            data["fpm_runtime"][0]["build"][key] = value
            self.assertIn(code, [item["code"] for item in source.contract(data, release())["blockers"]])
        data["fpm_runtime"] = []
        self.assertIn("fpm-uninspected", [item["code"] for item in source.contract(data, release())["blockers"]])

    def test_unknown_extension_and_apache_module_are_not_silently_dropped(self):
        data = snapshot()
        data["runtime"]["loaded_extensions"].append("proprietary loader")
        data["family"]["packages"][0]["files"].append("/usr/lib64/httpd/modules/libphp.so")
        codes = [item["code"] for item in source.contract(data, release())["blockers"]]
        self.assertIn("unknown-extension", codes)
        self.assertIn("apache-module", codes)

    def test_loaded_and_installed_core_modules_preserve_linkage(self):
        data = snapshot()
        data["runtime"]["loaded_extensions"].remove("mbstring")
        plan = source.contract(data, release())
        modules = {item["name"]: item for item in plan["features"]}
        self.assertEqual(modules["mbstring"]["linkage"], "shared")
        self.assertFalse(modules["mbstring"]["required_loaded"])
        self.assertEqual(modules["openssl"]["linkage"], "builtin")
        self.assertNotIn("igbinary", modules)
        self.assertEqual(plan["separately_packaged_extensions"][0]["decision"],
                         "retain-only-after-ABI-linkage-and-load-tests")

    def test_dependencies_are_measured_and_not_implicitly_security_approved(self):
        plan = source.contract(snapshot(), release())
        libraries = {item["name"]: item for item in plan["dependencies"]}
        self.assertEqual(libraries["openssl"]["decision"], "private-source-required")
        self.assertTrue(libraries["openssl"]["prohibit_system_replacement"])
        self.assertTrue(libraries["libxml2"]["prohibit_system_replacement"])
        self.assertEqual(libraries["libxml2"]["maintenance_status"], "not-established")
        self.assertIn("oniguruma", libraries)

    def test_separately_packaged_zip_keeps_libzip_dependency_without_changing_provider(self):
        data = snapshot()
        data["runtime"]["loaded_extensions"].append("zip")
        data["family"]["packages"].append(dict(
            name="php-pecl-zip", version="1.22.3", release="1.el7", role="separate-extension",
            files=["/usr/lib64/php/modules/zip.so"], configuration_files=[],
        ))
        data["runtime"]["build"]["Libzip library version"] = "1.10.1"
        plan = source.contract(data, release())
        libraries = {item["name"]: item for item in plan["dependencies"]}
        self.assertIn("libzip", libraries)
        self.assertEqual(libraries["libzip"]["observed"], "1.10.1")
        self.assertNotIn("zip", [item["name"] for item in plan["features"]])
        self.assertIn("php-pecl-zip", [item["name"] for item in plan["separately_packaged_extensions"]])

    def test_library_and_feature_phpinfo_keys_are_allowlisted_without_secrets(self):
        text = (
            "PHP Version => 8.3.36\nPHP API => 20230831\nPHP Extension => 20230831\n"
            "Zend Extension => 420230831\nThread Safety => disabled\n"
            "extension_dir => /usr/lib64/php/modules\n"
            "Libzip version => 1.10.1\nLibzip headers version => 1.10.0\nLibzip library version => 1.10.1\n"
            "AES-256 encryption => No\nJPEG Support => enabled\nFreeType Support => enabled\n"
            "APP_SECRET => must-not-store\n"
        )
        info = metadata.phpinfo(text)
        self.assertEqual(info["Libzip library version"], "1.10.1")
        self.assertEqual(info["AES-256 encryption"], "No")
        self.assertEqual(info["JPEG Support"], "enabled")
        self.assertNotIn("APP_SECRET", info)

    def test_capability_tradeoffs_are_not_hidden_by_extension_presence(self):
        data = snapshot()
        data["runtime"]["loaded_extensions"].extend(["zip", "gd"])
        data["runtime"]["build"].update({
            "AES-128 encryption": "No", "AES-192 encryption": "No", "AES-256 encryption": "No",
            "JPEG Support": "enabled", "FreeType Support": "enabled",
        })
        plan = source.contract(data, release())
        capabilities = {(item["feature"], item["name"]): item for item in plan["capabilities"]}
        self.assertFalse(capabilities[("zip", "aes-256")]["observed"])
        self.assertFalse(capabilities[("zip", "aes-256")]["required"])
        self.assertTrue(capabilities[("gd", "jpeg")]["required"])
        self.assertTrue(capabilities[("gd", "freetype")]["required"])
        self.assertIsNone(capabilities[("gd", "webp")]["observed"])

    def test_contract_identity_excludes_host_configuration_bytes_and_deployment_state(self):
        data = snapshot()
        baseline = source.contract(data, release())
        data["compatibility"]["sha256"] = "c" * 64
        data["services"][0]["state"] = "inactive"
        data["runtime"]["build"]["Configure Command"] = "another builder"
        changed = source.contract(data, release())
        self.assertEqual(baseline["source_contract_sha256"], changed["source_contract_sha256"])
        self.assertNotEqual(baseline["deployment_fingerprint"], changed["deployment_fingerprint"])
        self.assertIn("NOT a reusable binary", changed["source_contract_identity_scope"])
        data["runtime"]["loaded_extensions"].append("sockets")
        self.assertNotEqual(changed["source_contract_sha256"], source.contract(data, release())["source_contract_sha256"])

    def test_other_platform_architecture_and_ambiguous_paths_are_explicit(self):
        data = snapshot()
        data["distribution"]["point_release_evidence"] = "CentOS Linux release 7.8.2003"
        data["architecture"] = "aarch64"
        data["runtime"]["build"]["Scan this dir for additional .ini files"] = "/etc/php.d:/extra"
        codes = [item["code"] for item in source.contract(data, release())["blockers"]]
        self.assertTrue({"platform", "architecture", "configuration-path"}.issubset(codes))
        data["runtime"]["extension_dir"] = "/usr/lib/../bad"
        with self.assertRaisesRegex(ValueError, "absolute installation path"):
            source.contract(data, release())


class SourceFirstCommandTests(CommandFixture):
    def setUp(self):
        super().setUp()
        fpm = self.host / "runtime/sbin/php-fpm"
        fpm.parent.mkdir(parents=True)
        fpm.write_text(MOCK.replace('elif command == "php":', 'elif command in ("php", "php-fpm"):'))
        fpm.chmod(0o755)

    def test_plan_does_not_query_repositories_download_srpms_or_create_artifact_store(self):
        result = self.cli("plan", "php-cli")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["strategy"], "source-first-managed-replacement")
        self.assertFalse(self.artifacts.exists())
        self.assertFalse(any(call[0] in ("repoquery", "dnf", "mock") for call in self.calls()))
        curl_urls = [call[-1] for call in self.calls() if call[0] == "curl"]
        self.assertEqual(len(curl_urls), 2)
        self.assertTrue(all("php.net/releases/" in url for url in curl_urls))

    def test_source_first_reports_runtime_drift_while_legacy_path_remains_strict(self):
        self.make_archive("8.2.35")
        result = self.cli("plan", "php-cli", MOCK_VERSION="8.2.35")
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["installation_state"]["package_runtime_drift"])
        self.assertEqual(plan["installed_runtime"], "8.2.35")
        strict = self.cli("discover", "php-cli", MOCK_VERSION="8.2.35")
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("Package version does not match", strict.stderr)

    def test_source_first_preparation_saves_only_authenticated_upstream_and_contract(self):
        result = self.cli("prepare", "php-cli", "--source-first", "--artifact-dir=" + str(self.artifacts))
        self.assertEqual(result.returncode, 0, result.stderr)
        path = pathlib.Path(result.stdout.strip())
        metadata.verify_manifest(path)
        plan = metadata.load(path.parent / "source-plan.json")
        self.assertFalse(plan["build_ready"])
        manifest = metadata.load(path)
        self.assertEqual(manifest["source_contract"], plan)
        self.assertTrue((path.parent / "sources/php-8.2.35.tar.xz").exists())
        self.assertFalse((path.parent / "source-discovery").exists())
        self.assertFalse((path.parent / "sources/packaging").exists())
        self.assertFalse(any(call[0] in ("repoquery", "dnf", "mock") for call in self.calls()))
        self.assertIn("not implemented", result.stderr)
        manifest["source_contract"]["target"] = "8.2.999"
        metadata.dump(manifest, path)
        with self.assertRaisesRegex(ValueError, "Source contract differs"):
            metadata.verify_manifest(path)

    def test_source_first_rejects_vendor_profiles_maps_and_cross_branch_targets(self):
        result = self.cli("prepare", "php-cli", "--source-first", "--source-map=" + str(self.mapping))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls())
        result = self.cli("plan", "php-cli", "--target=8.3.36")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed branch", result.stderr)
