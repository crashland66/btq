# Vault Schema

This document describes the Obsidian vault structure that the current BT Pipeline code expects.

It is not an aspirational schema. It is a description of what the code reads and writes today.

The vault root is configured by `vault_dir` in [config.json](/Users/operator/btq/config.json). Runtime paths such as `pipeline_dir`, `outbox_dir`, and `working_dir` are configured separately and are not part of the vault.

AI bootstrap documents, queue authoring guidance, architecture notes, and agent instructions are also not part of the vault. They are source-controlled repository artifacts exported to the external iCloud `BTDocs` directory by `./scripts/btq-export-docs`.

Checked-in example artifacts live under:

- [project/docs/examples/vault/](/Users/operator/btq/project/docs/examples/vault)

Those files are documentation examples only. The pipeline does not read them at runtime.

## Top-Level Directories

Current vault-facing code assumes these top-level directories:

- `Accounts/`
- `Journal/`
- `People/`

Also present in queue-processor read-only query helpers:

- `Sites/`

Required vs optional:

- `Accounts/` is required for any site-based queue job to work.
- `Journal/` is required for journal appends, unknown capture storage, and unknown reclassification.
- `People/` is required if the system needs to process `add_person`, `remove_from_schedule`, or `flag_retention_risk` jobs.
- `Sites/` is not part of the active write path. It is only read by `queue_processor.main` query commands such as `list-critical-sites` and `list-employees-by-site`.

## Site Notes Under Accounts

Site-based writes and site resolution depend on `about.md` files under `Accounts/.../Locations/.../about.md`.

Expected path pattern:

```text
Accounts/<Account>/Locations/<Site Directory>/about.md
```

Examples from the current site registry:

- `Accounts/Wgtco/Locations/7030 - Western Gas Transmission/about.md`
- `Accounts/RHN/Locations/7020 - Lakeshore Community Health Center - RHN/about.md`
- `Accounts/Summitsteel/Locations/7050 - Summit Wire/about.md`
- `Accounts/Glenco/Locations/7070 - Glenwood Elementary School/about.md`
- `Accounts/Glenco/Locations/7071 - Glenwood High School/about.md`

These paths are not discovered dynamically from the vault. They are also hard-coded in the site registry at [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py).

Runtime vs discovery distinction:

- the active runtime routing path in this repository uses the current contents of `event_pipeline/sites.py`
- a structurally valid site note in the vault is necessary for queue-processor site validation
- but structural validity alone is not sufficient for event-to-queue routing if the site is not present in the active runtime registry
- I did not find a checked-in nightly registry-refresh implementation in this repository
- if your deployed environment runs an external nightly process that inspects the vault and refreshes the registry, that refresh step is outside the code currently documented here

Checked-in structural example:

- [project/docs/examples/vault/Accounts/ExampleAccount/Locations/9999 - Example Service Yard/about.md](/Users/operator/btq/project/docs/examples/vault/Accounts/ExampleAccount/Locations/9999%20-%20Example%20Service%20Yard/about.md)

Important:

- That example is structurally valid for frontmatter parsing.
- It is not currently routable by the checked-in runtime because `Example Service Yard` does not exist in the active registry in `event_pipeline/sites.py`.
- A real production site note must match both this file shape and the active runtime registry entry in [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py).

### Required Frontmatter for Site `about.md`

The queue processor only treats an `about.md` file as a valid location record when its frontmatter contains:

- `type: location`
- either `job: "<site_id>"` or `site_id: "<site_id>"`
- usually `location: <canonical site name>`

Minimal working example:

```md
---
job: "7030"
account: Wgtco
location: Western Gas Transmission
type: location
---
# Western Gas Transmission

## Operational Notes
```

What is enforced:

- Site jobs are identified by either a `job` or `site_id` field. Treat site IDs
  as strings because site/job numbers are identifiers, not numbers.

What is implied but not strongly enforced:

- `location` is used by `site_name_for_about_path()` when the processor decides whether a file is a site note and whether to add `visit_key` or `visit_gap` metadata.
- Missing or incorrect `location` frontmatter will not stop all writes, but it can break visit linking behavior.
- `account` is not required by the active parser, but it is part of the current path convention and is useful for human readability.

