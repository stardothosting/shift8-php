# Isolated CentOS 7 / Remi builds

This is an **experimental build driver**, not certification that PHP 8.2.35 or
any future release is production-safe on EL7. Its orchestration is regression
tested with a mock-command fixture. A parallel Remi PHP 8.3.36 build has also
completed on CentOS 7.9 using reviewed current packaging and EL7 compatibility
changes. Compilation alone does not validate a production upgrade.

## Verified PHP 8.3.36 exercise

The repository recipe has completed a fresh full build on CentOS 7.9 for the
parallel Remi family. A separate disposable root was populated with the original
PHP 8.3.8 RPMs and the seven exact, signature-verified installed PECL versions,
then upgraded with the 16 selected core replacement RPMs.

The exercise passed:

- Manifest/digest verification and `rpm --test` against the unchanged live host.
- An actual upgrade in the disposable root, retaining all 59 baseline modules.
- The runtime smoke and packaged FPM configuration check after that upgrade.
- Six upstream OpenSSL peer/SAN/name-verification tests, with no skips, including
  IPv4/IPv6 SAN handling and rejection of a mismatched peer name.
- An additional smoke with the built FFI and intl RPMs installed **only** in the
  disposable root, exercising FFI directives and the ICU 73 path.
- A rollback to the original RPMs in that root, followed by the baseline runtime
  smoke and packaged FPM configuration check.

The full provider PHP suite still reported three failures:
`openssl_error_string_basic.phpt`, `bug74589_utf8.phpt`, and
`php_cli_server_pdeathsig.phpt`. They are not waived by the successful targeted
checks. Review the retained diffs before deployment; the first concerns error
codes returned by the old OpenSSL, while the others concern Unicode paths and
CLI server worker shutdown. These observations are not a production safety
claim or permission to discard the tests.

Production PHP remained at 8.3.8 and its FPM start timestamp did not change.
Custom production pools, application traffic and external data/cache services
were not exercised. The RPMs remain local unsigned, non-deployable artifacts.
The rollback rehearsal does not replace production configuration backups or a
reviewed maintenance/recovery plan. Other distributions/families and future
upstream releases still need their own native validation.

## Responsibilities and adapter boundaries

- The Bash CLI/native adapters discover only the requested runtime family.
- The metadata helper supports Python 2.7 and Python 3.6+, with native `tar` for
  XZ archive inspection. A real Python 2.7 container smoke covers metadata and
  full CLI discovery with Python 3 unavailable. Python itself remains a dependency.
- The source adapter authenticates PHP release bytes and tries to authenticate
  original RPM packaging first. Exact SRPM retrieval selects provider-related
  source indexes (enabled or disabled) and exact-match binary indexes that
  might also publish sources. Remi's package-specific source page is checked as
  a separate fallback; exact SRPM downloads still require the original source
  identity and a trusted RPM signature. If the old SRPM has disappeared but the
  provider publishes a version-matched historical packaging tag, the adapter can
  stage those root packaging files over HTTPS next to the new upstream tarball.
  That fallback is not equivalent to a signed SRPM and does not attest file-set
  completeness. If all of that fails, use a reviewed source map for historical
  individual files/patches or independently verified archived material. There is
  no URL guessing or clone retry loop. Source-query output, exit codes and
  errors are retained under `source-discovery`; empty results are not labelled
  network failures. Yum uses its supported `logfile` setting, while DNF uses
  `logdir`.
- The build adapter accepts explicitly reviewed final packaging. When a build
  profile is supplied, its pinned final inputs are the packaging source; there
  is no separate historical SRPM/tag download requirement. A simultaneous
  `--source-map` is not used for that build and a diagnostic explains precedence.
  The adapter does not rewrite spec text based on guessed `%patch47` syntax. Native `rpmspec` inside
  mock expands the actual target RPM macros; the expanded `%prep` script records
  the application order for `%autosetup`, `%autopatch`, `%patch` or numbered
  declarations as supported by that root's RPM version. Unsupported macros fail.
- The artifact adapter records and selects replacements for **installed core
  packages only**. Extra built packages are explicitly excluded and PECL packages
  remain listed for retention. Runtime validation and deployment remain separate
  unfinished stages.

## Reviewed profile

First run discovery and obtain `compatibility.sha256`:

```sh
./shift8-php discover php-cli
```

