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


class RuntimePathTests(TemporaryTest):
    PRIVATE = "/opt/shift8-php/deps/openssl-3.5.9"

    def elf(self, relative="usr/bin/php"):
        root = self.directory / "root"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + b"\0" * 12)
        return root

    def readelf(self, output):
        from unittest import mock
        process = mock.Mock()
        process.communicate.return_value = (output.encode(), b"")
        return mock.patch.object(stage_source.subprocess, "Popen", return_value=process)

    NEEDS = " 0x1 (NEEDED) Shared library: [libssl.so.3]\n"

    def test_build_only_runtime_path_is_rejected(self):
        root = self.elf()
        stale = self.NEEDS + " 0xf (RPATH) Library rpath: [/builddir/build/BUILD/openssl-stage" + self.PRIVATE + "/lib64]\n"
        with self.readelf(stale), self.assertRaisesRegex(ValueError, "Build-only"):
            stage_source.verify_runtime_paths(str(root), self.PRIVATE)

    def test_private_openssl_without_runtime_path_is_rejected(self):
        root = self.elf()
        with self.readelf(self.NEEDS), self.assertRaisesRegex(ValueError, "Missing private OpenSSL"):
            stage_source.verify_runtime_paths(str(root), self.PRIVATE)

    def test_final_private_runtime_path_is_accepted(self):
        root = self.elf()
        library = root / "opt/shift8-php/deps/openssl-3.5.9/lib64"
        library.mkdir(parents=True)
        for name in ("libssl.so.3", "libcrypto.so.3"):
            (library / name).write_text("x")
        good = self.NEEDS + " 0xf (RPATH) Library rpath: [" + self.PRIVATE + "/lib64]\n"
        with self.readelf(good):
            stage_source.verify_runtime_paths(str(root), self.PRIVATE)

    def test_missing_private_library_in_payload_is_rejected(self):
        root = self.elf()
        good = self.NEEDS + " 0xf (RPATH) Library rpath: [" + self.PRIVATE + "/lib64]\n"
        with self.readelf(good), self.assertRaisesRegex(ValueError, "not in the staged payload"):
            stage_source.verify_runtime_paths(str(root), self.PRIVATE)

    def test_private_libssl_needs_its_own_runtime_path(self):
        root = self.elf("opt/shift8-php/deps/openssl-3.5.9/lib64/libssl.so.3")
        (root / "opt/shift8-php/deps/openssl-3.5.9/lib64/libcrypto.so.3").write_text("x")
        with self.readelf(self.NEEDS), self.assertRaisesRegex(ValueError, "Private libssl"):
            stage_source.verify_runtime_paths(str(root), self.PRIVATE)
