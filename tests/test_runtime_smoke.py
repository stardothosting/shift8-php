import json
import shutil
import re
import subprocess
import unittest

from test_milestones import ROOT, TemporaryTest


@unittest.skipUnless(shutil.which("php"), "PHP CLI is needed for runtime smoke regression tests")
class RuntimeSmokeTests(TemporaryTest):
    def test_openssl_error_identity_retains_library_and_reason(self):
        text = (ROOT / "patches/php-test-portability.patch").read_text()
        match = re.search(r"\+    \$identity = static function \(\$code\) \{\n(.*?)\n\+    \};", text, re.DOTALL)
        self.assertIsNotNone(match)
        body = "\n".join(line[1:] for line in match.group(1).splitlines())
        code = "$identity = static function ($code) {\n" + body + "\n};\n"
        for first, second, equal in (
            ("0909006C", "0906D06C", True),
            ("04099079", "040A1079", True),
            ("0909006C", "0806D06C", False),
            ("0909006C", "0906D06D", False),
        ):
            code += "if (($identity('{}') === $identity('{}')) !== {}) exit(1);\n".format(
                first, second, "true" if equal else "false",
            )
        result = subprocess.run([shutil.which("php"), "-n", "-r", code], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_smoke(self, modules=None, target=None):
        php = shutil.which("php")
        version = subprocess.check_output([php, "-n", "-r", "echo PHP_VERSION;"], text=True)
        baseline = self.write("baseline.json", json.dumps(dict(
            runtime=dict(loaded_extensions=modules if modules is not None else ["Core", "json"]),
        )))
        return subprocess.run(
            [php, "-n", str(ROOT / "tests/runtime-smoke.php"), target or version, str(baseline)],
            text=True, capture_output=True,
        )

    def test_minimal_runtime_smoke_reports_only_smoke_success(self):
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["application_health"], "not-tested")

    def test_missing_baseline_module_is_an_error(self):
        result = self.run_smoke(modules=["Core", "shift8_missing_module"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Missing installed module", result.stdout + result.stderr)

    def test_wrong_target_version_is_an_error(self):
        result = self.run_smoke(target="0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected PHP version", result.stdout + result.stderr)
