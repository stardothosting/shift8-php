# Isolated CentOS 7 / Remi builds

## Source-first route versus vendor-profile builds

`shift8-php plan` and `prepare --source-first` are a separate route. They use
installed RPMs as an inventory of paths, configuration and extension ownership,
not as a requirement to recover/rebuild an old source package. They do not use
the vendor-spec machinery documented below.

The source-first contract includes:

- The running PHP version for target/downgrade checks, even after a manual
  file swap left RPM version metadata stale. Drift is an explicit blocker for a
  managed transaction, not silently repaired or accepted as rollback evidence.
  The legacy vendor-profile route retains its strict mismatch rejection.
- System and parallel Remi layout detection with exact CLI, FPM, extension and
  compiled configuration paths. FPM is inspected separately with `-i`; it is not
  started. Distinct CLI/FPM versions, ABIs, thread safety or compiled INI paths
  require a separate adapter/review.
- Installed core shared modules, loaded features and separately packaged
  extensions. Feature proposals are data, **not executable configure commands**.
  The old `Configure Command` is never evaluated or replayed. Unknown/proprietary
  modules, control-panel paths and Apache module payloads block automatic use.
- Observed allow-listed library versions and explicit isolated-resolution
  requirements. Legacy OpenSSL requires a private-source decision. Runtime
  presence/version alone does not approve library maintenance, headers or ABI.
  System OpenSSL/libxml replacement is prohibited.
- A pre-resolution source-contract identity separate from the full host
  deployment fingerprint. Changing service state or configuration bytes need
  not require recompilation; they still matter for deployment. The contract
  hash is **not** a binary cache key: final compiler, dependencies, patch bytes
  and other resolved build inputs must be part of that key.

`plan` reads the two authoritative PHP release feeds but does not query old Yum
repositories or download source packages. `prepare --source-first` saves the
checksum-verified upstream tarball, release receipts, contract, discovery and
manifest. Blockers are retained in that manifest; preparing sources is not a
build approval. Profile/map/keyring options from the historical packaging path
are rejected rather than ignored.

There is no source-first private-dependency builder, generated replacement RPM
backend or live transaction/rollback executor yet. In particular, the contract
does not solve mixed OpenSSL consumers by setting a global `LD_LIBRARY_PATH`,
replace glibc, or claim arbitrary PECL binaries can be retained. Those surfaces
must use a pinned, tested dependency graph and an actual recovery payload before
this route can install anything.

### Native source-first preparation check

The source-first route has been exercised on the CentOS 7.9 Remi php83 host
after a manual file update left running PHP at 8.3.36 and RPM metadata at 8.3.8.
It selected the running branch's official 8.3.36 release, retrieved and verified
the upstream tarball, and wrote a checked source-preparation manifest without
querying historical package repositories or downloading packaging inputs.

The contract identified the parallel paths, 41 core features, seven separate
extension packages, and independently matching FPM facts. It recorded the
installed Apache module and package/runtime drift as transaction blockers, and
identified the legacy OpenSSL runtime as requiring a private-source decision.
No installed PHP files, host libraries or services were changed. This is native
evidence for inspection/source preparation only, not a source-first build or
deployment test.

### Lessons from the multi-server source rollout

The supplied rollout reference describes PHP 8.3.36 installed beside older
PHP installations under a new prefix, with OpenSSL 1.1.1 and private libzip
1.10.1. It reports FPM startup on three CentOS 7.9 hosts, but explicitly leaves
web cutover, retirement and several PECL modules incomplete. That is valuable
operational evidence, not a complete parity/security certification. Its paths,
ports, users, historical hashes and library versions are examples, not defaults.

Keep two workflows distinct:

- **Patch replacement:** a maintained patch release in the same PHP branch,
  preserving the existing paths/configuration through a managed replacement.
  This remains the project's original objective and current planner's scope.
- **Side-by-side migration:** a different branch or deliberately separate
  prefix/service/listener. Build and validate independently; route only approved
  applications later. PHP 7.4/8.2 to 8.3 is an application migration, not a
  transparent patch replacement. This mode is not implemented.

