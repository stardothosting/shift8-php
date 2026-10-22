import sys
import unittest

from test_milestones import ROOT

sys.path.insert(0, str(ROOT / "lib"))
import configure_source


class ConfigureSourceTests(unittest.TestCase):
    def test_supported_array_preserves_enable_disable_and_with_without(self):
        arguments = ["--disable-all", "--enable-cli", "--without-pear", "--prefix=/opt/php83"]
        configure_source.validate(arguments, "--enable-all --enable-cli --with-pear --prefix=PREFIX")
        configure_source.verify_nice(arguments,
            "# generated\nCFLAGS='-O2' \\\n'./configure' \\\n'--disable-all' \\\n'--enable-cli' \\\n'--without-pear' \\\n'--prefix=/opt/php83' \\\n\"$@\"")
        configure_source.validate(["--disable-all"], "--disable-all Disable all extensions")
        configure_source.validate(["--enable-cli"], "--disable-cli Disable CLI")

    def test_bare_separator_truncation_duplicate_and_unknown_are_rejected(self):
        for arguments in (["--"], ["--enable-cli", "--disable-cli"], ["--unknown"], ["--enable-cli\n--with-zip"]):
            with self.assertRaises(ValueError):
                configure_source.validate(arguments, "--enable-cli --with-zip")
        with self.assertRaisesRegex(ValueError, "exact effective"):
            configure_source.verify_nice(["--enable-cli", "--with-zip"], "'./configure' '--enable-cli' \"$@\"")

    def test_config_nice_is_parsed_not_executed(self):
        with self.assertRaisesRegex(ValueError, "exact effective"):
            configure_source.verify_nice(["--enable-cli"], "'./configure' '--enable-cli' '--with-curl' $(dangerous)")
