# ADR-007 CouchDB As Source Of Truth

## Status

Accepted as architectural direction (2026-05-20). Implementation is staged and
incremental — see Migration Path. No code in the queue processor's write path
changes until the schema work in this ADR is done.

## Context

The operational vault — markdown files in an iCloud-synced Obsidian directory —
is currently the source of truth (see "Markdown Vault as Primary Store" in
`architecture_decisions.md`, which already named CouchDB/PouchDB as a deferred
alternative and predicted this migration).

Two things have changed the calculus:

The vault is never hand-edited. Jordan only reads it in Obsidian — "Obsidian was
the design tool, not the destination." Editable markdown was the one capability
a file vault offered over a database; if it goes unused it is pure cost. And the
cost is real and recurring: iCloud sync races, atomic-write machinery
(`io_atomic`), vault-name-prefix path bugs, and BTDocs drift detection are all
tax paid for keeping a file tree as authoritative state. A meaningful share of
recent maintenance is fighting that tax.

The system is becoming an AI-native operations loop:

```text
transcription + photo-vision  ->  structured store  ->  LLM reasoning
        (capture)                  (connective tissue)   (scoped context)
```

That loop wants a store that is queryable, HTTP/JSON-native, and has a change
feed. CouchDB is exactly that, and it is already load-bearing: field capture and
voice memo run CouchDB-native today, and the ops dashboard already reads it live
(ADR follow-on; voice memo intake state). The markdown vault is the odd component
out — the only one requiring sync.

## Decision

CouchDB becomes the source of truth for operational entities. The Obsidian vault
becomes a **generated, read-only projection**, regenerated from CouchDB's
`_changes` feed.

The deterministic writer boundary ("Deterministic Writer Boundary" in
`architecture_decisions.md`) is retained without change in spirit: AI and
probabilistic stages do not mutate truth directly; writes flow through
schema-checked, idempotent, replayable jobs. That boundary now guards CouchDB
writes instead of vault writes.

## Options Considered

### Option A — Status quo: markdown vault as truth

Keep the file vault authoritative. Rejected: the sync/atomic-write/path-bug tax
continues and grows, and it does not serve the AI reasoning loop.

### Option B — CouchDB as truth, vault as a generated read-only projection (chosen)

CouchDB holds truth; a projector regenerates the vault from the `_changes` feed.
Obsidian remains the read UI — its search, backlinks, and graph come for free.
Once the vault is *derived*, a sync race or corrupted file stops being data loss:
re-project. Smallest leap that stops the bleeding.

### Option C — CouchDB as truth, bespoke cached HTML read view

Same storage decision, but replace Obsidian with a purpose-built cached HTML
view. Deferred, not rejected: more build, and it must re-earn search and
navigation. Revisit only when Obsidian's read experience is genuinely outgrown.

### Option D — A relational database (Postgres/SQLite)

Stronger ad-hoc and relational querying. Rejected: it loses the HTTP/JSON-native
API, the `_changes` feed, and replication that both the AI loop and the existing
CouchDB-native apps depend on — and it introduces a *new* infrastructure
dependency rather than consolidating onto one already proven in the stack.

## Schema And Retrieval

Entities are first-class. Every document carries its entity references
(`site_id`, `person_id`, ...) so retrieval is **entity-scoped**: an index pulls
*all* documents for one site or one person — complete and exact, not a
similarity-search sample and not a whole-database dump. That completeness is what
makes LLM answers reliable; schema design and retrieval strategy are the same
decision.

Map-reduce views become reusable "context lenses" — named, precomputed,
incremental (they update off the `_changes` feed) definitions of the right slice
for a class of question.

## Reporting

Ad-hoc reporting is LLM-driven, not SQL/relational — which is why CouchDB's lack
of joins is acceptable. The discipline:

```text
recurring, exact, fast   ->  a CouchDB view or small deterministic query
ad-hoc synthesis/judgment ->  LLM over entity-scoped documents
ad-hoc numeric/aggregate  ->  LLM emits a query; the query computes
```

The LLM is the reasoning engine, never the arithmetic engine.

## Context Access

CouchDB is natively an HTTP/JSON API — every document, query, view, and the
`_changes` feed is a URL returning JSON. Context injection is therefore a URL
fetch with almost no glue code. The fetch tool must be read-only, scoped to an
allowlist of databases/views, and hold credentials itself; the LLM-visible URL is
a credential-free path. (CouchDB basic-auth credentials currently sit in plaintext
in launchd plists — they must not leak into prompts, logs, or tool transcripts.)

## Migration Path

Incremental, never big-bang. The boundary moves under real pressure.

1. Done — the ops dashboard reads CouchDB live (voice memo intake state). Its
   cached + graceful-degradation pattern is the template for further reads.
2. Schema design — define which entities become CouchDB documents, their doc
   schema, and entity-ref tagging.
3. First CouchDB-native slice — the next new entity type, or the next painful
   vault sync bug, lands CouchDB-first.
4. The projector — a service that regenerates the vault from the `_changes` feed.
5. Move the queue processor write path to CouchDB, retaining the deterministic
   writer boundary.
6. Vault becomes strictly derived; retire the atomic-write and sync-race
   machinery that is no longer load-bearing.

## Risks

- CouchDB becomes a critical dependency; backup and restore must be robust
  (`provision-vps-couchdb` exists; backup discipline does not yet).
- Ad-hoc relational querying is weaker than SQL; cross-entity reporting needs
  deliberate view design or stitched entity pulls.
- The projected read experience must match what Obsidian gives (search,
  backlinks, graph) or it is a downgrade; Option B preserves this for free only
  as long as the vault remains the projection target.
- The migration touches the queue processor write path — a large surface that
  must stay incremental and ADR-gated.
- Credentials in plaintext plists are a pre-existing exposure that the
  context-fetch tool must not widen.

## Consequences

CouchDB-native querying, the `_changes` feed, and replication become available to
the whole system; the AI reasoning loop gets a real substrate; iCloud sync races
stop being a class of bug.

In exchange, CouchDB availability and backup become critical, and relational
reporting requires deliberate design rather than ad-hoc SQL.

This decision supersedes "Markdown Vault as Primary Store" over the course of the
migration. The vault does not disappear — it is demoted from source of truth to
derived projection.