Both can share source authentication, dependency resolution, build profiles and
validation. They cannot share an implicit cutover or retirement policy. An
installed Apache module blocks in-place replacement until handled; it should
not necessarily block a future independent FPM deployment that leaves it alone.
Likewise stale RPM metadata requires reconciliation for managed replacement,
but should not cause a future side-by-side builder to overwrite the old runtime.

| Rollout lesson | Current evidence / next implementation requirement |
| --- | --- |
| Lost configure arguments, stray bare `--` | Proposals are arrays and never replay the historical shell command. Before building, validate the full resolved profile, reject malformed arguments and verify the resulting features. No complete source-first profile executor exists yet. |
| Parallel OpenSSL headers versus runtime | Select explicit pkg-config module, headers, link flags and runtime path as one provider. Verify the final loaded version and ELF closure, not only a direct FPM `ldd` line. Built-in OpenSSL must not require `openssl.so`. Provider resolution is not implemented. |
| Old system libzip / unavailable devel RPM | Keep libzip requirements even when ZIP is a separately packaged extension. PHP's actual `Libzip version` and header/library keys are allow-listed. A missing trusted package must lead to an approved private build, not guessed package names. |
| ZIP present but AES disabled; GD missing formats | Contract capability records preserve observed AES/JPEG/FreeType/etc. support. Unknown is not false. Final API-level feature tests and explicit application requirements remain mandatory; the private libzip recipe must not silently disable AES. |
| Correct build rejected by `grep -q`/pipefail | Verification should consume captured command output, distinguish command failure from mismatch, and be rerunnable independently of compilation. Add the actual SIGPIPE regression with the verifier implementation. |
| Service active before listener is ready | Bounded readiness must combine compatible systemd property output, master PID/executable, and TCP/Unix listener ownership. Current service discovery does not establish readiness. No foreign listener may be stopped to make a check pass. |
| Root-only runtime/library directories | Keep artifact/state/backup stores private, but assign intentional runtime modes during staged installation. Never reuse artifact hardening on the installation prefix. Test execution/module loading as every relevant pool user; preserve SELinux enforcement. |
| Missing completion marker after successful startup | Use atomic, fingerprint-bound stage state reconciled with actual files/processes. Verification must not require a success marker or cause a rebuild. Build/deploy/finalize state recovery is not implemented. |
| Core parity without Redis/Memcached/etc. | Separately packaged modules retain explicit source/provider decisions. Dependency ordering, target phpize/php-config, ABI and application parity are mandatory. Core extension success is not full parity. |
| Multiple package families / aliases / routes | The selected package family is isolated today. Complete all-runtime, process, active web include/upstream, cron and CLI resolution inventory remains to be implemented. Never infer application routing from the default CLI. |
| FPM started but sites not moved | Keep built, staged, baseline-deployed, application-verified, cutover and retired as separate states. No automatic Nginx edits or old-PHP removal on build/deploy success. |
| Historical compatibility libraries | OpenSSL 1.1.1 compatibility is not current security support. Resolve lifecycle policy independently from buildability; an explicit compatibility exception must not be described as supported or secure. |

Prioritize the remaining work in dependency order:

1. Complete installation/provider facts: all runtimes, actual loaded/shared
   modules and versions, phpize/php-config, pool users/listeners and active routes.
   Capture only allow-listed build variables; redact application credentials.
2. Resolve pinned library providers and complete argument/environment arrays.
   Preserve capability requirements and validate a final dependency graph.
   CMake/libzip flags, JIT and compiler choices belong to resolved profiles.
3. Implement the source-first isolated builder and reusable validate-build
   checks. Install through a staging root, never live `make install`; validate
   archive members/link targets and final files before promotion.
4. Add fingerprinted state/recovery, actual-file backups, non-root permissions
   and mode-specific deployment. A private-prefix payload can be packaged without
   requiring the original vendor spec. In-place payloads need package-aware
   file ownership and complete rollback.
5. Add identity-bound service readiness and FastCGI/application probes before
   explicit routing changes. Rehearse routing/runtime rollback before retirement.

