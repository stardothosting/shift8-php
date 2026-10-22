# shift8-php

Inspect packaged PHP installations, find releases in the same PHP branch, and
prepare sources for rebuilding distribution packages.

**This tool does not install PHP updates.** Discovery and source preparation
are implemented. There is an experimental RPM build driver, but no validated
end-to-end CentOS 7.9 build workflow yet. `deploy` and `rollback` are disabled.

The goal is to update PHP without changing its package names, installation paths,
extensions or service configuration. The current code is a starting point for
that workflow, not a replacement for a distribution's package updater.

## Commands

| Command | Current behavior |
| --- | --- |
| `discover <package>` | Inspect the installed package family and its PHP runtime; print JSON. |
| `prepare <package>` | Select an upstream release, check repositories and record a candidate update or download source inputs. |
| `prepare <package> --build-profile=...` | Attempt an isolated mock build using a supplied, reviewed spec, patches and configuration. |
| `check <package> --manifest=...` | Verify saved artifact checksums and compare the recorded installation fingerprint with this host. |
| `deploy <package>` | Not implemented; exits without installing anything. |
| `rollback <package>` | Not implemented; exits without changing anything. |

Use an exact installed CLI package name, such as `php-cli`, `php83-php-cli` or
`php8.2-cli`. The tool finds the executable through package ownership rather than
assuming that the default `php` command belongs to the requested installation.
It stops if it cannot resolve a single CLI executable.

## Getting started

Run from the checkout; there is no installer.

```sh
./shift8-php discover php-cli
./shift8-php prepare php-cli --dry-run
```

`discover` uses local package and runtime information. `prepare --dry-run` also
queries PHP release information and configured repositories, but does not
download source archives, build packages or write a persistent artifact store.
Both commands start the selected PHP CLI with its existing configuration and
extensions. Neither installs packages or restarts services.

To save preparation results, use a private directory owned by your account:

```sh
./shift8-php prepare php-cli --artifact-dir="$HOME/shift8-php-artifacts"
```

If a repository lists a candidate that reaches the upstream target version,
preparation records it in a `native-update-review` manifest. It does not download
or install the binary packages. Otherwise it attempts source retrieval.
`--source-only` requests source retrieval regardless of repository candidates.

### CentOS 7.9 and Remi

For a standard Remi installation, start with the commands above. For a parallel
installation, supply its own CLI package:

```sh
./shift8-php discover php83-php-cli
./shift8-php prepare php83-php-cli --dry-run
```

Discovery and preparation do not update either installation. The build driver is
currently restricted to standard Remi PHP 8.2 on CentOS 7, not parallel Remi.
It requires an existing mock environment and a reviewed set of final packaging
files. No ready-to-use CentOS 7 profile or finalized compatibility patches are
included.

See [Building Remi RPMs with mock](docs/mock-builds.md) for the profile format,
dependency queries, compiler setup, patch checks and build logs.

## Requirements

- Linux and Bash 4.1 or newer.
- Python 3.6 or newer, or Python 2.7, using only the standard library.
- The installed PHP runtime, its RPM or dpkg database, and `ldd` for library
  inspection.
- System `tar` with XZ support, `mktemp` and the usual Linux command-line tools.
- For online preparation: curl with HTTPS support and working CA certificates.
- For saved preparation: `flock` and a writable artifact directory.

RPM repository queries need `dnf repoquery` or Yum's standalone `repoquery`.
Debian source retrieval needs `apt-get`, configured `deb-src` indices, `gpgv` and
a trusted keyring containing the source signer's key. Isolated RPM builds also
need mock, RPM tools and `setsid`.

Missing tools are reported, not installed automatically. Python 3 is preferred;
the Python 2.7 path is there for older systems such as CentOS 7. It does not
extend the support lifetime of Python or the operating system.

## Release and source selection

The target is the latest stable release returned by PHP's official release
service for the installed major/minor branch. PHP 8.2 stays on 8.2. An explicit
`--target=X.Y.Z` must be published, in the same branch, not a downgrade, and not
older than the latest security-tagged release in the returned metadata.

Unsupported upstream branches are rejected. Building their last historical
release is not treated as a current security fix. Vendor advisories and
downstream backports are not evaluated automatically; a higher package version
or reachable repository is not enough to establish CVE coverage.

Source preparation checks:

- **Upstream PHP:** the archive must match the SHA-256 published by PHP's release
  service and contain the expected version header.
- **RPM packaging:** first try to retrieve the exact installed SRPM, verify its
  signature against installed trusted keys, and check its source identity.
  Search relevant source repositories, including disabled ones, before checking
  an exact-match binary index that might also contain sources. Provider routing
  uses matching package metadata, repository endpoints, configuration files and
  signing-key references; it does not enable every archived repository on the
  host. If the original SRPM is gone but the provider exposes a version-matched
  historical packaging tag, preparation can stage those packaging files beside
  the new upstream PHP tarball instead of failing immediately.
- **Debian packaging:** verify the exact source package's signed `.dsc`, then
  download and verify every declared SHA-256 member.