The fingerprint must be complete. It includes the actual distribution, package
versions/releases/epochs, ABI/ZTS, loaded modules, runtime file/configuration
hashes, runtime libraries and Requires/Provides. Identical PHP version strings
alone are insufficient. Service state, memory, free disk and timestamps do not
make otherwise identical hosts incompatible.

Prepare a local profile. The following placeholders are intentionally invalid:

```json
{
  "schema_version": 1,
  "reviewed": true,
  "configuration_closure_reviewed": true,
  "signature_policy_reviewed": true,
  "compatibility_sha256": "REPLACE_WITH_DISCOVERY_FINGERPRINT",
  "installed_source": "php-8.2.20-1.el7.remi.src.rpm",
  "target": "8.2.35",
  "mock_config": {
    "path": "/absolute/reviewed/el7-remi.cfg",
    "sha256": "REPLACE_WITH_REVIEWED_CONFIG_SHA256"
  },
  "trust_files": [
    {
      "path": "/absolute/reviewed/RPM-GPG-KEY-CentOS-SIG-SCLo",
      "sha256": "REPLACE_WITH_INDEPENDENTLY_VERIFIED_KEY_SHA256"
    }
  ],
  "compiler": {
    "cc": "/opt/rh/devtoolset-10/root/usr/bin/gcc",
    "cxx": "/opt/rh/devtoolset-10/root/usr/bin/g++",
    "major": 10
  },
  "jobs": 2,
  "inputs": [
    {
      "name": "php82.spec",
      "role": "spec",
      "path": "/absolute/reviewed/final/php82.spec",
      "sha256": "REPLACE_WITH_REVIEWED_FINAL_SPEC_SHA256",
      "original": {
        "path": "/absolute/reviewed/original/php82.spec",
        "sha256": "REPLACE_WITH_AUTHENTICATED_ORIGINAL_SPEC_SHA256"
      },
      "review": "Reviewed target/version/release, sources, original vendor behavior and final patch order"
    },
    {
      "name": "php-openssl-compat.patch",
      "role": "patch",
      "path": "/absolute/reviewed/final/php-openssl-compat.patch",
      "sha256": "REPLACE_WITH_REVIEWED_PATCH_SHA256",
      "review": "Reviewed every accessor occurrence, OpenSSL version gate, ownership, mutability and lifetime"
    }
  ]
}
```

`inputs` must contain the complete final spec and every referenced auxiliary/
patch/source, **except** the authenticated target `php-X.Y.Z.tar.xz`, which is
staged automatically and cannot be replaced by a profile. Roles are `spec`,
`source`, or `patch`. There must be exactly one spec and no duplicate filenames.
Every patch needs a semantic review note. Adapted files can record an original
path/digest; originals are preserved separately. No version or compiler path in
this example is an unconditional project default.

The mock configuration must already exist. Review **all** transitive includes,
site defaults, macros, plugin settings, mounts, repository definitions and archive
key identities; pin the relevant files in `trust_files`. Configuration is
executable, privileged input: a checksum and `reviewed: true` are operator
attestations, not an independent safety audit. The driver rechecks pins at every
phase; it does not infer which includes your configuration imports.

The profile must preserve package signature verification, TLS verification and
buildroot isolation. Independently authenticate the archived CentOS SCL and vendor
keys; do not trust a key solely because it was fetched next to an RPM. Keep
obsolete mirror lists out of **this isolated configuration** and use only reviewed
archive endpoints. Do not disable host repositories, change their priorities,
upgrade the host's OpenSSL/ICU or install a host compiler.

Include the selected devtoolset packages in the mock root's reviewed setup. The
driver checks both C and C++ compiler major versions **inside** that root, then
scopes `PATH`, `CC` and `CXX` to their directory during RPM phases. The system
default compiler is unchanged. If a spec overrides these settings, review the
expanded spec and build output rather than claiming the requested compiler was
necessarily used by every subordinate build.

## Running

```sh
# Validate discovery, authoritative target and reviewed profile without building:
./shift8-php prepare php-cli --build-profile=/absolute/reviewed/profile.json \
  --artifact-dir="$HOME/shift8-php-artifacts" --dry-run

# Explicit opt-in; build dependencies are installed only into the mock root:
./shift8-php prepare php-cli --build-profile=/absolute/reviewed/profile.json \
  --artifact-dir="$HOME/shift8-php-artifacts"
```

The profile supplies the complete final packaging, even when the old installed
SRPM is no longer retrievable. For source-only preparation without a profile,
`--source-map` remains an alternative to SRPM retrieval. Reviewed packaging is
not a claim of vendor signature authentication. No downloaded build script is
executed on the host.

