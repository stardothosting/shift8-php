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


if __name__ == "__main__":
    unittest.main()
