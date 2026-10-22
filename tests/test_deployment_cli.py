import json
import subprocess
import sys
from unittest import mock

from test_milestones import ROOT, TemporaryTest, CommandFixture

sys.path.insert(0, str(ROOT / "lib"))
import deployment_cli


class DeploymentCliTests(TemporaryTest):
    def policy(self):
        return self.write("policy.json", json.dumps(dict(
            schema_version=1, reviewed=True, package="php83-php-cli",
            recovery_dir="/private/recovery", state_dir="/private/state",
            service=dict(name="php83-php-fpm.service", listeners=[dict(type="tcp", address="127.0.0.1", port=9005)]),
            health_commands=[["/usr/bin/curl", "--fail", "https://localhost/health"]],
        )))

    def test_default_calls_only_nonapplying_deployment_plan(self):
        path = self.policy()
        with mock.patch.object(deployment_cli.deployment, "plan", return_value={"apply": False}) as plan:
            deployment_cli.main("deploy", "php83-php-cli", "/manifest", "/discovery", str(path), False)
        self.assertFalse(plan.call_args[1]["apply"])

    def test_recovery_does_not_need_a_working_live_php(self):
        path = self.policy()
        with mock.patch.object(deployment_cli.deployment, "rollback", return_value={}) as rollback:
            deployment_cli.main("rollback", "php83-php-cli", "", "", str(path), False)
        rollback.assert_called_once_with("/private/state", apply=False)

    def test_unreviewed_or_different_target_policy_is_blocked(self):
        path = self.policy()
        with self.assertRaisesRegex(ValueError, "exact selected"):
            deployment_cli.main("deploy", "php-cli", "/manifest", "/discovery", str(path), True)
        value = json.loads(path.read_text())
        value["reviewed"] = False
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "explicit review"):
            deployment_cli.main("deploy", "php83-php-cli", "/manifest", "/discovery", str(path), True)


class ManagedCommandGates(CommandFixture):
    def test_apply_cannot_accidentally_enable_prepare(self):
        result = self.cli("prepare", "php-cli", "--apply")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls())

    def test_source_build_requires_verified_manifest_and_environment_before_discovery(self):
        result = self.cli("build", "php-cli")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.calls())

    def test_rollback_does_not_start_discovery_when_live_runtime_is_broken(self):
        policy = self.write("policy.json", json.dumps(dict(
            schema_version=1, reviewed=True, package="php-cli", state_dir=str(self.directory / "missing"),
        )))
        result = self.cli("rollback", "php-cli", "--deployment-policy=" + str(policy), MOCK_VERSION="broken")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls())

    def test_mutating_failure_message_does_not_claim_host_is_unchanged(self):
        result = subprocess.run(
            ["bash", "-c", 'source "$1/lib/common.sh"; HOST_MUTATION_POSSIBLE=1; die 7 "failed"', "bash", str(ROOT)],
            text=True, capture_output=True,
        )
        self.assertNotIn("was not modified", result.stderr)
        self.assertIn("may have changed", result.stderr)