### Valid Site Note vs Currently Routable Site

These are different states in the current system:

- structurally valid site note:
  an `about.md` file in the expected path shape with valid enough frontmatter for queue-processor site validation
- present in the active runtime registry:
  a canonical site entry exists in `event_pipeline/sites.py` with a matching `site_id`, `note_path`, and aliases
- currently routable site:
  runtime extraction resolves the spoken site name to that canonical site, and `event_to_queue` can convert that canonical site into the configured `about.md` path

Practical consequence:

- a new site can exist in the vault and still not route yet
- until the active registry contains that site, runtime event routing will still return `unknown` or fail to map the canonical site to a note path
- the queue processor can only write to a site `about.md` after the job already references a valid known site

### `Visits/` Subdirectory

Site visit anchors are written under a sibling `Visits/` directory beside `about.md`.

Expected path pattern:

```text
Accounts/<Account>/Locations/<Site Directory>/Visits/YYYY-MM-DD.md
```

This directory does not need to exist in advance. `visit_create` creates it when needed.

Visit file entries currently look like:

```md
---
type: visit
timestamp: 2026-04-19T23:04:30.000000+00:00
site: Western Gas Transmission
date: 2026-04-19
visit_key: "Western Gas Transmission:2026-04-19"
source: ingestion
confidence: high
evidence: I was at Western Gas Transmission.
---
```

Idempotency:

- If the same `evidence` string already exists in that day’s visit file, `visit_create` skips it.

### `Issues/` Subdirectory

Operational site issues are written under a sibling `Issues/` directory beside
`about.md`.

Expected path pattern:

```text
Accounts/<Account>/Locations/<Site Directory>/Issues/<issue_id>__<slug>.md
```

This directory does not need to exist in advance. `log_site_issue` creates it
when needed.

Dashboards and site-viewer exports read these files as structured operational
objects. That indexing is display-only: it must not update status, mark client
notification, stage queue jobs, invoke the queue processor, or mutate canonical state.
`client_notified` records communication only and is separate from `status:
resolved`.

Issue files use `type: site_issue` frontmatter and keep client notification
separate from resolution:

```md
---
type: site_issue
issue_id: iss_...
site_id: "7050"
site: Summit Wire
account: Summitsteel
title: Restroom drain backup and inoperable stall
status: open
priority: high
category: maintenance
source: field_capture
reported_by: Tom Walsh
observed_at: 2026-05-06T18:27:03-04:00
created_at: 2026-05-08T20:00:00+00:00
client_notified: true
client_informed: true
client_informed_at: 2026-05-08T15:10:00-04:00
client_informed_by: Jordan
client_informed_method: email
client_notified_at: 2026-05-08T15:10:00-04:00
client_notified_by: Jordan
client_notified_method: email
resolved_at:
resolution_trigger: Maintenance confirms the drain is clear and the stall is operable.
resolution_summary: ""
related_capture_ids:
  - cap-photo-summit-drain
related_candidate_ids:
  - ac_...
related_media:
  - /media/cap-photo-summit-drain/drain.jpg
source_artifacts: []
btq_job_ids:
  - <computed job id>
---
```

Allowed issue status values are `open`, `monitoring`, and `resolved`. Client
notification is tracked through `client_notified`; it does not mean the issue is
resolved. The issue is resolved only when the status is `resolved` and the
record includes the appropriate resolution fields.

Issue bodies contain these sections:

- `## Summary`
- `## Observations`
- `## Evidence`
- `## Client Communication`
- `## Follow-up / Resolution Notes`
- `## History`

Idempotency:

- `issue_id` is deterministic from site, title, observed time, source, and first
  capture reference unless an explicit `issue_id` is supplied.
- Reprocessing the same queue job does not duplicate the issue file.
- A later queue job with the same explicit `issue_id` updates the issue and
  preserves prior `btq_job_ids` and history.

### `Supplies/` Subdirectory

Consumable supply needs are written under a sibling `Supplies/` directory beside
`about.md`.

Expected path pattern:

```text
Accounts/<Account>/Locations/<Site Directory>/Supplies/<supply_id>__<slug>.md
```

This directory does not need to exist in advance. `log_supply_need` creates it
when needed.