### Parallel PHP 8.3 compatibility recipe

`lib/build_metadata.py el7-profile` turns an existing reviewed **current-target**
profile into a separate, pinned EL7 profile. It requires a prepared build's
discovery/release metadata, matching profile pins, and a new output directory.
It neither downloads a guessed source RPM nor bypasses an unverifiable signature.
Review the provider packaging and repository patch before running this helper:

```sh
python3 lib/build_metadata.py el7-profile \
  /absolute/reviewed/current-profile.json \
  /absolute/prepared/build-directory \
  /absolute/new/el7-inputs
# Inspect el7-inputs/profile.json, the final spec and preserved originals.
./shift8-php prepare php83-php-cli \
  --build-profile=/absolute/new/el7-inputs/profile.json \
  --artifact-dir=/var/lib/shift8-php
```

The recipe is limited to parallel Remi PHP 8.3 on CentOS 7. It stops if expected
spec contexts drift, appear twice, or the patch name/number is already used.
All existing patch inputs are retained; the final spec and compatibility patch
are checksum-pinned, and the original spec is preserved separately.

- Uses iODBC instead of requesting EL7's missing `pkgconfig(odbc)` capability.
  This changes the optional ODBC build, not an application compatibility promise.
  Do not install that extra RPM, especially over an existing ODBC installation,
  without separate driver/application testing.
- Uses ICU 73 on EL7, as available from Remi, while retaining ICU 74 requirements
  elsewhere. PHP's intl behavior must be exercised against the resolved library.
- Applies [the source patch](../patches/php83-el7.patch) with zero fuzz after
  the provider's patch 49. Explicit `strncmp` lengths preserve `ZEND_STRL`
  semantics with EL7 fortify macros. The OpenSSL <1.1 fallback borrows the ASN1
  string's data as const without changing certificate-verification logic.
- Does not disable fortification, skip the test suite, drop provider patches,
  install host libraries or increase build parallelism.

The helper inherits the base profile's review attestations. It is not an
automatic review: inspect the generated files before building, and repeat the
review when upstream packaging or source changes. A future PHP patch release
still has to pass native spec identity, dependency resolution, zero-fuzz patch
application, the build/test phases and runtime validation.

Builds are gated to **CentOS 7 with reviewed Remi PHP profiles**. Supported
families in this release are standard Remi PHP 8.2 and parallel Remi
`php83-php` 8.3. Other parallel branches, AlmaLinux and Debian
preparation remain available, but their build profiles are still rejected pending
native validation. Offline isolated builds are rejected until authenticated
buildroot cache locking is implemented. `--build-only`, `--force-rebuild` and
prerelease shortcuts are still unsupported; a fresh unique build prevents
accidental reuse of stale output.

## Phases and production regressions

1. Pin and preserve final inputs, originals, profile and the authenticated target
   archive. Compute the complete input identity.
2. Initialize a unique mock root. Record its identifier and toolchain packages;
   check the reviewed GCC/G++ versions.
3. Stage inputs, use native RPM spec expansion, and check exact package family
   and target version. Read native BuildRequires rather than guessing devel names.
4. Read capabilities such as `pkgconfig(oniguruma)`, `pkgconfig(gdlib)` and
   `pkgconfig(icu-uc)` from the expanded spec. Skip the host dependency repository
   sweep: mock's dependency solver is authoritative for its isolated root.
5. Generate a source package from the **final reviewed** spec/patch set, then ask
   mock to resolve dependencies from that SRPM. A missing compiler or conflicting
   `gd-devel`/`gd3php-devel`, `libicu-devel`/`libicu73-devel`, or reverse-dependent
   `harfbuzz-devel` stops this phase. It does not trigger a host `yum swap`.
   Review replacements and their complete simulated transaction in the disposable
   environment before revising the configuration. Automatic development-header
   swap planning/approval is not implemented; solver diagnostics and capability/
   reverse-dependency reports are provided instead.
6. Run `%prep` before compilation, with zero patch fuzz. Failed/reversed hunks or
   fuzz reports stop the build. Native RPM semantics determine patch order.
7. Compile/package with scoped compilers, regenerate the final SRPM again, copy
   generated artifacts out, and statically inspect the exact installed core
   replacement set. Require preserved ABI Provides, architecture, configuration
   and runtime/module/service paths. Record Requires/Provides, scriptlets, file
   metadata and hashes; retain the separate PECL package list.
