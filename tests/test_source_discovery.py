import json
import os
import subprocess

from test_milestones import ROOT, TemporaryTest, metadata


class SourceDiscoveryTests(TemporaryTest):
    def inventory(self):
        source = "php83-php-8.3.8-1.el7.remi.src.rpm"
        discovery = dict(family=dict(
            source_name="php83-php", source_reference=source,
            packages=[dict(vendor="Remi's RPM repository <https://rpms.remirepo.net/>")],
        ))
        report = dict(candidates=[dict(repository="remi-safe", source=source)])
        self.write("etc/yum.conf", "[main]\ngpgcheck=1\n")
        self.write(
            "etc/yum.repos.d/remi-safe.repo",
            "[remi-safe]\nenabled=1\nbaseurl=http://rpms.remirepo.net/enterprise/7/safe/$basearch/\n"
            "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-remi\n"
            "[remi-safe-debuginfo]\nenabled=0\nbaseurl=http://rpms.remirepo.net/enterprise/7/debug-remi/$basearch/\n",
        )
        self.write(
            "etc/yum.repos.d/CentOS-Sources.repo",
            "[base-source]\nenabled=0\nbaseurl=http://vault.centos.org/centos/$releasever/os/Source/\n"
            "[updates-source]\nenabled=0\nbaseurl=http://vault.centos.org/centos/$releasever/updates/Source/\n",
        )
        self.write(
            "etc/yum.repos.d/CentOS-Vault.repo",
            "".join(
                "[C7.{}.{}-{}]\nenabled=0\nbaseurl=http://vault.centos.org/centos/7.{}/os/$basearch/\n".format(
                    minor, 1406 + minor, group, minor,
                )
                for minor in range(10) for group in ("base", "extras", "updates")
            ),
        )
        self.write(
            "etc/yum.repos.d/webtatic.repo",
            "[webtatic-archive-source]\nenabled=0\nbaseurl=https://repo.webtatic.com/yum/el7-archive/SRPMS/\n",
        )
        return discovery, metadata.repository_config("rpm", str(self.directory)), report

    def test_centOS7_parallel_remi_does_not_scan_unrelated_vault_and_webtatic_indexes(self):
        discovery, config, report = self.inventory()
        plan = metadata.source_repositories(discovery, config, report)
        self.assertEqual(plan["source_repositories"], [])
        self.assertEqual(plan["mixed_repositories"], ["remi-safe"])
        self.assertIn("base-source", plan["excluded_repositories"])
        self.assertIn("webtatic-archive-source", plan["excluded_repositories"])
        self.assertEqual(len(plan["excluded_repositories"]), 34)
        self.assertEqual(metadata.provider_source_index(discovery, plan),
                         "https://rpms.remirepo.net/SRPMS/repoview/php83-php.html")

    def test_disabled_provider_sources_precede_exact_binary_index(self):
        discovery, _, report = self.inventory()
        self.write(
            "etc/yum.repos.d/remi-source.repo",
            "[remi-source]\nenabled=0\nbaseurl=https://rpms.remirepo.net/SRPMS/\n",
        )
        config = metadata.repository_config("rpm", str(self.directory))
        plan = metadata.source_repositories(discovery, config, report)
        self.assertEqual(plan["source_repositories"], ["remi-source"])
        self.assertEqual(plan["mixed_repositories"], ["remi-safe"])

    def test_custom_repository_id_is_recognized_by_source_endpoint(self):
        discovery, _, report = self.inventory()
        self.write("etc/yum.repos.d/custom.repo",
                   "[historical-packages]\nenabled=0\nbaseurl=https://rpms.remirepo.net/SRPMS/\n")
        plan = metadata.source_repositories(
            discovery, metadata.repository_config("rpm", str(self.directory)), report,
        )
        self.assertEqual(plan["source_repositories"], ["historical-packages"])

    def test_same_reviewed_key_links_provider_source_cdn_to_binary_repository(self):
        discovery, _, report = self.inventory()
        self.write(
            "etc/yum.repos.d/remi-cdn.repo",
            "[remi-source]\nenabled=0\nbaseurl=https://sources.example.test/SRPMS/\n"
            "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-remi\n",
        )
        plan = metadata.source_repositories(
            discovery, metadata.repository_config("rpm", str(self.directory)), report,
        )
        self.assertEqual(plan["source_repositories"], ["remi-source"])

    def test_unrecognized_provider_still_uses_explicit_source_repositories(self):
        discovery = dict(family=dict(source_reference="php-8.2.20-1.src.rpm", packages=[]))
        config = dict(configured=[
            dict(id="vendor-source", enabled=False, endpoints=[]),
            dict(id="binary-only", enabled=True, endpoints=[]),
        ])
        plan = metadata.source_repositories(discovery, config, dict(candidates=[]))
        self.assertEqual(plan["source_repositories"], ["vendor-source"])
        self.assertEqual(plan["mixed_repositories"], [])
        self.assertEqual(metadata.provider_source_index(discovery, plan), "")

    def test_standard_remi_and_other_distributions_do_not_use_parallel_names(self):
        discovery, config, report = self.inventory()
        discovery["runtime"] = dict(version="8.2.20")
        discovery["family"]["source_name"] = "php"
        discovery["family"]["source_reference"] = "php-8.2.20-1.el7.remi.src.rpm"
        report["candidates"][0]["source"] = discovery["family"]["source_reference"]
        plan = metadata.source_repositories(discovery, config, report)
        self.assertTrue(metadata.provider_source_index(discovery, plan).endswith("/php.html"))
        self.assertEqual(
            metadata.provider_packaging_tree(discovery, plan),
            "https://git.remirepo.net/cgit/rpms/php/php.git/tree/?h=v8.2.20",
        )

    def test_parallel_remi_packaging_tree_uses_installed_upstream_tag(self):
        discovery, config, report = self.inventory()
        discovery["runtime"] = dict(version="8.3.8")
        plan = metadata.source_repositories(discovery, config, report)
        self.assertEqual(
            metadata.provider_packaging_tree(discovery, plan),
            "https://git.remirepo.net/cgit/rpms/php/php83.git/tree/?h=v8.3.8",
        )

    def test_repository_credentials_and_query_tokens_are_not_persisted_in_routing_evidence(self):
        self.write(
            "etc/yum.repos.d/private.repo",
            "[private-source]\nbaseurl=https://user:secret@packages.example.test/SRPMS/?token=secret\n"
            "gpgkey=https://keys.example.test/vendor.key?token=secret\n",
        )
        config = metadata.repository_config("rpm", str(self.directory))
        self.assertNotIn("secret", json.dumps(config))
        self.assertEqual(config["configured"][0]["endpoints"][0]["host"], "packages.example.test")

    def test_http_source_metadata_is_upgraded_without_changing_the_exact_filename(self):
        source = "php83-php-8.3.8-1.el7.remi.src.rpm"
        self.assertEqual(
            metadata.source_location("http://example.test/SRPMS/" + source, source),
            "https://example.test/SRPMS/" + source,
        )
        for location in (
            "file:///tmp/" + source,
            "https://user:password@example.test/" + source,
            "https://example.test/foreign-8.3.8-1.el9.src.rpm",
            "https://example.test/" + source + "\n",
        ):
            with self.subTest(location=location), self.assertRaises(ValueError):
                metadata.source_location(location, source)

    def test_vendor_page_selects_only_exact_original_srpm_not_newer_or_different_release(self):
        source = "php83-php-8.3.8-1.el7.remi.src.rpm"
        path = self.write(
            "provider.html",
            '<a href="../{}">exact</a><a href="../{}">duplicate</a>'
            '<a href="../php83-php-8.3.36-1.remi.src.rpm">newer</a>'
            '<a href="../php83-php-8.3.8-1.remi.src.rpm">different release</a>'
            '<a href="https://unrelated.example.test/{}">external</a>'.format(source, source, source),
        )
        url = "https://rpms.remirepo.net/SRPMS/repoview/php83-php.html"
        self.assertEqual(metadata.provider_source_locations(str(path), url, source),
                         ["https://rpms.remirepo.net/SRPMS/" + source])
        path.write_text('<html><h1>404 Not Found</h1></html>')
        self.assertEqual(metadata.provider_source_locations(str(path), url, source), [])

    def test_provider_packaging_tree_selects_only_root_plain_files_for_exact_tag(self):
        path = self.write(
            "packaging-tree.html",
            '<a href="/cgit/rpms/php/php83.git/plain/php83.spec?h=v8.3.8">spec</a>'
            '<a href="/cgit/rpms/php/php83.git/plain/php.ini?h=v8.3.8">ini</a>'
            '<a href="/cgit/rpms/php/php83.git/plain/.gitignore?h=v8.3.8">dotfile</a>'
            '<a href="/cgit/rpms/php/php83.git/plain/php83.spec?h=v8.3.8">dup</a>'
            '<a href="/cgit/rpms/php/php83.git/plain/patches/fix.patch?h=v8.3.8">subdir</a>'
            '<a href="/cgit/rpms/php/php83.git/plain/php83.spec?h=v8.3.36">wrong-tag</a>'
            '<a href="https://unrelated.example.test/php83.spec?h=v8.3.8">external</a>',
        )
        url = "https://git.remirepo.net/cgit/rpms/php/php83.git/tree/?h=v8.3.8"
        self.assertEqual(
            metadata.provider_packaging_files(str(path), url),
            [
                ("php.ini", "https://git.remirepo.net/cgit/rpms/php/php83.git/plain/php.ini?h=v8.3.8"),
                ("php83.spec", "https://git.remirepo.net/cgit/rpms/php/php83.git/plain/php83.spec?h=v8.3.8"),
            ],
        )

    def test_vendor_metadata_size_is_bounded(self):
        path = self.write("large.html", " " * 524289)
        with self.assertRaisesRegex(ValueError, "512 KiB"):
            metadata.provider_source_locations(str(path), "https://rpms.remirepo.net/page.html", "php.src.rpm")

    def test_dnf_and_yum_use_their_own_native_log_options(self):
        binaries = self.directory / "bin"
        binaries.mkdir()
        for tool in ("dnf", "repoquery"):
            script = binaries / tool
            script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
            script.chmod(0o755)
        for tool in ("dnf", "repoquery"):
            command = (
                'set -Eeuo pipefail; source "$1/lib/repositories.sh"; '
                'WORK=$2; IPV4=0; REPO_TOOL=$3; rpm_query custom-source sources php-8.2.20-1'
            )
            result = subprocess.run(
                ["bash", "-c", command, "_", str(ROOT), str(self.directory), tool],
                env=dict(os.environ, PATH=str(binaries) + ":" + os.environ["PATH"]),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual("logdir=" in result.stdout, tool == "dnf")
            self.assertEqual("logfile=" in result.stdout, tool == "repoquery")
            if tool == "repoquery":
                self.assertIn("--show-duplicates", result.stdout)