Supply files use `type: supply_need` frontmatter:

```md
---
type: supply_need
supply_id: sup_...
site_id: "7050"
site_name: Summit Wire
account: Summitsteel
item_name: BrightWash cleaner
quantity_needed: 2 bottles
urgency: high
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: open
created_at: 2026-05-08T20:00:00+00:00
notes: Supply closet is empty.
related_capture_ids:
  - cap-supply-summit
related_candidate_ids:
  - ac_supply_summit
related_media: []
source_artifacts: []
btq_job_ids:
  - <computed job id>
---
```

Allowed supply status values are `open`, `ordered`, `delivered`, `stocked`,
and `no_action_needed`. Allowed urgency values are `low`, `normal`, `high`, and
`critical`.

Optional order, delivery, and stocked fields are written when present:
`ordered_at`, `ordered_by`, `ordered_note`, `delivered_at`, `delivered_by`,
`delivered_note`, `stocked_at`, `stocked_by`, and `stocked_note`.

Supply bodies contain these sections:

- `## Notes`
- `## Related captures/candidates`
- `## Source artifacts/media` when source artifacts or related media exist
- `## History`

Idempotency:

- `supply_id` is deterministic from site, item name, requester, and observed
  time unless an explicit `supply_id` is supplied.
- Reprocessing the same queue job does not duplicate the supply file.
- A later queue job with the same explicit `supply_id` updates the supply need
  and preserves prior `created_at`, `supply_id`, `btq_job_ids`, and history.

### `Equipment/` Subdirectory

Durable equipment requests are written under a sibling `Equipment/` directory
beside `about.md`.

Expected path pattern:

```text
Accounts/<Account>/Locations/<Site Directory>/Equipment/<equipment_id>__<slug>.md
```

This directory does not need to exist in advance. `log_equipment_request`
creates it when needed.

Equipment request files use `type: equipment_request` frontmatter:

```md
---
type: equipment_request
equipment_id: eqr_...
site_id: "7050"
site_name: Summit Wire
account: Summitsteel
equipment_name: vacuum
reason: Current vacuum will not start.
priority: urgent
requested_by: Tom Walsh
observed_at: 2026-05-08T14:12:43+00:00
source: field_capture
status: open
created_at: 2026-05-08T20:00:00+00:00
notes: Needed for lobby carpet.
related_capture_ids:
  - cap-equipment-summit
related_candidate_ids:
  - ac_equipment_summit
related_media: []
source_artifacts: []
btq_job_ids:
  - <computed job id>
---
```

Allowed equipment status values are `open`, `approved`, `ordered`,
`provided`, `denied`, and `no_action_needed`. Allowed priority values are
`low`, `normal`, `high`, and `urgent`.

Optional approval, denial, order, and provided fields are written when present:
`approved_at`, `approved_by`, `approval_note`, `denied_at`, `denied_by`,
`denial_note`, `ordered_at`, `ordered_by`, `ordered_note`, `provided_at`,
`provided_by`, and `provided_note`.

Equipment request bodies contain these sections:

- `## Notes`
- `## Reason`
- `## Related captures/candidates`
- `## Source artifacts/media` when source artifacts or related media exist
- `## History`

Idempotency:

- `equipment_id` is deterministic from site, equipment name, requester, and
  observed time unless an explicit `equipment_id` is supplied.
- Reprocessing the same queue job does not duplicate the equipment request
  file.
- A later queue job with the same explicit `equipment_id` updates the
  equipment request and preserves prior `created_at`, `equipment_id`,
  `btq_job_ids`, and history.

### `Opportunities/` Sibling Directory

The queue processor builds its site cache by taking each valid `about.md` and recording:

```text
<site about path parent>/Opportunities
```

Current site note resolution then derives the site `about.md` path back from that cached `Opportunities/` directory.

Practical consequence:

- The code assumes each valid site `about.md` lives in a location directory that can also contain an `Opportunities/` sibling directory.
- The `Opportunities/` directory does not need to exist for normal site-note writes, but the directory shape is assumed by the cache logic.

## Journal Files

Journal writes use UTC dates and these filename patterns:

- `Journal/YYYY-MM-DD.md`
- `Journal/YYYY-MM-DD-unknown.md`