For Remi packages, preparation also checks the provider's package-specific
source page when native indexes return no usable location. This is a bounded
metadata lookup, not a full repository listing or Git clone. Only a link naming
the exact installed SRPM is accepted. An HTTP artifact location is tried over
HTTPS at the same host/path, with certificate and RPM signature checks retained.

An old `SOURCERPM` reference does not guarantee that the source is still online.
If the exact file is missing, newer releases or source packages without the
original distribution suffix are not substituted. For supported providers such
as Remi, preparation next tries the package family's historical packaging tag
for the installed PHP version and stages its root packaging files over HTTPS.
That fallback is provider-published metadata, not an SRPM signature, and still
requires review before any real rebuild claim.

If neither the original SRPM nor a bounded provider packaging tag is available,
`--source-map` accepts a reviewed list of HTTPS URLs and independently verified
hashes. It is an explicit alternative, not automatic archive or full Git
history discovery.
A map has this form:

```json
{
  "installed_source": "php-8.2.20-1.el7.remi.src.rpm",
  "reviewed": true,
  "files": [
    {
      "name": "php82.spec",
      "url": "https://git.remirepo.net/cgit/rpms/php/php82.git/plain/php82.spec?h=v8.2.20",
      "sha256": "REPLACE_WITH_INDEPENDENTLY_VERIFIED_SHA256"
    }
  ]
}
```

Include all required patches and auxiliary files, not just the spec. The example
hash is a placeholder. Map inputs are trusted on the operator's review, not
represented as vendor-signed material.

## Options

| Option | Purpose |
| --- | --- |
| `--target=X.Y.Z` | Choose a published patch release within the installed branch. |
| `--artifact-dir=PATH` | Store preparation results outside the default `/var/lib/shift8-php`. |
| `--dry-run` | Inspect and plan without saving preparation results or building. |
| `--source-only` | Retrieve sources even when a repository candidate exists. |
| `--source-map=PATH` | Supply reviewed packaging URLs and SHA-256 values. |
| `--source-keyring=PATH` | Use a trusted keyring for Debian source signatures. |
| `--build-profile=PATH` | Opt into the experimental mock build driver with a real local reviewed profile path; omit it for source preparation only. |
| `--manifest=PATH` | Select a saved manifest for `check`. |
| `--offline` | Use verified cached inputs without network requests. |
| `--ipv4` | Force IPv4 for curl downloads and RPM repository queries. |
| `--verbose` | Print additional diagnostics. |

`--yes` is reserved and does not enable deployment. `--build-only`,
`--force-rebuild` and `--allow-prerelease` are not supported.

## Artifacts and compatibility checks

Preparation prints the path to its manifest. The store contains per-run build
directories, cached downloads and logs; unsuccessful runs may leave diagnostics
and partial downloads but no success manifest.

For RPM source lookup failures, the run's `source-discovery` directory records
the selected/skipped repositories, query output, exit codes, stderr and any
provider metadata checked. An empty successful query is reported separately from
a failed metadata request. On CentOS 7, this avoids a slow sweep through unrelated
disabled CentOS Vault and Webtatic repositories. It does not repair those
repositories or make a missing historical source package available.

The store and its standard subdirectories must be owned by the invoking account
and have mode `0700`. Its parent directory must already exist. Symlinks,
hard-linked files and entries writable by other users are rejected.

Manifests record package and runtime discovery, release metadata, source
authentication and file checksums. Build manifests also include compiler facts,
input hashes, phase logs and RPM metadata. Every current manifest has
`deployable: false`. The manifests themselves are unsigned.

```sh
./shift8-php check php-cli --manifest=/path/to/build/manifest.json
```

This compares the saved fingerprint with the current installation: distribution,
architecture, PHP family and ABI, recorded modules, libraries, package
relationships and configuration hashes. It checks recorded inputs, not every
possible external loader, FPM pool or web-server setting. A match does not test
the new binaries or establish that an application can use them.

Unsigned cached metadata expires after 24 hours. Checksum-pinned source downloads
can resume and must pass final verification. Downloads use HTTPS, certificate
validation, timeouts and bounded retries, with IPv4 fallback for transient network
failures. Offline preparation cannot discover releases published since the cache
was populated; offline mock builds are not supported.

## Testing and remaining work

```sh
make test       # Offline Python regression suite
make lint       # Bash syntax, ShellCheck and shfmt
make bats       # CLI checks and the Python suite
make legacy     # Python 2.7 tests and CLI smoke test in Docker
```

The tests include the supplied CentOS 7/Remi package inventory, parallel PHP
families, Debian package metadata, source verification, network failures and mock
build sequencing. The mock tests use simulated commands; they do not compile PHP.
The legacy test runs a real Python 2.7 interpreter, but its PHP/package data is
also simulated.

Native read-only discovery has been exercised on Ubuntu 24.04 with PHP 8.3.
GitHub Actions defines additional discovery jobs for AlmaLinux 8/9, Debian 12
and Ubuntu 24.04. Those jobs are not evidence of a tested package replacement.

The main unfinished work is a real CentOS 7/Remi build using the complete reviewed
patch set, followed by binary and extension tests, FPM/application checks, package
transaction previews, installation, backups and rollback. Automatic patch
adaptation, development-package swaps and private package signing are also not
implemented.

Contribution and disclosure notes are in [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md). A license has not been selected yet.