The reference does not include the accompanying full rollout script or its
complete effective configure array. Do not invent the omitted arguments or
claim that reproducing the listed subset reproduces the three deployed builds.

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

That build's full provider PHP suite reported three failures:
`openssl_error_string_basic.phpt`, `bug74589_utf8.phpt`, and
`php_cli_server_pdeathsig.phpt`. They are not waived by the successful targeted
checks. Review the retained diffs before deployment; the first concerns error
codes returned by the old OpenSSL, while the others concern Unicode paths and
CLI server worker shutdown. These observations are not a production safety
claim or permission to discard the tests.

Those three tests were subsequently reproduced against the built runtime and
passed with the measured environment wrapper and reviewed test-portability
patch described below, with none of the three skipped. This is separate
evidence; the old build reports and RPMs are not relabelled as clean builds.

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

### Scenario-specific test handling

Use measured capabilities, not hostname checks, assumed locale names or a
test-failure allowlist:

1. The pinned [environment wrapper](../lib/build_environment.py) runs inside
   mock, selecting an actually available UTF-8 character locale. It sets
   `LC_CTYPE` while leaving numeric/time locale settings alone. Missing locale
   data is an explicit error, not a silently selected C locale.
2. The wrapper reaps orphan processes as PID 1, or requests Linux child-subreaper
   behavior when not PID 1. This allows the unchanged CLI server death-signal
   test to distinguish dead workers from live ones rather than seeing unreaped
   zombies. Unsupported reaping fails explicitly; leftover running children
   are an error. No host process names are killed.
3. The reviewed [test portability patch](../patches/php-test-portability.patch)
   initializes the Unicode path test's character locale explicitly. PHP begins
   in the C locale even with a UTF-8 environment; setting environment variables
   alone is insufficient.
4. The same patch compares the **library and reason** fields of packed OpenSSL
   <3 errors, not their release-specific function-number field. Wrong libraries
   or reasons still fail; the test is not changed to accept arbitrary errors.
   It changes test expectations, not cryptographic runtime or TLS verification.
5. The recipe preserves and pins both patches and stops on drift or conflicting
   patch numbers. The wrapper's digest is part of staged input validation.
   `php-tests.json` records the measured locale/reaper evidence.

The compile/package command now passes through this wrapper. Python must exist
inside the reviewed root; no interpreter, locale data or build dependencies are
installed on the host. The driver uses available root Python 3/2.7 paths and
reports a missing interpreter explicitly.

**Failed or missing PHP test summaries now stop preparation**, even if the
provider spec returns zero. The build keeps its diagnostic files and isolated
root, writes `failed:php-tests`, and does not write a success manifest. There is
also no success for an empty/all-skipped suite or missing environment evidence.
There is no waiver for these test names. Unknown scenarios stay blocked until a bounded
fix is reviewed and exercised. Unrelated upstream capability-based skips remain
visible; a skipped test is not evidence that its feature passed.

For a quick diagnosis, reproduce failures and nearby tests using the already
built CLI in a disposable root before another full compilation. Use the
provider-prepared source/test tree: running a pristine upstream test against a
vendor-modified runtime can create a different failure, such as the custom-EC
parameter test that this Remi spec explicitly removes during `%prep`.
Rebuild when runtime source, configuration or final packaging changes; never
retroactively change an existing manifest's evidence.

With the final generated recipe applied by native `%prep`, the adjacent
OpenSSL, directory and CLI suites have run against the existing PHP 8.3.36
RPMs: 294 passed, zero failed, 30 upstream capability/environment skips
(324 total), with all three previously failing tests executed and passing.
The selected locale was `en_US.utf8`, and the wrapper ran as the namespace's
PID 1. This verifies the scenario fixes without another full compilation.
Existing RPMs/manifests retain their original full-build test evidence; a new
package build must use the updated recipe and pass the strict test gate.

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
test result. Both conditions stop preparation after saving diagnostics, leaving
deployment blocked even if the provider's RPM command produced binaries.

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
