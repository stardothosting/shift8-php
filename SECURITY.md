# Security policy

## Current status

This is an experimental source-preparation utility, not an approved production
deployment tool. No supported production release or security response SLA exists.
Compilation is opt-in through a reviewed mock profile for the initial EL7 family.
Deploy and rollback are intentionally disabled.

## Reporting

Do not publish credentials, exploit details against customer systems, raw PHP
information tables or production manifests in public issues.

Before public release, maintainers must enable GitHub private vulnerability
reporting and publish a monitored security contact. If this checkout is supplied
directly and that channel is not available, contact the person or organization
that supplied it privately. Do not invent a public disclosure email.

## Trust boundaries

- Protect local package databases, system CA certificates, RPM trusted keys,
  Debian source keyrings, reviewed file maps and the private artifact store.
- HTTPS plus an authoritative checksum authenticates upstream source bytes;
  locally computed hashes alone do not authenticate an unknown download.
- RPM signatures must be verified with already-trusted keys. Debian source
  members must match a valid signed `.dsc`. No automatic key acquisition occurs.
- A source-map checksum is an operator assertion, not a vendor signature.
- Local manifests are unsigned. Never accept an artifact store from an
  untrusted user merely because all its own hashes match.
- Cached metadata can be stale even before the 24-hour expiry; offline operation
  cannot establish awareness of the newest releases.
- Neither source authenticity nor metadata inspection proves runtime
  compatibility, current CVE coverage, or safe rollback.
- Mock configurations are privileged executable configuration, not inert JSON.
  They, their transitive includes, archive key identities and signature/TLS policy
  require independent operator review and checksum pinning. Do not use a config
  that disables isolation, mounts production paths or bypasses package signatures.
- Final specs/patches are executable build inputs. They are run only inside the
  reviewed mock root, never on the host. The tool does not automatically decide
  whether an OpenSSL compatibility change preserves vulnerability fixes.