### Daily Journal Files

Human-readable daily note target:

```text
Journal/YYYY-MM-DD.md
```

Current queue jobs that may append here:

- `append_to_note` with `destination: journal`
- current event mappings for:
  - `employee_callout`
  - `incident`
  - `employee_onboarding`

These files do not need to exist in advance. `append_to_note` can create them.

### Unknown Journal Files

Unknown or missed captures are stored in:

```text
Journal/YYYY-MM-DD-unknown.md
```

This file may contain one or more structured `unknown_capture` entries. The queue processor scans the file for blocks that begin with:

```md
---
type: unknown_capture
```

A current unresolved entry looks like:

```md
---
type: unknown_capture
timestamp: 2026-04-19T10:00:00+00:00
audio_file: missed.m4a
status: unresolved
retry_count: 0
last_attempted: null
---

## Original Transcript
Original transcript text.

## Normalized Transcript
This remains an unclassified walkthrough note.

## Notes
#unknown #needs-review
```

Checked-in example:

- [project/docs/examples/vault/Journal/2026-04-19-unknown.md](/Users/operator/btq/project/docs/examples/vault/Journal/2026-04-19-unknown.md)

When resolved, the frontmatter is updated in place with fields such as:

- `status: resolved`
- `resolved_at: <timestamp>`
- `resolved_site: <site or multiple or unknown>`

The body also gets an appended resolution marker:

```md
---
RESOLUTION:
Reclassified and routed to structured events.
---
```

### Unknown Capture Retry / Reclassification Assumptions

Unknown capture behavior depends on these conventions:

- The file must live under `Journal/`.
- The filename must match `*-unknown.md`.
- Each entry must begin with `type: unknown_capture`.
- `status` defaults to `unresolved` if missing during parsing.
- `retry_count` defaults to `0` if missing.
- `last_attempted` defaults to `None` if missing.

Reclassification triggers when:

- the file modification time is newer than the capture timestamp
- or the body contains a site signal resolvable by the current site registry
- or the body contains `#site:`
- or retry backoff allows another attempt

Unknown does not mean failure. It means the data has not yet been classified into supported event types.

Structural vs illustrative notes for the example:

- `type`, `timestamp`, `audio_file`, `status`, `retry_count`, and `last_attempted` are part of the actively parsed frontmatter shape.
- `## Original Transcript`, `## Normalized Transcript`, and `## Notes` are expected by the current reclassification helper logic.
- `#unknown #needs-review` is conventional default content from the transcription pipeline, not a strict parser requirement.
- `#site:` is optional, but it is one of the explicit signals that can trigger reclassification.

## Runtime Outbox

The configured runtime outbox is used by the shell staging scripts, not by the transcription pipeline’s generated local queue jobs. It is outside the vault.

Expected pattern:

```text
BTpipeline/outbox/*.json
```

Used by:

- [btq-stage-outbox](/Users/operator/btq/scripts/btq-stage-outbox)
- [btq-run](/Users/operator/btq/scripts/btq-run)
- [btq-dry](/Users/operator/btq/scripts/btq-dry)

Queue files in `BTpipeline/outbox/` must match the current queue contract in [queue_spec.py](/Users/operator/btq/project/queue_spec.py).

## People Notes

People files are needed for employee-targeted jobs and for `add_person` entity creation.

Expected path pattern:

```text
People/<Last>, <First>.md
```

The processor does not accept exact path references for people jobs. Existing employee-targeted jobs search the `People/` tree and resolve a person file by frontmatter. `add_person` generates `People/<Last>, <First>.md` internally for normal personal names.

Filenames are presentation, not identity. Names may change later. Writer-created records use `person_id` as the canonical stable identity anchor.

Expected frontmatter fields:

- `type: person` for writer-created records, or legacy `type: employee`
- `person_id` for writer-created records
- `name`
- `first` and `last` compatibility fields for normal personal names
- `role` for writer-created records

Helpful but not strictly required for all code paths:

- `preferred` or `preferred_name`
- `status`
- `status_date`
- `job`
- `additional_jobs`

Example:

```md
---
name: Peter Nash
first: Peter
last: Nash
type: employee
status: active
status_date: 2026-04-01
job: "7030"
additional_jobs:
  - "7050"
---
# Peter Nash
```

