# Public Repository Sanitization

This repository is public. All sample, test, fixture, and seed data must use the
sandbox identity only: site `SANDBOX` / "Sandbox Site"; person `sandbox-user` /
"Sandy Sandbox".

Never commit real customer, site, worker, or operator names in authored sample
data. Never commit auth tokens, API keys, passwords, private keys, internal
hostnames, tailnet addresses, private CouchDB endpoints, or absolute user-home
paths such as `/Users/<name>/` or `/home/<name>/`. Use public placeholders such
as `/Users/example/`, `/runtime/`, `/tmp/`, or `/srv/` when examples need paths.

Production code must not hardcode real identifiers. Customer, site, worker,
host, credential, and runtime path values must come from runtime data or
configuration.

The repository gate is `scripts/sanitization-scan`. It runs from git hooks and
CI over the same shared logic. It uses public-safe pattern rules only; it does
not keep a denylist of real identifiers. Because arbitrary names cannot be
identified without such a denylist, commit messages and branch names are scanned
for path, token, hostname, and endpoint patterns rather than personal names.

For genuine false positives, prefer changing the example to the sandbox identity
or to a generic placeholder. If a real exception is still needed, add an inline
comment on the same line:

```text
# sanitization-ok: public placeholder in documentation
// sanitization-ok
```

The scanner also reads `scripts/sanitization-allow.txt`, which contains tracked
regular-expression exceptions for public-safe patterns only. Do not put secrets,
customer names, worker names, internal hostnames, or private paths in that file.
