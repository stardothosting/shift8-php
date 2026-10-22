import sys
from unittest import mock

from test_milestones import ROOT, TemporaryTest, metadata
from test_recovery import discovery

sys.path.insert(0, str(ROOT / "lib"))
import environment_setup


class EnvironmentSetupTests(TemporaryTest):
    def test_configuration_pins_collection_support_and_disables_bootstrap(self):
        data = metadata.load(discovery(self.directory))
        data["family"]["source_name"], data["runtime"]["branch"] = "php83-php", "8.3"
        text = environment_setup.configuration(data, "shift8-php83-el7-x86_64")
        self.assertIn("php83-build", text)
        self.assertIn("[remi-php83]", text)
        self.assertIn('config_opts["use_bootstrap"] = False', text)
        self.assertNotIn("gpgcheck=0", text)

    def test_only_the_maintained_openssl_series_is_accepted(self):
        path = discovery(self.directory)
        for version in ("3.0.1", "1.1.1w", "3.5", "3.5.x"):
            with self.assertRaises(ValueError):
                environment_setup.generate(str(path), str(self.directory / "env"), version, "1")

    def test_digest_sidecar_must_be_a_sha256(self):
        with mock.patch.object(environment_setup.meta, "run", return_value=metadata.Result(0, "nothing", "")):
            with self.assertRaises(ValueError):
                environment_setup.official_digest("https://example.invalid/x.sha256")
