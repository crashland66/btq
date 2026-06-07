# Scalability Review

## Filesystem Scaling

The current design scales to small and moderate local workloads. It will degrade as directories accumulate thousands of files because queue, processed, failed, and event directories are scanned and sorted repeatedly.

Pressure points:

- `processed_job_id_exists()` scans every processed JSON file.
- unknown reclassification scans journal unknown files.
- nightly digest scans multiple directories.
- no compaction or archival policy is enforced.

## Queue Growth

File queues are inspectable but weak at scale. There are no leases, visibility timeouts, priorities, dead-letter metadata, or indexed job state. Failed jobs are preserved but not automatically classified or retried.

## Markdown Vault Growth

Append-oriented markdown will become harder to parse and reconcile as notes grow. Full-file replacement means large files become increasingly expensive and more conflict-prone under sync.

## Sync Limitations

iCloud can work as a human convenience layer, but it is not a durable queue or database. The code correctly avoids using iCloud for runtime processing. The remaining limitation is that vault files themselves are sync-managed in the default config.

## Concurrency Assumptions

The system assumes one active queue processor. Multiple producers can stage jobs, but multiple consumers can race. Scaling consumers horizontally is not safe without leases or a transactional store.

## Throughput Bottlenecks

Primary bottlenecks:

- Whisper transcription
- sequential queue processing
- repeated directory scans
- full-file markdown reads/writes
- static site resolution registry maintenance

## Migration Paths

SQLite:

- best near-term upgrade
- store jobs, attempts, processed IDs, target hashes, and leases
- keep markdown vault as human projection

CouchDB/PouchDB:

- good fit for offline replication and document sync
- useful if multi-device operational capture becomes central
- requires conflict resolution design

Append-only event store:

- fits operational memory and replay goals
- can preserve immutable source events and derive vault projections
- requires snapshot/projection tooling

Message queues:

- useful when multi-worker throughput matters
- premature unless runtime leaves single-machine local-first mode

Object storage:

- good for audio, transcripts, and immutable artifacts
- should be paired with metadata DB/index

Structured metadata indexes:

- lowest-risk incremental path
- index markdown records, job markers, unknown captures, visits, and supply orders
- enables audit and dashboards without abandoning the vault