8. Recheck input identity, clean the successful isolated root and write a
   **non-deployable** manifest. No package transaction or service restart occurs.

The timezone database and phpinfo changes described in the production notes are
**review cases, not templates for automatic patching**. Preserve
`AX_CHECK_COMPILE_FLAG`, vendor system-timezone semantics and the new PHPT regex
when producing reviewed replacements. For OpenSSL 1.0.2, inspect all five
`ASN1_STRING_get0_data()` occurrences and the version-gated accessor's ownership/
lifetime/security effects. The driver does not fabricate an unverified
compatibility patch or claim that successful compilation preserves a CVE fix.

## Diagnostics, reuse and recovery

Build output is retained per phase; every ten seconds the terminal reports
activity, recent output, elapsed time, process metrics (when `ps` exists) and disk
space. Native logs, expanded spec, BuildRequires, installed buildroot packages,
profile/input audit, compiler facts and artifact hashes are part of the manifest.
Failures distinguish dependencies, `%prep`, compilation/spec phases, RPM packaging
and explicit `%check` failure markers. Full logs remain the authoritative evidence.

Some provider specs deliberately tolerate `make test` failures. The driver
therefore records PHP test summaries separately in `php-tests.json` and in the
artifact report. Any nonzero failed count is recorded as `failed`, even if the
RPM command exits zero; absent summaries are `not-reported`, never a passing
test result. Both conditions emit a warning and leave deployment blocked. RPM
production can complete for investigation, but must not be described as a clean
test pass.

On failure, no success manifest is written. `failure.json`, `build-state`,
`mock-root-id` and original patches remain for inspection. The failed mock root
is deliberately preserved so the operator can inspect `.rej` files using that
root/configuration, then review corrections and start a fresh build. Cleanup of
that isolated root is an operator recovery operation, not permission to delete
host paths. Interrupted active build processes are terminated as their dedicated
process group; the root may still require mock-managed recovery.

Build identity includes the final input bytes, upstream digest, patch/spec/profile
digests and pinned configuration/trust inputs. Changing any reviewed input
invalidates output. The final SRPM is regenerated after the final patch set;
an earlier SRPM is never promoted merely because compilation succeeded.

To investigate reuse on another server:

```sh
./shift8-php check php-cli --manifest=/private/transferred/build/manifest.json
```

An exact complete fingerprint is necessary, **not sufficient**, for deployment.
No automatic artifact transfer, binary runtime/extension/FPM/application
validation, private RPM signing, configuration backup,
recovery-package capture or rollback execution is implemented yet. All generated
RPMs are treated as local unsigned artifacts, never vendor-authenticated.
Neither compilation nor this static inspection establishes production readiness.

For a native dependency/file-conflict transaction check on the matching host:

```sh
./shift8-php discover php83-php-cli > /private/current-discovery.json
python3 lib/build_metadata.py transaction \
  /absolute/prepared/build-directory /private/current-discovery.json
```

This verifies the manifest and the complete discovery fingerprint, then runs
only `rpm -Uvh --test` with the selected installed-core replacements. It does not
install packages, execute scriptlets, restart FPM, select extra built modules or
use force/nodeps/signature-bypass flags. Results are saved to
`transaction-check.json`, bound to the manifest digest; failure exits nonzero.
Supply freshly collected discovery, not a copied old snapshot. This check
does not establish application health or rollback readiness. Package scriptlets
may restart FPM during a real upgrade, even though this test never runs them.

For isolated runtime smoke checks, install the selected RPMs and exact retained
PECL versions into a separate disposable mock root, never onto the live host.
Copy `tests/runtime-smoke.php` and the baseline discovery JSON into that root.
Run the built CLI with its packaged configuration:

```sh
# Inside the isolated root only:
/opt/remi/php83/root/usr/bin/php -d apc.enable_cli=1 -d ffi.enable=1 \
  /builddir/runtime-smoke.php 8.3.36 /builddir/baseline.json
/opt/remi/php83/root/usr/sbin/php-fpm -t
```

The smoke requires every baseline module to load and exercises PHP primitives,
crypto sign/verify (including rejection of altered data), SQLite, GD, selected
PECL APIs and, when installed, the FFI directives and ICU path touched by the EL7
recipe. It does not connect to production databases/cache services. Also run
upstream OpenSSL SAN/peer-verification tests against the built CLI; simple
extension loading is not a substitute for testing certificate-name validation.
Packaged FPM configuration checks are not validation of a production site's
custom pools, credentials, sockets or application traffic.
