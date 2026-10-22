import json
import pathlib
import sys
import os

from test_milestones import ROOT, TemporaryTest

sys.path.insert(0, str(ROOT / "lib"))
import stage_source


class StageSourceTests(TemporaryTest):
    def test_payload_pruning_preserves_runtime_modes_not_private_artifact_modes(self):
        root = self.directory / "root"
        binary = root / "usr/bin/php"
        binary.parent.mkdir(parents=True)
        binary.write_text("binary")
        binary.chmod(0o700)
        config = root / "etc/php.ini"
        config.parent.mkdir()
        config.write_text("generated config must not overwrite production")
        private = root / "opt/shift8-php/deps/openssl-3.5.9/lib64/libssl.so.3"
        private.parent.mkdir(parents=True)
        private.write_text("library")
        private.chmod(0o600)
        plan = self.write("payload.json", json.dumps(dict(
            files=["/usr/bin/php"], private_prefix="/opt/shift8-php/deps/openssl-3.5.9",
        )))
        stage_source.stage(str(root), str(plan))
        self.assertTrue(binary.exists())
        self.assertFalse(config.exists())
        self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
        self.assertEqual(private.stat().st_mode & 0o777, 0o644)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o755)

    def test_missing_runtime_and_escaping_links_are_errors(self):
        root = self.directory / "root"
        root.mkdir()
        plan = self.write("payload.json", json.dumps(dict(
            files=["/php"], private_prefix="/opt/shift8-php/deps/openssl-3.5.9",
        )))
        with self.assertRaisesRegex(ValueError, "missing"):
            stage_source.stage(str(root), str(plan))
        (root / "php").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "escapes"):
            stage_source.stage(str(root), str(plan))
