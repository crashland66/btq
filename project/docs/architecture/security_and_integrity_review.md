# Security and Integrity Review

## Trust Boundaries

Trusted components:

- checked-in deterministic Python handlers
- configured local runtime directory
- configured vault roots
- operator-controlled scripts

Less-trusted inputs:

- audio transcripts
- generated events
- generated queue jobs
- iCloud ingress files
- manually authored outbox jobs
- edited Markdown projection files

The main integrity boundary is `queue_processor.main` plus the registered handlers: they validate job shape, resolve known sites/employees, enforce idempotency, and own canonical CouchDB writes. Markdown output is projection/export, not the authoritative mutation target.

## Injection Risks

Queue content can include arbitrary markdown-like text. The system preserves it after job validation and may render it into Markdown projections, but it does not sanitize markdown links, embeds, tags, or frontmatter-looking text inside content. This is acceptable for trusted local operators, but it is not safe for untrusted external job authors.

Supply email parsing reads HTML from paths under repo parent or vault. It parses content and writes order records/markdown. Malicious or malformed email HTML could produce misleading records even if path containment succeeds.

## Malformed Job Risks

`queue_spec.validate_job()` checks job type and required fields. It does not reject extra payload fields for most job types, enforce full type constraints for every field, or validate all semantic ranges. Some deeper checks happen in handlers.

Invalid jobs move to `failed/`, which is good operationally but not a security boundary against a malicious local user.

## Replay Abuse

Any actor with filesystem write access can place jobs in outbox or runtime queue. Idempotency reduces accidental duplication but does not authenticate intent. A malicious user could alter payload text to bypass computed-job dedupe.

## Filesystem Assumptions

Path containment uses resolved paths and common path checks. This is a solid baseline. Residual risks include symlink changes between validation and write, local filesystem permission issues, and direct manual edits outside the processor.

## Sync Risks

iCloud ingress is treated carefully. Markdown projection/export can still be sync-managed for human convenience, so conflict copies, delayed propagation, and concurrent edits can make the projection stale or misleading until regenerated. CouchDB remains the canonical store.

## Operational Tampering

Integrity can fail if an operator:

- edits `btq_job_ids`
- deletes processed job files
- manually changes canonical CouchDB documents or projection content after marker insertion
- runs multiple processors concurrently
- edits config to point roots at unintended locations
- adds a site outside the active CouchDB site registry

Recommended integrity upgrades:

- append-only audit manifest
- signed or checksummed queue jobs
- processor lock file with stale-lock handling
- target-content hash in applied-job records
- structured processed-job index
