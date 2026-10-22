import subprocess
import sys
from unittest import mock

from test_milestones import ROOT, TemporaryTest, metadata
from test_source_contract import snapshot

sys.path.insert(0, str(ROOT / "lib"))
import recovery


def discovery(directory):
    data = snapshot()
    for item in data["family"]["packages"]:
        item.update(epoch="0", architecture="x86_64", release="1.el7.remi", version="8.3.8")
    path = directory / "discovery.json"
    metadata.dump(data, path)
    return path


class RecoveryTests(TemporaryTest):
    def result(self, code, out="", err=""):
        return metadata.Result(code, out, err)

    def test_http_repository_url_is_upgraded_and_must_be_unique(self):
        item = dict(name="php-cli", version="8.3.8", release="1.el7.remi", architecture="x86_64")
        with mock.patch.object(recovery.meta, "run", return_value=self.result(0, "http://r.example/a.rpm\nhttp://r.example/a.rpm\n")):
            self.assertEqual(recovery.locations(item, ["remi"]), "https://r.example/a.rpm")
        with mock.patch.object(recovery.meta, "run", return_value=self.result(0, "https://a/x.rpm\nhttps://b/x.rpm\n")):
            with self.assertRaisesRegex(ValueError, "Exactly one"):
                recovery.locations(item, [])
        with mock.patch.object(recovery.meta, "run", return_value=self.result(1, "", "no")):
            with self.assertRaisesRegex(ValueError, "repoquery failed"):
                recovery.locations(item, [])

    def test_repository_ids_cannot_inject_options(self):
        path = discovery(self.directory)
        with self.assertRaisesRegex(ValueError, "Invalid repository"):
            recovery.collect(str(path), str(self.directory / "out"), ["x,--evil"])

    def test_unsigned_or_wrong_package_is_rejected(self):
        path = discovery(self.directory)

        def run(command):
            if command[0] == "repoquery":
                return self.result(0, "https://r.example/php-common.rpm\n")
            if "-qp" in command:
                return self.result(0, "php-common\t(none)\t8.3.8\t1.el7.remi\tx86_64\n")
            return self.result(1, "", "RSA signature: NOKEY")

        with mock.patch.object(recovery.meta, "run", side_effect=run), \
                mock.patch.object(recovery.subprocess, "check_call"):
            with self.assertRaisesRegex(ValueError, "Downloaded RPM is not|signature"):
                recovery.collect(str(path), str(self.directory / "out"))

    def test_verified_exact_packages_are_collected(self):
        path = discovery(self.directory)
        calls = []

        def run(command):
            calls.append(command[0])
            if command[0] == "repoquery":
                return self.result(0, "https://r.example/x.rpm\n")
            if "-qp" in command:
                name = command[-1].rsplit("/", 1)[1].split("-8.3.8", 1)[0]
                return self.result(0, "{}\t(none)\t8.3.8\t1.el7.remi\tx86_64\n".format(name))
            return self.result(0, "Header V4 RSA/SHA256 Signature, key ID abc: OK\n", "")

        with mock.patch.object(recovery.meta, "run", side_effect=run), \
                mock.patch.object(recovery.subprocess, "check_call"):
            recovery.collect(str(path), str(self.directory / "out"))
        self.assertTrue((self.directory / "out").is_dir())
        self.assertNotIn("-U", calls)