Checked-in example:

- [project/docs/examples/vault/People/Doe, Avery.md](/Users/operator/btq/project/docs/examples/vault/People/Doe,%20Avery.md)

Structural vs illustrative notes for the example:

- employee lookup currently depends on combinations of `name`, `first`, `last`, `preferred`, `preferred_name`, and the filename stem.
- `type: employee` is used in current examples and conventions, but the active lookup function does not require it to match a file by name.
- `job` is the primary assigned work site. Keep this field singular during the transition because existing Dataview queries, dashboards, staffing reports, vault lookups, and operational automations may already depend on it.
- `additional_jobs` is an optional list of secondary assigned work sites.
- effective authorized site membership is `job` plus `additional_jobs`, with duplicates ignored.
- legacy `sites: [...]` remains readable as a fallback for older records that do not have `job`.
- `status` and `status_date` are not required for employee-targeted writes, but they are used by read-only query helpers and reflect current conventions.

Current employee-targeted writes:

- `add_person` creates `People/<Last>, <First>.md` for normal personal names and fails safely on duplicate employee ID or normalized name collision
- `add_person` renders `first` and `last` compatibility fields when it can derive them from `name`; explicitly supplied `first` and `last` values are preserved
- `add_person` may write `job` and `additional_jobs`; when `assignments` are present, the first assignment job is rendered as `job` and later assignment jobs are rendered as `additional_jobs`
- keyed `add_person` replay uses `<runtime_root>/idempotency_keys.jsonl`; same key plus same payload is a no-op, same key plus different payload fails
- `remove_from_schedule` appends under `## Schedule Changes`
- `flag_retention_risk` appends under `## Retention Risks`

These sections do not need to exist in advance. The processor creates them if missing.

## Sites Directory

`Sites/` is not part of the active deterministic write pipeline.

It is only read by queue-processor helper commands that list site or employee information. Those helpers expect frontmatter records with:

- `site_id`
- `name`
- `status`
- `status_date`

If you are only running the audio -> queue -> vault pipeline, `Sites/` is not required.

## What the System Writes vs What Humans Maintain

System-written or system-appended files:

- `Journal/YYYY-MM-DD.md`
- `Journal/YYYY-MM-DD-unknown.md`
- `Accounts/.../about.md`
- `Accounts/.../Visits/YYYY-MM-DD.md`
- `People/*.md` for supported employee jobs

Usually human-maintained seed files:

- site `about.md` files under `Accounts/.../Locations/.../about.md`
- employee records under `People/`
- any optional `Sites/` query records

Important distinction:

- The system appends to or updates these files.
- The system does not scaffold a full vault from scratch.
- Incorrect or missing seed files will cause some queue jobs to fail.

## Naming and Registry Assumptions

Site resolution is finite and registry-driven. The active site list lives in [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py).

Each site entry currently defines:

- canonical name
- string `site_id`
- canonical `note_path`
- aliases

Practical consequences:

- A site mentioned in transcripts must either match the registry directly or be alias-resolvable.
- Queue jobs for site-based actions assume the canonical site can be converted back into a valid vault `about.md` path.
- Renaming site directories in the vault without updating the active registry will break routing.
- Adding a new site note to the vault does not make it immediately routable unless the active registry is also refreshed.

Nightly refresh note:

- The repository documentation now distinguishes runtime routing from any external registry-refresh process.
- I did not find checked-in code here that rebuilds `event_pipeline/sites.py`, writes a generated registry artifact, or otherwise refreshes the runtime registry from the vault.
- If your production environment performs a nightly registry refresh, that behavior is external to the code path documented in this repository state.

## Fragile Areas

These are current coupling points that are implied by code more than enforced by explicit schema validation:

- site `about.md` frontmatter must be valid enough for `site_name_for_about_path()`
- site paths must stay aligned with the hard-coded registry in `event_pipeline/sites.py`
- people resolution depends on human names and frontmatter, not stable person IDs
- unknown capture scanning depends on exact markdown/frontmatter markers
- journal and visit dates use UTC in the current implementation

If the vault structure drifts from these assumptions, the queue processor may fail jobs, misroute writes, or silently stop linking visits.
