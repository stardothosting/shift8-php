# Contributing

Work incrementally. Discovery and source preparation must remain non-mutating
with respect to the host's package database, repositories, PHP and services.
Do not add a deployment escape hatch while validation and recovery are missing.

Use Bash for orchestration/native adapters and Python's standard library for
structured data. Maintain Bash 4.1 and Python 2.7/3.6+ compatibility for runtime
environments. Never source distribution metadata or evaluate remote packaging
instructions. Use the requested package's ownership metadata, not PATH's PHP.

Before proposing changes:

```sh
make test
make lint
make bats
make legacy
```

Add offline fixtures for every packaging convention or failure mode changed.
Use disposable native containers for discovery integration; never test package
replacement on a production machine. Record the exact OS, packages, ABI, commands
and results before expanding the tested-support matrix. Mocked signature checks
are not evidence of a real vendor trust chain.

Builds must use an explicitly reviewed mock profile. Never move dependency
installation, development-header swaps, spec macro execution or compiler changes
onto the production host. Preserve original patches, require reviewed context/
behavior changes, and invalidate outputs if any final input changes. Do not claim
fixture-tested mock orchestration establishes a reliable production EL7 build.

Do not commit credentials, private keys, full phpinfo output, customer
configuration, application data, downloaded source bundles or generated builds.
Keep artifacts outside the checkout. Report potential vulnerabilities privately
as described in [SECURITY.md](SECURITY.md).

Public release remains blocked on license selection, a maintained disclosure
contact and production-safety milestones. Contributions must not imply that an
upstream version number or a successful dependency solver proves CVE coverage.
