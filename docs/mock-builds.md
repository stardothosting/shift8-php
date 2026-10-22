# Isolated CentOS 7 / Remi builds

This is an **experimental build driver**, not certification that PHP 8.2.35 or
any future release is production-safe on EL7. Its orchestration is regression
tested with a mock-command fixture. A real historical Remi spec/patch closure and
operator-configured mock root must still be exercised before claiming a reliable
EL7 production build.

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
- The build adapter accepts explicitly reviewed final packaging. It does not
  rewrite spec text based on guessed `%patch47` syntax. Native `rpmspec` inside
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

If original packaging is no longer retrievable, also supply a reviewed
`--source-map`. That is an explicit alternative to SRPM retrieval, not a claim of
vendor authentication. No remote build script is executed on the host.

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
4. Query capabilities such as `pkgconfig(oniguruma)`, `pkgconfig(gdlib)` and
   `pkgconfig(icu-uc)` across configured enabled and disabled repositories with
   operation-local enabling, private caches and bounded network settings.
   Record provider and installed reverse-dependency output. Repository query
   failure is isolated; mock's dependency solver is authoritative for its root.
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
No automatic artifact transfer, native transaction simulation, binary runtime/
extension/FPM/application validation, private RPM signing, configuration backup,
recovery-package capture or rollback execution is implemented yet. All generated
RPMs are treated as local unsigned artifacts, never vendor-authenticated.
Neither compilation nor this static inspection establishes production readiness.
