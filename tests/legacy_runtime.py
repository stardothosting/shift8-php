#!/usr/bin/env python
"""Small real-interpreter smoke suite, runnable with stock Python 2.7."""
from __future__ import print_function, unicode_literals

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import metadata
import build_metadata
import build_environment
import source_metadata
import deployment
import configure_source
import stage_source
import source_build

class LegacyRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="shift8-legacy.")

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_fixture_and_json_roundtrip(self):
        data = metadata.inventory("rpm", "php-cli", os.path.join(ROOT, "tests/fixtures/remi-el7.tsv"))
        self.assertEqual(len(data["candidates"]), 16)
        path = os.path.join(self.directory, "inventory.json")
        metadata.dump(data, path)
        self.assertEqual(metadata.load(path), data)

    def test_legacy_config_parser(self):
        directory = os.path.join(self.directory, "etc/yum.repos.d")
        os.makedirs(directory)
        with open(os.path.join(directory, "remi.repo"), "w") as stream:
            stream.write("[remi]\nenabled=0\ngpgcheck=1\n")
        result = metadata.repository_config("rpm", self.directory)
        self.assertFalse(result["configured"][0]["enabled"])

    def test_tar_xz_uses_system_tar_not_python_tarfile(self):
        prefix = os.path.join(self.directory, "php-8.2.35")
        os.makedirs(os.path.join(prefix, "main"))
        with open(os.path.join(prefix, "main/php_version.h"), "w") as stream:
            stream.write('#define PHP_VERSION "8.2.35"\n')
        archive = os.path.join(self.directory, "php.tar.xz")
        result = metadata.run(["tar", "-cJf", archive, "-C", self.directory, "php-8.2.35"])
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata.archive(archive, "8.2.35")

    def test_metadata_and_build_entry_points_import_without_modern_apis(self):
        self.assertEqual(metadata.version_tuple("8.2.35"), (8, 2, 35))
        self.assertTrue(callable(build_metadata.profile))
        self.assertEqual(metadata.run([sys.executable, "-c", "print('legacy child')"]).stdout.strip(), "legacy child")

    def test_isolated_environment_wrapper_with_real_python27(self):
        result = metadata.run([sys.executable, os.path.join(ROOT, "lib/build_environment.py"),
                               sys.executable, "-c", "print('legacy environment child')"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SHIFT8_BUILD_ENVIRONMENT", result.stdout)
        self.assertIn("legacy environment child", result.stdout)

    def test_source_contract_helpers_work_with_python27(self):
        self.assertEqual(source_metadata.absolute("/opt/remi/php83/root/usr/bin/php"),
                         "/opt/remi/php83/root/usr/bin/php")
        self.assertEqual(source_metadata.module_name("Zend OPcache"), "opcache")
        self.assertEqual(len(source_metadata.identity(dict(target="8.3.36"))), 64)

    def test_phpinfo_ignores_nonascii_application_output_on_python27(self):
        text = (
            b"PHP Version => 8.3.8\nPHP API => 20230831\nPHP Extension => 20230831\n"
            b"Zend Extension => 420230831\nThread Safety => disabled\n"
            b"extension_dir => /opt/remi/php83/root/usr/lib64/php/modules\n"
            b"APP_VALUE => caf\xc3\xa9\n"
        )
        result = metadata.phpinfo(text)
        self.assertEqual(result["PHP Version"], "8.3.8")
        self.assertNotIn("APP_VALUE", result)

    def test_source_index_routing_and_html_parser(self):
        source = "php83-php-8.3.8-1.el7.remi.src.rpm"
        data = dict(family=dict(source_reference=source, source_name="php83-php", packages=[]))
        config = dict(configured=[dict(id="vendor-source", enabled=False)])
        plan = metadata.source_repositories(data, config, dict(candidates=[]))
        self.assertEqual(plan["source_repositories"], ["vendor-source"])
        path = os.path.join(self.directory, "provider.html")
        with open(path, "w") as stream:
            stream.write('<a href="../' + source + '">source</a>')
        self.assertEqual(
            metadata.provider_source_locations(path, "https://rpms.remirepo.net/SRPMS/repoview/php83-php.html", source),
            ["https://rpms.remirepo.net/SRPMS/" + source],
        )

    def test_deployment_listener_parsing_and_rpm_queries_work_with_python27(self):
        original = (deployment._proc_fds, deployment._proc_link, deployment._proc_read)
        try:
            deployment._proc_fds = lambda pid: ["3"]
            deployment._proc_link = lambda path: "socket:[777]"
            deployment._proc_read = lambda path: (
                "sl local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
                "0: 0100007F:232C 00000000:0000 0A 0:0 0:0 0 0 0 777 1 0 0 0 0\n"
            )
            result = deployment._owned_listeners(1, [dict(type="tcp", address="127.0.0.1", port=9004)])
            self.assertEqual(result[0]["port"], 9004)
            with self.assertRaises(deployment.DeploymentError):
                deployment._owned_listeners(1, [dict(type="tcp", address="127.0.0.1", port=9999)])
        finally:
            deployment._proc_fds, deployment._proc_link, deployment._proc_read = original
        for query in (deployment.RPM_QUERY,):
            self.assertNotIn("\n", query.replace("\\n", ""))
            self.assertNotIn("\t", query.replace("\\t", ""))

    def test_source_build_helpers_reject_malformed_arguments_with_python27(self):
        with self.assertRaises(ValueError):
            configure_source.validate(["--"], "--enable-cli")
        with self.assertRaises(ValueError):
            source_build.safe("x'; touch /tmp/p; '")
        self.assertTrue(callable(stage_source.stage))


if __name__ == "__main__":
    unittest.main()
