# Queue Authoring Guide

This guide is for humans and AI that create BTQ runtime jobs.

Use it together with:

- [queue_spec.py](project/queue_spec.py)
- [pipeline.md](project/docs/pipeline.md)
- [vault_schema.md](project/docs/vault_schema.md)

If this guide and [queue_spec.py](project/queue_spec.py) ever disagree, follow `queue_spec.py`.

## Authoring Transport — CouchDB

Jobs are authored into CouchDB `btq_queue`, not dropped as files. The iCloud
`BTpipeline/outbox/` JSON-drop is **retired** (removed 2026-05-22; CouchDB
cutover 5/23–25) — do not author there.

To author a job, pipe the JSON doc to `scripts/btq-enqueue`:

```bash
cat job.json | python3 scripts/btq-enqueue --dry-run  # validate only, no write
cat job.json | python3 scripts/btq-enqueue            # PUT into btq_queue
```

`btq-enqueue` validates the doc against `queue_spec.py` before writing,
derives a `job_id` (`<UTC-timestamp>__<slug>`) when one is not supplied, and
uses the `job_id` as the CouchDB `_id`. The `couchdb_queue_watcher` on the
runtime host then materializes each pending doc into canonical `btq_vault`
state. Always `--dry-run` first.

Both authoring surfaces produce the same JSON job contract; only the
transport differs. The audio pipeline (Whisper transcription) parses voice
memos and PUTs derived jobs into the same `btq_queue`. There is no separate
file-drop contract to satisfy.

## Cowork Queue Visibility

Cowork cannot read CouchDB directly from its Linux sandbox and cannot reach the
tailnet-hosted read MCP server. Queue job writes from Cowork still go through
the `cowork_drop` file bridge, where the host-side watcher validates the job
and calls `btq-enqueue`. Queue reads use the symmetric `cowork_read` file
bridge. Do not put CouchDB credentials, network readers, enqueue tooling, or
admin tooling inside the Cowork sandbox.

The read bridge lives under `<pipeline_dir>/cowork_read/`:

- `requests/` - Cowork writes `<request_id>.json`
- `responses/` - the host watcher writes `<request_id>.json`
- `processed/` - successful requests are moved here after a response is written
- `failed/` - malformed requests and unknown tools are moved here after an error response

Request shape:

```json
{
  "request_id": "stable-id",
  "tool": "queue_state",
  "args": {"states": ["failed", "pending"], "recent_limit": 10},
  "created_at": "2026-06-05T00:00:00+00:00"
}
```

Allowed tools are `queue_state`, `list_queue_jobs`, and `get_job`. `queue_state`
accepts optional `states` and `recent_limit`; `list_queue_jobs` accepts optional
`date`, `since`, `state`, `all_dates`, and `limit`; `get_job` requires
`job_id`.

Response shape:

```json
{
  "request_id": "stable-id",
  "ok": true,
  "tool": "queue_state",
  "result": {},
  "completed_at": "2026-06-05T00:00:01+00:00"
}
```

Errors use `{"ok": false, "error": {"code": "...", "message": "..."}}`.
Transient CouchDB failures such as `couchdb_unavailable` leave the request in
`requests/` for retry and write no response.

From Cowork, use the sandbox-safe helper:

```bash
scripts/btq-cowork-read --tool queue_state
scripts/btq-cowork-read --tool list_queue_jobs --arg date=2026-06-05 --arg state=pending
scripts/btq-cowork-read --tool get_job --arg job_id=JOB_ID
```

The helper uses only the Python standard library and file I/O. It reads
`pipeline_dir` from the BTQ config, writes a request, polls for the matching
response, and returns a structured `reader_timeout` error if the host watcher is
down.

Run the host watcher on the Pro with
`python -m queue_processor.cowork_read_watcher`. To install it as a launchd
agent on the Pro, run `scripts/install-cowork-read-launch-agent` on that host.
The host watcher performs only queue reads through `couchdb_queue_reader`; it
has no enqueue, write, retry, repair, delete, acknowledgement, or vault lookup
surface.

BTDocs-facing mirror:

- `Queue/queue_authoring_guide.md` in the configured BTDocs export directory is the author-facing projection for AI and human setup contexts.
- The repository copy is canonical. Run `./scripts/btq-export-docs` to update the iCloud BTDocs projection.
- Do not place queue schema, AI instructions, or bootstrap documents inside the canonical operational store.
- Use the exported file's `BTQ_*` metadata header and `BTDocs/export_manifest.json` to detect stale or mismatched AI guidance.

## Core Authoring Rules

- Only job types defined in `project/queue_spec.py` are executable.
- Provide an explicit `job_id` string when you want a stable idempotency key; otherwise `btq-enqueue` derives one and uses it as the CouchDB `_id`.
- Do not invent job types.
- Do not invent payload fields and assume the runtime will use them.
- Do not put `observation` or `observations` fields into queue jobs.
- Split distinct operational facts into separate jobs.
- Prefer unresolved capture or reclassification over guessing.
- Use queue jobs for execution, not direct edits to canonical state.
- If the needed action does not map cleanly to a supported job type, keep it as non-executable intent.

Observation boundary:

- The event pipeline may attach `observations` to certain journal-bound events.
- Those observations are journal-only context.
- They are not part of the queue contract.
- They must not be authored as queue payload fields.
- They must not be used to drive structured writes, derived signals, hiring decisions, scheduling changes, or state updates.

## Identity Rules

### Site identity (type-dependent)

The field a job uses to identify a site **depends on the job type**. Check
the field name in `queue_spec.py` before authoring — there is no single
universal site field.

- **`site` = canonical site-name string.** Used by `visit_create`,
  `trigger_recruiting`, `close_recruiting`, `remove_from_schedule`,
  `flag_access_constraint`, and `flag_retention_risk`.
  Example: `"Continental Metalworks"`.
- **`site_id` = site-number string.** Used by `log_site_issue`,
  `log_supply_need`, `log_equipment_request`, and `promote_prospect`.
  Example: `"7060"`. Site/job numbers are identifiers, not numbers — always
  quote them as strings (`"7060"`, never `1200`).
- **Either** — `update_site_equipment` accepts `site` (name) **or**
  `site_id` (number).
- **Name-or-id** — `photo_capture` uses `site`, which the runtime tolerates
  as a canonical name **or** an id string.
- `log_personnel_event` has no site field; it carries an optional
  `related_site` string (site id, name, or reference).

Do not use:

- the wrong identity field for the job type — a `site_id`-keyed type rejects a
  bare site name, and a `site`-keyed type rejects a number (the only
  exceptions are `update_site_equipment` and `photo_capture` above)
- ad hoc aliases
- entity references in place of a site identifier

How the runtime resolves it:

- the runtime resolves site strings through the current site registry in [sites.py](project/event_pipeline/sites.py)
- production-style registry lookup uses CouchDB `btq_sites` when
  `BTQ_COUCHDB_URL` is set; local/dev fallback uses the checked-in registry
  only when CouchDB is not configured

### Employee identity

Use the employee name string in the `employee` field.

Examples:

- `Peter Nash`
- `Avery Doe`

The runtime resolves employee names against canonical `employee` documents.

## Contract Model

The queue contract has two layers:

1. `queue_spec.py` validation
   This is the executable contract. Jobs that fail this validation are rejected.
2. queue processor behavior
   Some handlers read additional optional fields that `queue_spec.py` does not currently validate.

For authoring:

- include `job_id`, `job_type`, and `payload` at the top level
- always include every required field
- only use documented optional fields when they are explicitly listed below
- do not assume undocumented fields have any effect

Top-level field notes:

- the allowed top-level fields are `job_id`, `job_type`, `payload`, `metadata`, `intent`, and `idempotency_key`; any other top-level key is rejected
- `job_type` must be one of the supported runtime job types
- `payload` must be a JSON object
- `job_id` must be a non-empty string if provided
- if `job_id` is omitted, `btq-enqueue` derives one (`<UTC-timestamp>__<slug>`) and uses it as the CouchDB `_id`

## Supported Job Types

## 1. `append_to_note`

Use when:

- the runtime already knows the exact canonical journal or site target reference
- the action is a plain note append, not a structured staffing, access, retention, visit, or reclassification action

Do not use when:

- the intent is really a staffing trigger, access issue, retention issue, visit anchor, or unknown reclassification
- the intent is onboarding, new employee creation, person registration, or any other entity creation; use `add_person`
- the content is a personnel/HR event (attendance, performance, accommodation,
  disciplinary, recognition, on-the-job incident); use `log_personnel_event`
- the content is a forward-looking availability fact, PTO date, or last working
  day; use `log_availability_constraint`
- you do not know the exact target path

### Required payload fields

- `path`: string
- `content`: string
- `destination`: one of:
  - `missed`
  - `journal_unknown`
  - `site_note`
  - `journal`

### Optional payload fields

- none in the executable contract

### Runtime behavior notes

- `path` is the canonical journal/site target reference
- exact duplicate content is skipped
- if the target is a site (`location`) record, the runtime may append `visit_key` metadata or a `visit_gap` block automatically
- if `content` contains a journal observation section such as:
  - `**Observations:**`
  - `- ...`
  the runtime still treats it as plain note text only

### Valid example

```json
{
  "job_id": "2026-04-20T12-00-00Z__journal-note-1",
  "job_type": "append_to_note",
  "payload": {
    "path": "Journal/2026-04-20.md",
    "content": "Chase Lynch started orientation today at Glenwood High School.",
    "destination": "journal"
  }
}
```

## 2. `add_person`

Use when:

- the user asks to add, create, onboard, or register a new employee/person
- the person's name and role are known

Do not use when:

- the name or role is unknown
- the request is only a note about an existing employee
- the payload includes or depends on an explicit storage path
- the intent is to merge with or mutate an existing person record

### Required payload fields

- `name`: string
- `role`: string

### Optional payload fields used by runtime

- `employee_id`: numeric string or integer
- `employment_type`: string
- `status`: string
- `job`: primary assigned work-site id
- `additional_jobs`: list of secondary assigned work-site ids
- `assignments`: list of assignment objects with:
  - `job`
  - `account`
  - `location`
  - `shift`
- `contact`: object with `phone` and `email`, each string or null
- `metadata`: object with `source`

### Optional top-level fields

- `idempotency_key`: string, strongly recommended for replay safety

Use a stable source-system key when available, for example `ehub-567`.

### Runtime behavior notes

- the writer creates the canonical `employee` document internally
- the writer generates a permanent `person_id` internally; it is not derived from the name
- the writer preserves the transitional person assignment model: `job` is the primary work site and `additional_jobs` are secondary work sites
- when `assignments` are present and no explicit `job` is supplied, the first assignment job becomes `job`; later assignment jobs become `additional_jobs`
- the job payload must not include `path`, `file`, `folder`, `directory`, or any explicit storage path
- if an `idempotency_key` has already completed with the same payload, replay is a safe no-op
- if an `idempotency_key` has already completed with a different payload, the job fails safely
- failed jobs do not mark an idempotency key as completed
- before creating an employee, the runtime checks canonical CouchDB employee docs for the same `employee_id` or normalized full name; duplicates fail safely unless the idempotency key already proves a completed replay
- the runtime does not merge into or mutate an existing person record

### Valid example

```json
{
  "job_id": "2026-05-01T15-00-00Z__add-eric-daniel-dalton",
  "job_type": "add_person",
  "idempotency_key": "ehub-567",
  "payload": {
    "name": "Eric Daniel Dalton",
    "employee_id": "567",
    "role": "Cleaner",
    "employment_type": "part_time",
    "status": "active",
    "additional_jobs": ["7071"],
    "assignments": [
      {
        "job": "7060",
        "account": "Contworks",
        "location": "Continental Metalworks Holdings",
        "shift": "evening"
      }
    ],
    "contact": {
      "phone": null,
      "email": null
    },
    "metadata": {
      "source": "manager_journal"
    }
  }
}
```

Expected output: a canonical `employee` document in `btq_vault` with identity
anchor:

```yaml
person_id: per_01JV8W7T6K8F9ABCD1234
```

The `person_id` is the canonical identity anchor. The `name` is presentation and may stop matching the person's record if names change later.

## 2a. `set_employee_id`

Use when:

- an existing person needs a late-arriving eHub or source-system employee ID
  attached to the canonical employee record
- the person can be resolved by person ID, current employee ID, or unique name
- the update is limited to `employee_id` and the narrow late-arriving scalar
  fields listed below

Do not use when:

- the person does not already exist; use `add_person`
- the request is a general active/inactive status change; use
  `set_entity_status`
- the request needs structured edits such as contact, assignments,
  additional jobs, or arbitrary person fields
- the target person cannot be resolved uniquely

### Required payload fields

- `person`: non-empty resolver string
- `employee_id`: numeric string or non-negative integer

### Optional payload fields used by runtime

- `employment_type`: string
- `hire_date`: string
- `status`: string
- `source`: string provenance label
- `metadata`: object with `source`

### Optional top-level fields

- `idempotency_key`: string, strongly recommended for replay safety

Use a stable source-system key when available, for example `ehub-9213`.

### Runtime behavior notes

- the writer resolves `person` to an existing canonical `employee` document
- the writer updates only `employee_id`, `employment_type`, `hire_date`, and
  `status` when those fields are present
- identity fields such as `_id`, `type`, `person_id`, `created_at`, and prior
  `btq_job_ids` are preserved
- if no person matches, multiple people match, or the `employee_id` is already
  claimed by a different person, the job fails safely without writing
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-06-12T18-30-00Z__set-bobby-pack-ehub-id",
  "job_type": "set_employee_id",
  "idempotency_key": "ehub-9213",
  "payload": {
    "person": "Bobby Pack",
    "employee_id": "9213",
    "employment_type": "full_time",
    "hire_date": "2026-06-12",
    "status": "active",
    "source": "ehub_email"
  }
}
```

## 2b. `set_employee_contact`

Use when an existing employee's phone number or email address must be added,
corrected, or cleared. The employee must resolve uniquely by person ID,
employee ID, or current display name.

Required payload fields:

- `person`: non-empty resolver string
- `actor`: non-empty audit label
- `contact`: a non-empty object containing only `phone` and/or `email`; each
  value is a non-empty string or `null` to clear that field

Optional payload fields:

- `source`: non-empty provenance label

The writer changes only the supplied contact fields, preserves all identity and
assignment data, records `updated_at` and `edited_by`, and appends the queue job
ID through canonical read-modify-write.

```json
{
  "job_type": "set_employee_contact",
  "payload": {
    "person": "9001",
    "actor": "Sandbox Operator",
    "contact": {"phone": "2025550100"},
    "source": "employee_message"
  }
}
```

## 2b-1. `set_employee_home_address`

Use when an existing employee's home address must be added, corrected, or
intentionally removed. The employee must resolve uniquely by person ID,
employee ID, or current display name. Home addresses are sensitive employee
PII: they exist for operator decision support (e.g. finding potential
short-notice coverage near a site) and never appear on worker-facing surfaces,
static projections, or in log lines.

Required payload fields:

- `person`: non-empty resolver string
- `actor`: non-empty audit label

Optional payload fields:

- `action`: `set` (default) or `clear`; `clear` removes the stored address and
  must not also carry a `home_address`
- `home_address`: required when action is `set` — an object containing only:
  - `line1`: non-empty string (required)
  - `line2`: non-empty string or `null`
  - `city`: non-empty string (required)
  - `state`: non-empty string (required)
  - `postal_code`: non-empty string (required; US format `#####` or
    `#####-####` unless `country` is non-US)
  - `country`: non-empty string or `null` (defaults to US semantics)
- `source`: non-empty provenance label, stored as `home_address_source`

Unknown address keys, free-form strings, nested structures, and partial
addresses fail validation. Only operator-confirmed or authoritative HR/eHub
addresses may be authored — never infer an address from other data.

The writer changes only the address and audit fields, preserves all identity,
contact, and assignment data, records `updated_at` and `edited_by`, and appends
the queue job ID through canonical read-modify-write; replays are no-ops.

```json
{
  "job_type": "set_employee_home_address",
  "payload": {
    "person": "9001",
    "actor": "Sandbox Operator",
    "home_address": {
      "line1": "123 Example Street",
      "city": "Exampletown",
      "state": "PA",
      "postal_code": "15900"
    },
    "source": "operator_confirmed"
  }
}
```

## 2c. `assign_employee_site`

Use when:

- an existing employee needs one canonical site assignment added or removed
- the target person can be resolved by employee document `_id`, `person_id`,
  current `employee_id`, or unique display name
- the change is limited to the employee document's `site_ids` list

Do not use when:

- the person does not already exist; use `add_person`
- the request needs to edit name, status, contact, content, or arbitrary
  employee fields
- the target person cannot be resolved uniquely

### Required payload fields

- `employee_id`: non-empty resolver string
- `site_id`: non-empty site id string, for example `"592"`
- `actor`: non-empty string

### Optional payload fields used by runtime

- `action`: either `assign` or `unassign`; defaults to `assign`
- `source`: string provenance label

### Runtime behavior notes

- the writer resolves `employee_id` to an existing canonical `employee`
  document
- `assign` appends `site_id` to `site_ids` only when it is not already present
- `unassign` removes `site_id` from `site_ids` when present
- existing `site_ids` order is preserved and duplicate entries are collapsed to
  the first occurrence
- every other employee field is preserved, including status, name, employee ID,
  content, and existing audit/history fields
- if no person matches, multiple people match, CouchDB cannot be read/written,
  or the employee document is missing, the job fails safely without writing
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid examples

```json
{
  "job_id": "2026-06-22T10-00-00Z__assign-greenwood-megan-592",
  "job_type": "assign_employee_site",
  "payload": {
    "employee_id": "employee_greenwood_megan",
    "site_id": "592",
    "actor": "Greg",
    "action": "assign",
    "source": "operator_review"
  }
}
```

```json
{
  "job_id": "2026-06-22T10-05-00Z__unassign-greenwood-megan-592",
  "job_type": "assign_employee_site",
  "payload": {
    "employee_id": "employee_greenwood_megan",
    "site_id": "592",
    "actor": "Greg",
    "action": "unassign",
    "source": "operator_review"
  }
}
```

## 2d. `record_shift_report`

Use when:

- an end-of-day shift report should land in canonical `btq_vault`
- the report date is known and the Markdown content is ready for deterministic
  capture

Do not use when:

- the entry is a personal, non-operational journal note; use
  `personal_journal_entry`
- the report should only be preserved as raw evidence without a canonical
  shift-report document
- you are trying to mutate downstream projections or reader behavior directly

### Required payload fields

- `date`: `YYYY-MM-DD`
- `content`: non-empty Markdown string

### Optional payload fields

- `prepared_by`: string; defaults to `Greg` at runtime
- `source`: string provenance label

### Optional top-level fields

- `idempotency_key`: string, strongly recommended for replay safety

Use a stable date-keyed value when available, for example
`shift-report-2026-06-12`.

### Runtime behavior notes

- upserts the canonical `shift_report` document in `btq_vault`
- the document id is keyed by date:
  `shift_report_journal_YYYY_MM_DD_shift_report`
- the writer sets `date`, `operator`, `prepared_by`, and `content`
- `operator` is resolved by the handler, not supplied by the job payload
- reprocessing the same job is idempotent via the `btq_job_ids` marker
- a new job for the same date updates the same canonical document instead of
  creating a duplicate

### Valid example

```json
{
  "job_id": "2026-06-12T23-00-00Z__record-shift-report",
  "job_type": "record_shift_report",
  "idempotency_key": "shift-report-2026-06-12",
  "payload": {
    "date": "2026-06-12",
    "prepared_by": "Greg",
    "source": "closeday",
    "content": "# Shift Report\n\nEnd-of-day operations summary."
  }
}
```

## 2e. `shift_report_note`

Use when:

- an operator captures a displayed deep-analysis finding into today's shift report
- the full analysis text and photo references should be preserved as an
  operational fact for closeday report generation
- the capture should also leave durable canonical traceability in `btq_vault`

Do not use when:

- generating or replacing the full end-of-day shift report; use
  `record_shift_report`
- requesting additional image analysis; use `deep_analysis`
- mutating operational vault records directly from a raw capture or AI summary

### Required payload fields

- `date`: `YYYY-MM-DD`
- `content`: non-empty full analysis text
- `actor`: operator name or id confirming the send
- `capture_id`: source field capture document id
- `photo_asset_id`: source photo asset id within the capture

### Optional payload fields

- `site_id`: string site identifier when known
- `prompt_id`: string deep-analysis prompt id when known
- `prompt_label`: display label for the prompt when known

### Optional top-level fields

- `idempotency_key`: string, strongly recommended for replay safety

Use a stable content-aware value derived from the date, content, capture id,
photo asset id, and prompt metadata.

### Runtime behavior notes

- closeday reads today's queue payloads directly, so this job flows into the
  generated shift report without changing closeday
- upserts a canonical `shift_report_note` document in `btq_vault` for durable
  traceability
- the document id is content-aware and date-scoped:
  `shift_report_note_YYYY_MM_DD_<content_hash>`
- identical resends of the same note target the same document; distinct notes
  for the same date coexist as separate documents
- the writer sets `date`, `content`, `capture_id`, `photo_asset_id`, optional
  prompt/site fields, `actor`, `operator`, and `captured_at`
- `operator` and `captured_at` are resolved by the handler, not supplied by the
  job payload
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-06-12T18-15-00Z__shift-report-note-cap-photo-2026-06-12T14-48-00-04-00",
  "job_type": "shift_report_note",
  "idempotency_key": "shift-report-note:7f3d8c5b1a0e9d2c4b6a8130f9e2d1c0",
  "payload": {
    "date": "2026-06-12",
    "content": "Deep analysis found a damaged threshold at the west entrance.",
    "actor": "Jordan Avery",
    "capture_id": "cap-photo-2026-06-12T14-48-00-04-00",
    "photo_asset_id": "photo-west-entrance-1",
    "site_id": "7050",
    "prompt_id": "damage_hazard",
    "prompt_label": "Damage / hazard"
  }
}
```

## 2f. `record_day_record`

Use when:

- an operational day record should land in canonical `btq_vault`
- the day-record date is known and the Markdown content is ready for
  deterministic capture

Do not use when:

- the entry is a shift report; use `record_shift_report`
- the entry is a personal, non-operational journal note; use
  `personal_journal_entry`
- the record should only be preserved as raw evidence without a canonical
  day-record document
- you are trying to mutate downstream projections or reader behavior directly

### Required payload fields

- `date`: `YYYY-MM-DD`
- `content`: non-empty Markdown string

### Optional payload fields

- `source`: string provenance label

### Optional top-level fields

- `idempotency_key`: string, strongly recommended for replay safety

Use a stable date-keyed value when available, for example
`day-record-2026-06-12`.

### Runtime behavior notes

- upserts the canonical `day_record` document in `btq_vault`
- the document id is keyed by date: `day_record_YYYY_MM_DD`
- the writer sets `date`, `operator`, and `content`
- `operator` is resolved by the handler, not supplied by the job payload
- reprocessing the same job is idempotent via the `btq_job_ids` marker
- a new job for the same date updates the same canonical document instead of
  creating a duplicate

### Valid example

```json
{
  "job_id": "2026-06-12T23-05-00Z__record-day-record",
  "job_type": "record_day_record",
  "idempotency_key": "day-record-2026-06-12",
  "payload": {
    "date": "2026-06-12",
    "source": "closeday",
    "content": "## Day Record — 2026-06-12\n\nOperational day notes."
  }
}
```

## 3. `trigger_recruiting`

Use when:

- the fact is a site-level staffing shortage or open-position signal
- the site is known

Do not use when:

- the statement is about one employee possibly quitting
- the site is unknown
- it is only a general note with no recruiting implication

### Required payload fields

- `site`: string
- `priority`: string
- `details`: string

### Optional payload fields used by runtime

- `date`: string, expected as `YYYY-MM-DD`
- `open_positions`: integer or stringifiable value

### Runtime behavior notes

- `priority` is required by `queue_spec.py`
- `priority` does not currently have an enforced enum in `queue_spec.py`
- the runtime writes this to the resolved `location` document
- if no `date` is supplied, the runtime uses the current UTC date

### Valid example

```json
{
  "job_id": "2026-04-20T12-05-00Z__recruiting-egt-1",
  "job_type": "trigger_recruiting",
  "payload": {
    "site": "Western Gas Transmission",
    "priority": "high",
    "details": "Two openings remain on site.",
    "date": "2026-04-20",
    "open_positions": 2
  }
}
```

## 4. `remove_from_schedule`

Use when:

- an employee has resigned or should be removed from schedule coverage
- both employee and site are known

Do not use when:

- the person merely called off once
- the employee identity is uncertain

### Required payload fields

- `employee`: string
- `site`: string

### Optional payload fields used by runtime

- `date`: string, expected as `YYYY-MM-DD`

### Runtime behavior notes

- the runtime resolves the employee from canonical `employee` documents
- if no `date` is supplied, the runtime uses the current UTC date

### Valid example

```json
{
  "job_id": "2026-04-20T12-10-00Z__schedule-removal-1",
  "job_type": "remove_from_schedule",
  "payload": {
    "employee": "Peter Nash",
    "site": "Western Gas Transmission",
    "date": "2026-04-20"
  }
}
```

## 5. `flag_access_constraint`

Use when:

- the fact is about keys, badges, entry dependencies, locked areas, or other access blockers
- the site is known

Do not use when:

- it is a generic site observation with no access implication
- it is really a staffing or retention issue

### Required payload fields

- `site`: string
- `details`: string

### Optional payload fields used by runtime

- `date`: string, expected as `YYYY-MM-DD`
- `blocking`: boolean or other truthy/falsy value

### Runtime behavior notes

- the runtime writes this to the resolved `location` document
- if no `date` is supplied, the runtime uses the current UTC date
- `blocking` is currently runtime-tolerated but not enforced or required by `queue_spec.py`

### Valid example

```json
{
  "job_id": "2026-04-20T12-15-00Z__access-egt-1",
  "job_type": "flag_access_constraint",
  "payload": {
    "site": "Western Gas Transmission",
    "details": "Only one employee has the badge that can open the main entry.",
    "date": "2026-04-20",
    "blocking": true
  }
}
```

## 6. `flag_retention_risk`

Use when:

- there is a site-linked retention concern for a named employee
- employee, site, and details are all known

Do not use when:

- the employee name is missing
- the fact is only about staffing shortage
- the statement is too speculative to state as an operational fact

### Required payload fields

- `employee`: string
- `site`: string
- `details`: string

### Optional payload fields used by runtime

- `date`: string, expected as `YYYY-MM-DD`

### Runtime behavior notes

- the runtime appends this to the resolved `employee` document
- if no `date` is supplied, the runtime uses the current UTC date

### Valid example

```json
{
  "job_id": "2026-04-20T12-20-00Z__retention-egt-1",
  "job_type": "flag_retention_risk",
  "payload": {
    "employee": "Peter Nash",
    "site": "Western Gas Transmission",
    "details": "May leave if the evening workload stays the same.",
    "date": "2026-04-20"
  }
}
```

## 7. `reclassify_unknown`

Use when:

- you want the runtime to rescan a specific unknown journal file
- unresolved captures in that file may now be classifiable

Do not use when:

- you are trying to rerun transcription
- you do not have a specific unknown journal file path

### Required payload fields

- `path`: string

### Optional payload fields

- none in the executable contract

### Runtime behavior notes

- `path` is the canonical reference to the unknown-capture record to rescan
- this does not rerun Whisper

### Valid example

```json
{
  "job_id": "2026-04-20T12-25-00Z__reclassify-unknown-1",
  "job_type": "reclassify_unknown",
  "payload": {
    "path": "Journal/2026-04-20-unknown.md"
  }
}
```

## 7b. `record_unknown_capture`

> Producer-emitted, not hand-authored. The transcription pipeline emits this job
> when a capture cannot be classified into structured events. It writes a canonical
> `unknown_capture` document. Documented here for contract completeness.

Use when:

- the transcription pipeline could not route a capture to deterministic events
- you are recording an unresolved capture for later reclassification

Do not use when:

- you can produce a structured event job directly
- you are hand-authoring a note (use `append_to_note`)

### Required payload fields

- `path`: string — the canonical unknown-capture reference (`Journal/<date>-unknown.md` form)
- `content`: string — the rendered `unknown_capture` block
- `timestamp`: string — capture timestamp (ISO-8601)
- `audio_file`: string — source audio filename

### Optional payload fields

- `capture_id`, `original_transcript`, `normalized_transcript`, `capture_status`,
  `events_created`, `reason_heading`, `reasons`

### Runtime behavior notes

- writes a canonical `unknown_capture_<source_unknown_id>` document with
  `operator` set
- canonical dedup is by `btq_job_ids`; re-recording the same capture does not
  reset its status/retry state
- `append_to_note` no longer accepts `*-unknown.md` references

### Valid example

```json
{
  "job_id": "2026-04-20T12-25-00Z__record-unknown-1",
  "job_type": "record_unknown_capture",
  "payload": {
    "path": "Journal/2026-04-20-unknown.md",
    "content": "---\ntype: unknown_capture\n...\n",
    "timestamp": "2026-04-20T12:25:00+00:00",
    "audio_file": "walkthrough.m4a"
  }
}
```

## 8. `visit_create`

Use when:

- physical site presence is clearly implied
- the site is known
- you want a visit anchor created for same-day activity linking

Do not use when:

- it was only a phone call
- it is a planned future visit
- the site is unresolved
- the evidence is uncertain

### Required payload fields

- `site`: non-empty string
- `confidence`: one of:
  - `high`
  - `medium`
- `source`: non-empty string
- `evidence`: non-empty string

### Optional payload fields

- `occurred_at`: ISO-8601 datetime string with a timezone offset, such as
  `2026-06-23T21:30:32-04:00`; naive datetimes without a timezone are rejected

### Runtime behavior notes

- the runtime writes a `visit` record for the resolved site and local operational date
- duplicate evidence in the same visit file is skipped
- when `occurred_at` is supplied, `date` is derived from that instant in the
  default operational timezone, currently `America/New_York`
- when `occurred_at` is omitted, the processor runtime is used, but the visit
  `date` is still derived from `America/New_York`, not the UTC calendar date
- the canonical `timestamp` is stored as UTC-normalized event time, and the
  canonical document records the timezone used for date derivation

### Producer guidance

Producers should forward the capture or event time as `occurred_at`, using
`queue_spec.normalize_occurred_at` to coerce local or UTC timestamps into a
validator-safe value. Voice and photo captures should use the device capture
time, sent-email-derived QCs should use the sent-email timestamp, and
operator-chat backfills should use the explicit local event time when known.
When the event time is unknown, require review rather than silently using
processing time.

### Valid example

```json
{
  "job_id": "2026-04-20T12-30-00Z__visit-egt-1",
  "job_type": "visit_create",
  "payload": {
    "site": "Western Gas Transmission",
    "confidence": "high",
    "source": "ingestion",
    "evidence": "I was at Western Gas Transmission this afternoon.",
    "occurred_at": "2026-04-20T14:30:00-04:00"
  }
}
```

## 9. `photo_capture`

Use when:

- a local field photo should become canonical evidence
- the photo should be saved as media and linked from the daily journal entry
- the capture is observational, not a direct structured staffing or scheduling action

Do not use when:

- the input is only text; use `append_to_note` instead
- the image data is not a base64 data URL
- the photo should bypass the queue processor

### Required payload fields

- `site`: string
- `qc_category`: string
- `note`: string, may be empty
- `captured_at`: ISO datetime string
- `exported_at`: ISO datetime string
- `photos`: non-empty list of photo objects

Each photo object requires:

- `filename`: string
- `mime_type`: one of `image/jpeg`, `image/png`, or `image/webp`
- `data_url`: base64 data URL matching `mime_type`

### Runtime behavior notes

- the runtime saves the photo media and links it from a canonical `journal` entry
- the runtime appends a linked photo-capture entry to the dated `journal` record
- the journal date is derived from `captured_at`
- the runtime refuses to overwrite an existing media filename

### Valid example

```json
{
  "job_id": "2026-04-30T18-00-00Z__photo-capture-summit-wire",
  "job_type": "photo_capture",
  "metadata": {
    "capture_id": "cap-photo-2026-04-30T18-00-00Z",
    "source": "field_capture_app"
  },
  "payload": {
    "site": "Summit Wire",
    "qc_category": "Restrooms",
    "note": "Trash accumulation at admin entrance.",
    "captured_at": "2026-04-30T14:00:00-04:00",
    "exported_at": "2026-04-30T14:02:00-04:00",
    "photos": [
      {
        "filename": "admin-entrance.jpg",
        "mime_type": "image/jpeg",
        "data_url": "data:image/jpeg;base64,..."
      }
    ]
  }
}
```

## 9a. `deep_analysis`

Use when:

- an operator needs deeper image analysis on a single field-capture photo
- the base per-capture vision pass has already run or remains unchanged
- the result should be attached to the photo-vision sidecar for review

Do not use when:

- the action should mutate the operational vault directly
- the prompt should use a cloud vision model
- the request is for bulk analysis or a whole capture set instead of one photo

### Required payload fields

- `capture_id`: field capture document id
- `photo_asset_id`: photo asset id within the capture
- `actor`: operator name or id initiating the analysis

Exactly one prompt source is required:

- `preset_id`: one of `condition_detail`, `damage_hazard`, `cleanliness_qc`, `text_ocr`, `inventory_count`, `equipment_id`, or `shift_report`
- `custom_prompt`: non-empty operator-supplied prompt

Never provide both `preset_id` and `custom_prompt`. Never omit both.

### Optional payload fields

- none in the executable contract

### Runtime behavior notes

- this is an operator-triggered tier-2 escape hatch for deeper analysis on one capture photo
- the runtime runs the configured local vision model only; it does not call cloud vision APIs
- a vision or image failure records a `failed` deep-analysis result instead of failing the queue job
- the runtime appends the result to the photo-vision sidecar's `deep_analysis` list
- the runtime appends one metric event to `deep_analysis_requests.jsonl`

### Valid example

```json
{
  "job_id": "2026-05-28T18-00-00Z__deep-analysis-cap-photo-2026-05-28T12-48-00-04-00",
  "job_type": "deep_analysis",
  "payload": {
    "capture_id": "cap-photo-2026-05-28T12-48-00-04-00",
    "photo_asset_id": "photo-admin-entrance-1",
    "actor": "Jordan Avery",
    "preset_id": "damage_hazard"
  }
}
```

## 9b. `promote_prospect`

Use when:

- an operator has registered a won prospect as a live site
- existing field captures targeted at that prospect should be retargeted to the live site
- the prospect document should be marked won with promotion metadata

Do not use when:

- the destination `site_id` has not been registered yet
- the prospect is still open, quoted, lost, or withdrawn
- the action is only a raw intake or AI interpretation; promotion is an operator-approved mutation

### Required payload fields

- `prospect_id`: prospect identifier without the `prospect_` CouchDB prefix
- `site_id`: registered live site id
- `actor`: operator name or id initiating the promotion

### Runtime behavior notes

- the runtime fails closed if the prospect is missing or already promoted elsewhere
- the runtime fails closed if the destination site is not registered
- matching CouchDB captures move from `target_type="prospect"` to `target_type="location"`
- matching local field-capture intake JSONs are retargeted the same way

### Valid example

```json
{
  "job_id": "2026-05-27T20-00-00Z__promote-prospect-kmf-birch",
  "job_type": "promote_prospect",
  "payload": {
    "prospect_id": "kmf-birch-1",
    "site_id": "1801",
    "actor": "Jordan Avery"
  }
}
```

## 9c. `retarget_capture`

Use when:

- a single field capture was submitted against the wrong site or prospect
- an operator has confirmed the replacement target is a real site or active prospect
- the capture document and matching local intake JSON need the same target correction

Do not use when:

- the capture has already reached `stage == "acted_on"`
- the destination target is empty, `null`, `none`, `discard`, or any other sentinel
- the destination site is not registered or the destination prospect is terminal
- the operator wants bulk retargeting, deletion, or a discard flow

### Required payload fields

- `capture_id`: capture document id to retarget
- `new_target_type`: `location` or `prospect`
- `new_target_id`: registered site id or active prospect id
- `actor`: operator name or id initiating the retarget

### Runtime behavior notes

- the runtime fails closed if the capture is missing
- the runtime fails closed if the destination target is invalid or terminal
- retargeting to the capture's current target is idempotent and does not append history
- successful retarget mutates `target_type`, `target_id`, `site_id`, and appends `retarget_history`
- the matching local field-capture intake JSON is retargeted when present
- media URLs are not reissued; downstream projections rebuild URLs from the current capture document on next access

### Valid example

```json
{
  "job_id": "2026-05-28T17-00-00Z__retarget-capture-cap-photo-2026-05-28T12-48-00-04-00",
  "job_type": "retarget_capture",
  "payload": {
    "capture_id": "cap-photo-2026-05-28T12-48-00-04-00",
    "new_target_type": "location",
    "new_target_id": "1801",
    "actor": "Jordan Avery"
  }
}
```

## 10. `log_site_issue`

Use when:

- a reviewed operational site issue needs a structured canonical record
- the issue has a clear site, title, reporter, resolution trigger, and summary or observations
- client communication status must be tracked separately from resolution

Do not use when:

- the note is only raw intake or unresolved interpretation
- the action should merely append context to a note
- the issue is already client-facing copy; this is internal operational memory
- client notification should be treated as resolved; use `status: "resolved"` only when the resolution trigger has happened

### Required payload fields

- `site_id`: string
- `title`: string
- `reported_by`: employee/person string
- `client_notified`: boolean
- `resolution_trigger`: string describing the event that closes the issue
- either `summary`: non-empty string or `observations`: non-empty list of strings

### Optional payload fields

- `observations`: list of strings
- `category`: one of `maintenance`, `supply`, `access`, `staffing`, `quality`, `safety`, `client_request`, `other`
- `priority`: one of `low`, `normal`, `high`, `urgent`
- `status`: one of `open`, `monitoring`, `resolved`; defaults to `open`
- `observed_at`: ISO datetime string
- `source`: string such as `field_capture` or `daily_log`
- `notes`: string with extra operational context not covered by `summary`,
  `observations`, or the resolution fields (for example, links to related
  issues)
- `client_notified_at`, `client_notified_by`, `client_notified_method`, `client_notified_note`
- legacy aliases `client_informed*` are accepted for compatibility, but new jobs should use `client_notified*`
- `related_capture_ids`, `related_candidate_ids`, `related_media`, `source_artifacts`: lists of strings
- `resolved_at`, `resolution_summary`
- `issue_id`: explicit stable ID when updating a known issue

### Runtime behavior notes

- writes a canonical `site_issue` document keyed by `issue_id`
- derives a deterministic `issue_id` from site, title, observed time, source, and first capture reference unless `issue_id` is supplied
- reprocessing the same job does not duplicate the issue file
- a later job with the same explicit `issue_id` updates the same issue and preserves `btq_job_ids` history
- `client_notified` means communication happened; it does not mean resolved

### Valid example

```json
{
  "job_id": "2026-05-08T16-00-00Z__summit-wire-drain-issue",
  "job_type": "log_site_issue",
  "payload": {
    "site_id": "7050",
    "title": "Restroom drain backup and inoperable stall",
    "summary": "Drain backed up and the sink drain pushed water onto the restroom floor.",
    "observations": [
      "Drain backed up in the restroom.",
      "Sink drain backed up onto the floor.",
      "Missing mop limits immediate cleanup.",
      "Metal stall is inoperable."
    ],
    "category": "maintenance",
    "priority": "high",
    "status": "open",
    "observed_at": "2026-05-06T18:27:03-04:00",
    "reported_by": "Tom Walsh",
    "source": "field_capture",
    "client_notified": true,
    "client_notified_at": "2026-05-08T15:10:00-04:00",
    "client_notified_by": "Jordan",
    "client_notified_method": "email",
    "client_notified_note": "Emailed client with photo/context.",
    "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
    "related_capture_ids": ["cap-photo-summit-drain"],
    "related_candidate_ids": ["ac_386bdf44bf4f08764e5a7bb7"],
    "related_media": ["/media/cap-photo-summit-drain/drain.jpg"]
  }
}
```

## 11. `log_supply_need`

Use when:

- a reviewed field capture identifies a consumable supply restock need
- the item is site-specific and should be tracked through ordering, delivery,
  and stocking
- the request should be filterable by urgency or status instead of buried in a
  journal line

Do not use when:

- the item is durable equipment or a tool request
- the concern is broken, damaged, leaking, unsafe, or client-facing; use
  `log_site_issue`
- the capture is raw intake that has not been reviewed

### Required payload fields

- `site_id`: string
- `item_name`: string
- `requested_by`: employee/person string

### Optional payload fields

- `quantity_needed`: string
- `urgency`: one of `low`, `normal`, `high`, `critical`; defaults to `normal`
- `status`: one of `open`, `ordered`, `delivered`, `stocked`,
  `no_action_needed`; defaults to `open`
- `observed_at`: ISO datetime string
- `source`: string such as `field_capture` or `daily_log`
- `notes`: string
- `related_capture_ids`, `related_candidate_ids`, `related_media`,
  `source_artifacts`: lists of strings
- `supply_id`: explicit stable ID when updating a known supply need
- `ordered_at`, `ordered_by`, `ordered_note`
- `delivered_at`, `delivered_by`, `delivered_note`
- `stocked_at`, `stocked_by`, `stocked_note`

### Runtime behavior notes

- writes a canonical `supply_need` document keyed by `supply_id`
- derives a deterministic `supply_id` from site, item, requester, and observed
  time unless `supply_id` is supplied
- reprocessing the same job does not duplicate the supply file
- a later job with the same explicit `supply_id` updates the same record and
  preserves `created_at`, `supply_id`, and `btq_job_ids` history

### Valid example

```json
{
  "job_id": "2026-05-08T17-00-00Z__summit-wire-brightwash-cleaner",
  "job_type": "log_supply_need",
  "payload": {
    "site_id": "7050",
    "item_name": "BrightWash cleaner",
    "quantity_needed": "2 bottles",
    "urgency": "high",
    "requested_by": "Tom Walsh",
    "observed_at": "2026-05-08T14:12:43+00:00",
    "source": "field_capture",
    "notes": "Supply closet is empty.",
    "related_capture_ids": ["cap-supply-summit"],
    "related_candidate_ids": ["ac_supply_summit"]
  }
}
```

## 11a. `create_supply_request`

Use when one reviewed request contains one or more consumable supply lines that
must remain together as a single request. This job is distinct from
`log_supply_need`, which records one independently tracked need, and from
`supply_order`, which represents a parsed receipt.

Do not use for durable equipment, prices, budgets, cost estimates, or vendor
order data.

### Required payload fields

- `site_id`: string
- `requested_by`: employee/person string
- `items`: non-empty list; every item requires a non-blank `item_name`
- `observed_at`: ISO 8601 datetime string

Each item may also carry `quantity` as a number or free-text string, `unit`,
and `note`. Item order is preserved and duplicate-looking lines are not
deduplicated.

`observed_at` is **required here even though it is optional on
`log_supply_need`**, and it is deliberately part of the identity seed. Without
it, two unrelated requests carrying the same item list at the same site — filed
months apart — derive the SAME record id, and the later write silently destroys
the earlier submission's `related_capture_ids` and `notes`. Supplying the real
observation time is what keeps distinct requests distinct.

### Optional payload fields

- `notes`: string
- `related_capture_ids`: list of non-empty strings
- `source`: string such as `field_capture_audio`
- `request_id`: string; explicit identity override, used verbatim as the id
  seed. Mirrors `supply_id` on `log_supply_need`. Use it when two genuinely
  separate requests would otherwise collide, or when a caller needs to own the
  record id. Supplying the same `request_id` twice is treated as the same
  request.

### Runtime behavior notes

- writes one canonical `supply_request` document containing every submitted
  item
- defaults `status` to `open`
- derives a deterministic `supply_request_id` from the normalized site,
  requester, observed time, and ordered item list — or from `request_id`
  verbatim when supplied
- the id is deterministic and is **never** salted with wall-clock time; doing so
  would break replay idempotency, and the test suite actively forbids it
- reprocessing the same job does not duplicate the record or append duplicate
  items
- resubmission preserves prior `btq_job_ids`, so provenance accumulates and an
  earlier job's replay marker survives

### Valid example

```json
{
  "job_id": "2026-07-22T13-15-00Z__public-supply-request",
  "job_type": "create_supply_request",
  "payload": {
    "site_id": "public-site-001",
    "requested_by": "Public Worker",
    "items": [
      {"item_name": "Paper products", "quantity": "2 cases"},
      {"item_name": "Hand soap", "quantity": 4, "unit": "containers"}
    ],
    "observed_at": "2026-07-22T09:15:00-04:00",
    "notes": "Restock request.",
    "related_capture_ids": ["capture-public-001"],
    "source": "field_capture_audio"
  }
}
```

## 12. `log_equipment_request`

Use when:

- a reviewed field capture identifies a durable tool or equipment request
- the item is site-specific and should be tracked through approval, ordering,
  and provision
- the request should be filterable by priority or status instead of buried in a
  journal line

Do not use when:

- the item is a consumable supply restock need; use `log_supply_need`
- the concern is building maintenance, damage, leaking, unsafe, or
  client-facing; use `log_site_issue`
- the capture is raw intake that has not been reviewed

### Required payload fields

- `site_id`: string
- `equipment_name`: string
- `requested_by`: employee/person string

### Optional payload fields

- `reason`: string
- `priority`: one of `low`, `normal`, `high`, `urgent`; defaults to `normal`
- `status`: one of `open`, `approved`, `ordered`, `provided`, `denied`,
  `no_action_needed`; defaults to `open`
- `observed_at`: ISO datetime string
- `source`: string such as `field_capture` or `daily_log`
- `notes`: string
- `related_capture_ids`, `related_candidate_ids`, `related_media`,
  `source_artifacts`: lists of strings
- `equipment_id`: explicit stable ID when updating a known equipment request
- `approved_at`, `approved_by`, `approval_note`
- `denied_at`, `denied_by`, `denial_note`
- `ordered_at`, `ordered_by`, `ordered_note`
- `provided_at`, `provided_by`, `provided_note`

### Runtime behavior notes

- writes a canonical `equipment_request` document keyed by `equipment_id`
- derives a deterministic `equipment_id` from site, equipment name, requester,
  and observed time unless `equipment_id` is supplied
- reprocessing the same job does not duplicate the equipment request file
- a later job with the same explicit `equipment_id` updates the same record and
  preserves `created_at`, `equipment_id`, and `btq_job_ids` history

### Valid example

```json
{
  "job_id": "2026-05-08T17-00-00Z__summit-wire-vacuum",
  "job_type": "log_equipment_request",
  "payload": {
    "site_id": "7050",
    "equipment_name": "vacuum",
    "reason": "Current vacuum will not start.",
    "priority": "urgent",
    "requested_by": "Tom Walsh",
    "observed_at": "2026-05-08T14:12:43+00:00",
    "source": "field_capture",
    "notes": "Needed for lobby carpet.",
    "related_capture_ids": ["cap-equipment-summit"],
    "related_candidate_ids": ["ac_equipment_summit"]
  }
}
```

## 13. `update_site_equipment`

Use when:

- AC or the operator has confirmed a site's current cleaning equipment during a
  physical site visit
- the inventory snapshot should be visible in the site's operational notes
  instead of buried in a chronological journal or review section
- the table should replace the prior snapshot for that site

Do not use when:

- the item is a durable equipment request with approval, ordering, or provision
  workflow; use `log_equipment_request`
- the item is a consumable supply restock need; use `log_supply_need`
- the observation came from unreviewed raw intake or should surface as a review
  candidate

### Required payload fields

- `site` or `site_id`: site canonical name or site ID string
- `equipment`: non-empty list of equipment items
- `inspection_date`: `YYYY-MM-DD`
- `inspected_by`: employee/person string

Each `equipment` item requires:

- `description`: string
- `brand`: string
- `color`: string
- `status`: one of `operational`, `non_functional`, `untested`

### Optional payload fields

- `section_notes`: string

Each `equipment` item may include:

- `notes`: string

### Runtime behavior notes

- updates the equipment inventory on the resolved `location` document in place
- replaces the prior `Supplies / Equipment` snapshot under the location's
  operational notes, creating the subsection if absent
- requires the location's operational-notes section to already exist; the
  processor raises if it is missing
- reprocessing the same job is idempotent via the `job_id` marker HTML comment
- a later job with a different `job_id` replaces the table

### Valid example

```json
{
  "job_id": "2026-05-13T17-00-00Z__continental-equipment-inventory",
  "job_type": "update_site_equipment",
  "payload": {
    "site": "Continental Metalworks",
    "inspection_date": "2026-05-13",
    "inspected_by": "Jordan",
    "section_notes": "Jordan has blocked new equipment purchase without a client billing conversation. Repair path for blue scrubber is the current open action.",
    "equipment": [
      {
        "description": "Large walk-behind scrubber",
        "brand": "Viper",
        "color": "Red",
        "status": "operational",
        "notes": "Used 1x/week (2x/week colder months)"
      },
      {
        "description": "Small walk-behind scrubber",
        "brand": "(unknown)",
        "color": "Blue",
        "status": "non_functional",
        "notes": "Leaks; replacement vs. repair pending"
      },
      {
        "description": "iMop",
        "brand": "(unknown)",
        "color": "(unknown)",
        "status": "untested",
        "notes": "Fully charged but not in active use"
      }
    ]
  }
}
```

## 13a. `set_site_url`

Use when:

- an operator has a reference link for a canonical site, such as the client's
  location page, homepage, map link, portal, or related document
- the link should be available to agents and the dashboard as site context
- the action is to add, edit, or remove a URL on the canonical `location` doc

Do not use when:

- the link has not been reviewed by an operator
- the intent is to scrape or import facts from the linked page
- the action should change operational fields such as address, hours, access,
  supplies, equipment, or staffing

### Required payload fields

- `site_id`: site canonical name, alias, or site ID string
- `action`: one of `add`, `edit`, `remove`
- `url`: HTTP(S) URL; for edit this identifies the existing entry
- `actor`: operator/person string

### Optional payload fields

- `new_url`: HTTP(S) replacement URL for `edit`
- `label`: human-readable label
- `kind`: one of `official_location_page`, `client_homepage`, `maps`,
  `portal`, `document`, `other`; required for `add`
- `status`: one of `reference`, `verified`, `stale`, `deprecated`; defaults
  to `reference`
- `last_verified_at`, `last_verified_by`, `verification_note`: explicit
  operator-provided verification metadata
- `source`: string such as `ops_dashboard_site_detail` or `manual_review`

### Runtime behavior notes

- resolves `site_id` through the site registry and mutates
  `location_<site_id>` through canonical CouchDB read-modify-write
- `add` appends a new entry or replaces the existing entry with the same URL
- `edit` updates the matching URL in place; `remove` drops the matching URL
- verification status is operator-set; storing a URL never changes operational
  site facts
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-06-26T14-00-00Z__continental-site-url",
  "job_type": "set_site_url",
  "payload": {
    "site_id": "7060",
    "action": "add",
    "url": "https://example.com/locations/continental-metalworks",
    "label": "Official location page",
    "kind": "official_location_page",
    "status": "reference",
    "actor": "Greg",
    "source": "ops_dashboard_site_detail"
  }
}
```

## 13b. `set_site_hours`

Use when:

- an operator has verified or reviewed the client facility's operating hours for
  a canonical site
- the hours should be available to agents and the dashboard as structured site
  context
- the action is to set or clear `facility_hours` on the canonical `location` doc

Do not use when:

- the hours are inferred from scraping, search results, documents, or AI summary
  without operator review
- the intent is to change B&T service schedule fields such as `service_days`,
  `hours_per_week`, or `hours_per_day`
- the hours are only a planned future visit or a B&T cleaning shift

### Required payload fields

- `site_id`: site canonical name, alias, or site ID string
- `actor`: operator/person string

### Optional payload fields

- `action`: `set` or `clear`; defaults to `set`
- `facility_hours`: required for `set`; structured object with:
  - `status`: one of `verified`, `reference`, `stale`
  - `last_verified_at`, `last_verified_by`, `source`, `note`
  - `weekly`: weekday keys `mon` through `sun`; each day is a list of
    intervals with 24-hour `open` and `close` strings
  - `exceptions`: `date` or `nth_weekday` rules with their own interval lists
- `source`: string such as `ops_dashboard_site_detail` or `manual_review`

### Runtime behavior notes

- resolves `site_id` through the site registry and mutates
  `location_<site_id>` through canonical CouchDB read-modify-write
- `set` replaces `facility_hours`; `clear` removes the field
- verification metadata is operator-provided; storing hours never scrapes or
  imports facts from a URL
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-06-26T14-30-00Z__phn-facility-hours",
  "job_type": "set_site_hours",
  "payload": {
    "site_id": "600",
    "actor": "Greg",
    "action": "set",
    "source": "ops_dashboard_site_detail",
    "facility_hours": {
      "status": "verified",
      "last_verified_at": "2026-06-26",
      "last_verified_by": "Greg",
      "source": "operator_verified",
      "note": "Public-safe synthetic example.",
      "weekly": {
        "mon": [{"open": "08:30", "close": "17:00"}],
        "tue": [{"open": "08:30", "close": "17:00"}],
        "wed": [{"open": "08:30", "close": "17:00"}],
        "thu": [{"open": "08:30", "close": "17:00"}],
        "fri": [{"open": "08:30", "close": "15:00"}],
        "sat": [],
        "sun": []
      },
      "exceptions": [
        {
          "rule": "nth_weekday",
          "weekday": "tue",
          "ordinals": [2, 4],
          "hours": [{"open": "10:00", "close": "19:00"}],
          "note": "Second and fourth Tuesday"
        }
      ]
    }
  }
}
```

## 13c. `set_site_operational_calendar`

Use when an operator needs to upsert or remove one named academic/client
operational calendar on an existing canonical site. This contract is separate
from `facility_hours`, B&T service schedules, billing notes, and location prose.

### Required payload fields

- `site_id`: canonical site name, alias, or site ID string
- `action`: exactly `upsert` or `remove`
- `calendar_id`: lowercase stable slug using letters, digits, hyphens, or
  underscores
- `actor`: nonblank operator/person string

### Optional payload fields

- `source`: string queue-authoring provenance
- `calendar`: required for `upsert` and forbidden for `remove`

The payload rejects all other keys. For `upsert`, the normalized
`calendar.calendar_id` must equal the payload `calendar_id`.

### Complete calendar shape

A calendar rejects unknown keys and requires:

- `schema_version`: integer `1` exactly (a boolean is not accepted)
- `calendar_id`, `label`, `timezone`, `status`, `valid_from`,
  `valid_through`, `last_verified_at`, `last_verified_by`, `source`, and
  `events`
- `timezone`: an installed IANA timezone such as `America/New_York`
- `status`: `verified`, `reference`, or `stale`
- `valid_from` and `valid_through`: strict real `YYYY-MM-DD` dates, with
  `valid_through` on or after `valid_from`
- `last_verified_at`: a strict real timezone-aware ISO datetime
- optional `note`: source ambiguity or undated source information preserved as
  text, without converting it into an operational fact

`source` is a strict object with nonblank `kind` and `title`, a strict real
timezone-aware ISO `retrieved_at` datetime, and at least one of `page_url` or
`document_url`. `retrieved_at` may not be after `last_verified_at`. Source URLs
must be absolute HTTP(S), may not contain credentials or fragments, and are
provenance only: storing one does not promise scraping, refreshes, or automatic
monitoring.

Each `events` entry rejects unknown keys and requires:

- `event_id`: a unique lowercase stable slug within the calendar
- `start_date`, `end_date`: strict real `YYYY-MM-DD`, ordered and fully inside
  the calendar coverage dates
- `kind`: one of `first_student_day`, `final_student_day`, `no_student_day`,
  `school_break`, `teacher_in_service`, `early_dismissal`,
  `holiday_dismissal`, `snow_makeup_reserved`,
  `flexible_instruction_reserved`, or `informational`
- `label`
- `student_status`: `in_session`, `no_students`, `early_dismissal`, or
  `unknown`
- `facility_status`: `open`, `closed`, or `unknown`
- `bt_service_impact`: `normal`, `no_service`, `modified`, `confirm`, or
  `unknown`
- optional `dismissal_time`: strict local 24-hour `HH:MM`
- optional `note`: text

Student schedule, facility status, and B&T service impact are independent
facts. In particular, `student_status: "no_students"` never implies
`bt_service_impact: "no_service"`; authors must record each fact from evidence
or use `unknown`/`confirm`. Expired historical calendars remain stored unless
an explicit `remove` job targets them.

### Runtime behavior notes

- resolves `site_id` through the site registry and mutates
  `location_<site_id>` through canonical CouchDB read-modify-write
- `upsert` replaces the matching `calendar_id` in place or appends a new
  calendar; unrelated entries and location fields are preserved
- `remove` drops only the matching `calendar_id`
- reprocessing the same job is idempotent via the `btq_job_ids` marker
- dry-run reports the resolved target and performs no canonical read or write

### Valid upsert example

```json
{
  "job_id": "2026-07-30T14-00-00Z__demo-operational-calendar",
  "job_type": "set_site_operational_calendar",
  "payload": {
    "site_id": "demo-site-01",
    "action": "upsert",
    "calendar_id": "demo-2026-2027",
    "actor": "Demo Operator",
    "source": "manual_review",
    "calendar": {
      "schema_version": 1,
      "calendar_id": "demo-2026-2027",
      "label": "Demo 2026-2027 operational calendar",
      "timezone": "America/New_York",
      "status": "verified",
      "valid_from": "2026-08-01",
      "valid_through": "2027-06-30",
      "last_verified_at": "2026-07-30T13:45:00-04:00",
      "last_verified_by": "Demo Operator",
      "source": {
        "kind": "client_document",
        "title": "Public-safe synthetic calendar",
        "retrieved_at": "2026-07-30T13:30:00-04:00",
        "document_url": "https://example.com/demo-calendar.pdf"
      },
      "events": [
        {
          "event_id": "first-student-day",
          "start_date": "2026-08-24",
          "end_date": "2026-08-24",
          "kind": "first_student_day",
          "label": "First student day",
          "student_status": "in_session",
          "facility_status": "open",
          "bt_service_impact": "normal"
        },
        {
          "event_id": "autumn-in-service",
          "start_date": "2026-10-12",
          "end_date": "2026-10-12",
          "kind": "teacher_in_service",
          "label": "Teacher in-service day",
          "student_status": "no_students",
          "facility_status": "unknown",
          "bt_service_impact": "confirm",
          "note": "Student status does not determine service impact."
        }
      ],
      "note": "Synthetic authoring example only."
    }
  }
}
```

### Valid remove example

```json
{
  "job_id": "2027-07-01T12-00-00Z__remove-demo-operational-calendar",
  "job_type": "set_site_operational_calendar",
  "payload": {
    "site_id": "demo-site-01",
    "action": "remove",
    "calendar_id": "demo-2026-2027",
    "actor": "Demo Operator",
    "source": "manual_review"
  }
}
```

## 13d. `set_contact`

Use when:

- an operator has reviewed a structured account or site contact
- the contact should be stored on the canonical `account` or `location` doc as
  first-class structured metadata
- the action is to upsert or remove one contact by stable contact `id`

Do not use when:

- the contact is raw intake, unresolved interpretation, or unreviewed AI output
- the action should mutate legacy `customer_*` fields
- the target account or site does not already exist canonically
- the intent is to seed contacts directly without an approved queue job

### Required payload fields

- `action`: one of `upsert`, `remove`
- `target`: object with:
  - `type`: one of `account`, `site`
  - `id`: account canonical id/name/alias or site canonical name/alias/site ID
- `actor`: operator/person string
- `contact`: object; for `remove`, only `id` is allowed

### Required `contact` fields for `upsert`

- `id`: stable contact ID string
- `name`: non-empty display name
- `role`: one of `account_escalation`, `site_contact`, `access_contact`,
  `practice_manager`, `facilities`, `billing`, `safety`, `regional_manager`,
  `client_admin`, `emergency_access`, `other`
- `scope`: one of `account`, `site`; must match `target.type`
- `source`: non-empty provenance string

At least one of `phone`, `email`, or non-empty `notes` is required for
`upsert`.

### Optional payload fields

- `source`: job-level source string such as `manual_review` or
  `ops_dashboard_site_detail`

### Optional `contact` fields for `upsert`

- `title`, `phone`, `email`, `source_date`, `notes`
- `source_date` must be `YYYY-MM-DD` when present
- `email` must be simple email-shaped when present
- `phone` is stored as a non-empty string when present; it is not reformatted

### Runtime behavior notes

- `site` targets resolve through the site registry and mutate
  `location_<site_id>.site_contacts`
- `account` targets accept a canonical `account_<slug>` id or an unambiguous
  account name/alias and mutate `account_<slug>.account_contacts`
- the target document must already exist; `set_contact` never creates accounts
  or locations
- `upsert` replaces an existing contact with the same `id` and never creates
  duplicates; `remove` is a no-op when the contact is absent
- `customer_*` fields are neither read nor written by this job
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-07-07T14-30-00Z__phn-contact",
  "job_type": "set_contact",
  "payload": {
    "action": "upsert",
    "target": {"type": "account", "id": "account_phn"},
    "actor": "Greg",
    "source": "manual_review",
    "contact": {
      "id": "contact_public_safe",
      "name": "Public Safe Contact",
      "title": "Operations Manager",
      "phone": "555-0100",
      "email": "contact@example.com",
      "role": "account_escalation",
      "scope": "account",
      "source": "operator_verified",
      "source_date": "2026-07-07",
      "notes": "Public-safe synthetic example."
    }
  }
}
```

## 14. `log_personnel_event`

Use when:

- a reviewed personnel/HR event needs a structured canonical record
- the event concerns attendance, performance, accommodation, discipline,
  recognition, or an on-the-job incident
- the record should be filterable by employee, event type, severity, or status
  instead of buried in a People note paragraph

Do not use when:

- the note is raw intake, unresolved interpretation, or a general manager note
- the action should merely append non-event context to a known note
- the event is actually a site issue, supply need, or equipment request
- a status-transition job is expected; personnel-event transition jobs do not
  exist yet

### Required payload fields

- `employee`: employee/person string
- `event_type`: one of `attendance`, `performance`, `accommodation`,
  `disciplinary`, `recognition`, `incident`, `other`
- `summary`: non-empty string
- `occurred_at`: ISO datetime string
- `reported_by`: employee/person string

### Optional payload fields

- `severity`: one of `info`, `concern`, `verbal_warning`, `written_warning`,
  `final_warning`, `separation`
- `status`: one of `open`, `monitoring`, `resolved`; defaults to `open`
- `related_site`: site ID, site name, or other site reference string
- `source`: string such as `field_capture` or `daily_log`
- `notes`: string
- `client_notified`, `client_notified_at`, `client_notified_by`,
  `client_notified_method`, `client_notified_note`
- `resolution_trigger`, `resolved_at`, `resolution_summary`
- `related_capture_ids`, `related_candidate_ids`, `related_media`,
  `source_artifacts`: lists of strings
- `event_id`: explicit stable ID when updating a known personnel event

### Runtime behavior notes

- writes a canonical `personnel_event` document keyed by `event_id`, linked to
  the resolved `employee` by `person_id`
- author `employee` in the **First Last** form per the Identity Rules section
  above (e.g. `"Marcus Tate"`). The runtime normalizes both
  `"Marcus Tate"` and `"Tate, Marcus"` to the same person — so legacy
  `"Last, First"` strings encountered in existing data still resolve correctly,
  but new jobs should use the canonical First Last form
- normalization happens before `event_id` is hashed, so the two name forms
  produce the same `event_id` — re-authoring the same event from either
  string updates the same record rather than creating a sibling
- the employee document is never moved or rewritten by this job
- derives a deterministic `event_id` from employee, event type, occurrence
  time, and reporter unless `event_id` is supplied
- reprocessing the same job does not duplicate the event record
- a later job with the same explicit `event_id` updates the same event and
  preserves `created_at`, `event_id`, and `btq_job_ids` history

### Valid example

```json
{
  "job_id": "2026-05-18T09-00-00Z__tate-late-summit-wire",
  "job_type": "log_personnel_event",
  "payload": {
    "employee": "Marcus Tate",
    "event_type": "attendance",
    "severity": "concern",
    "status": "open",
    "summary": "Marcus did not call or show for the Summit Wire opening shift; site was uncovered until midshift coverage arrived.",
    "occurred_at": "2026-05-18T05:30:00-04:00",
    "reported_by": "Jordan",
    "related_site": "7050",
    "source": "field_capture",
    "notes": "First documented attendance event. Verbal warning expected; awaiting Marcus callback.",
    "client_notified": false,
    "resolution_trigger": "Marcus returns to schedule and the next two scheduled shifts are covered without incident."
  }
}
```

## 15. `log_availability_constraint`

Use when:

- a reviewed human report says an employee is unavailable on a specific date
- a reviewed human report gives an employee's last working day
- the fact must be filterable by person and, when known, by related site

Do not use when:

- the source text or human reporter is missing
- the note is raw intake or unresolved model interpretation
- the action should change employee status, status date, assignment, or
  schedule; this job only records the availability fact
- the fact is a backward-looking attendance, performance, disciplinary,
  recognition, or incident event; use `log_personnel_event`

### Required payload fields

- `employee`: employee/person string
- `constraint_type`: one of `unavailable_date`, `last_working_day`
- `date`: `YYYY-MM-DD`
- `reported_by`: non-empty human reporter string
- `source_text`: non-empty verbatim human report text

### Optional payload fields

- `related_site`: site ID, site name, or other site reference string
- `reported_at`: ISO timestamp string
- `event_id`: explicit stable ID when updating a known availability constraint

### Runtime behavior notes

- writes a canonical `availability_constraint` document keyed by the canonical
  person, `constraint_type`, and `date`
- derives a deterministic ID from `{person, constraint_type, date}` unless
  `event_id` is supplied; `reported_by` and `reported_at` are provenance, not
  identity
- requires human provenance: both `reported_by` and the verbatim `source_text`
  must be present
- reprocessing the same person/kind/date updates the same record and preserves
  `created_at`
- `last_working_day` writes its own dated document and does not change employee
  `status`, `status_date`, `job`, `additional_jobs`, or schedule fields
- `related_site` is optional context and does not make this a site-routability
  gated job

### Valid examples

```json
{
  "job_id": "2026-06-11T14-00-00Z__forsythe-unavailable-2026-06-24",
  "job_type": "log_availability_constraint",
  "payload": {
    "employee": "forsythe_adriana",
    "constraint_type": "unavailable_date",
    "date": "2026-06-24",
    "reported_by": "Adriana Forsythe",
    "source_text": "I cannot work on June 24.",
    "related_site": "789",
    "reported_at": "2026-06-11T14:00:00-04:00"
  }
}
```

```json
{
  "job_id": "2026-06-11T14-05-00Z__forsythe-last-working-day",
  "job_type": "log_availability_constraint",
  "payload": {
    "employee": "forsythe_adriana",
    "constraint_type": "last_working_day",
    "date": "2026-08-14",
    "reported_by": "Adriana Forsythe",
    "source_text": "My last working day will be August 14.",
    "related_site": "789"
  }
}
```

## 16. `set_entity_status`

Use when:

- a reviewed operator action should flip a site or employee active state
- the entity already exists and must remain preserved
- the change should be reversible and represented as structured status, not a
  deletion or free-form note

Do not use when:

- the target entity cannot be resolved
- the request is raw intake or unreviewed model output
- a personnel event history entry is also needed; author
  `log_personnel_event` separately

### Required payload fields

- `entity_type`: one of `site`, `employee`
- `entity_id`: site ID string for sites; employee/person string for employees
- `status`: one of `active`, `inactive`
- `reason`: non-empty review rationale
- `source`: non-empty source string, such as `voice_memo` or `manual_review`

### Optional payload fields

- `observed_at`: `YYYY-MM-DD`
- `details`: string

### Runtime behavior notes

- site jobs update canonical `location_<site_id>` with boolean `active`
- employee jobs resolve the canonical `employee` document, require `person_id`,
  and patch canonical `employee_<person_id>` with string `status`
- records are preserved; the job never deletes locations, people, schedules, or
  personnel event history
- reprocessing the same job is idempotent via the `btq_job_ids` marker

### Valid example

```json
{
  "job_id": "2026-06-01T15-00-00Z__western-gas-inactive",
  "job_type": "set_entity_status",
  "payload": {
    "entity_type": "site",
    "entity_id": "7030",
    "status": "inactive",
    "reason": "Reviewed voice memo says Western Gas is no longer an account.",
    "source": "voice_memo",
    "observed_at": "2026-06-01",
    "details": "Operator selected Western Gas in the voice memo dashboard."
  }
}
```

## 17. `close_recruiting`

Use when:

- closing a recruiting effort that was previously opened via
  `trigger_recruiting`
- the closure outcome is known: `filled`, `cancelled`, `withdrawn`, or
  `superseded`
- the site is known

Do not use when:

- the recruiting effort is still active; use `trigger_recruiting` to add a new
  trigger
- the site is unknown
- this is an out-of-band employee placement unrelated to any open recruiting;
  handle it outside the queue

### Required payload fields

- `site`: string
- `outcome`: one of `filled`, `cancelled`, `withdrawn`, `superseded`

### Conditionally required payload fields

- `filled_by`: string, required when `outcome=filled`

### Optional payload fields used by runtime

- `date`: string, expected as `YYYY-MM-DD`
- `recruiting_trigger_id`: string, links to a specific trigger entry for
  information only
- `notes`: free-text closure rationale

### Runtime behavior notes

- the runtime appends a closure entry to the resolved `location` document's
  recruiting-closed history
- when `outcome=filled`, the runtime also appends a placement note to the
  resolved `employee` document's schedule-changes history, symmetric with
  `remove_from_schedule`
- trigger entries from `trigger_recruiting` are not mutated; the
  recruiting-history section remains append-only and a closure is its own
  appended entry
- idempotent on `job_id`, using the same `has_job_been_applied` pattern as
  other job types
- if no `date` is supplied, the runtime uses the current UTC date

### Valid example

```json
{
  "job_id": "2026-05-26T16-03-02Z__close-recruiting-apex-7080",
  "job_type": "close_recruiting",
  "payload": {
    "site": "Apex Powdered Metals",
    "outcome": "filled",
    "filled_by": "David Pearson",
    "date": "2026-05-26",
    "notes": "Closes Dana Reed indefinite pregnancy-illness coverage gap; Pearson permanent M-F."
  }
}
```

## Status-transition jobs

These jobs advance an existing supply-need, equipment-request, or site-issue
canonical document through its status lifecycle. They require the canonical
document already exists in `btq_vault` and the current status matches the valid
source set. Status transitions are idempotent: re-applying the same `job_id` is
a no-op.

All status-transition jobs reject unknown payload fields. They move to the
failed queue if the target file cannot be found, cannot be parsed, or has a
current status outside the valid source statuses below.

### Supply transition payload

Required payload fields for all `mark_supply_*` jobs:

- `supply_id`: string
- `actor`: string

Optional payload fields:

- `note`: string
- `occurred_at`: ISO datetime string; defaults to the processor runtime when
  absent

Supply transitions:

| Job type | Valid source statuses | Target status | Lifecycle fields set |
| --- | --- | --- | --- |
| `mark_supply_ordered` | `open` | `ordered` | `ordered_at`, `ordered_by`, optional `ordered_note` |
| `mark_supply_delivered` | `ordered` | `delivered` | `delivered_at`, `delivered_by`, optional `delivered_note` |
| `mark_supply_stocked` | `delivered` | `stocked` | `stocked_at`, `stocked_by`, optional `stocked_note` |
| `mark_supply_no_action_needed` | `open`, `ordered`, `delivered` | `no_action_needed` | no transition-specific lifecycle field; optional `note` is merged into `notes` |

Example:

```json
{
  "job_id": "2026-05-08T18-00-00Z__mark-supply-ordered",
  "job_type": "mark_supply_ordered",
  "payload": {
    "supply_id": "sup_summit_brightwash",
    "actor": "Jordan",
    "note": "Ordered from Staples.",
    "occurred_at": "2026-05-08T18:00:00+00:00"
  }
}
```

### Equipment transition payload

Required payload fields for all `mark_equipment_*` jobs:

- `equipment_id`: string
- `actor`: string

Optional payload fields:

- `note`: string
- `occurred_at`: ISO datetime string; defaults to the processor runtime when
  absent

Equipment transitions:

| Job type | Valid source statuses | Target status | Lifecycle fields set |
| --- | --- | --- | --- |
| `mark_equipment_approved` | `open` | `approved` | `approved_at`, `approved_by`, optional `approval_note` |
| `mark_equipment_denied` | `open`, `approved` | `denied` | `denied_at`, `denied_by`, optional `denial_note` |
| `mark_equipment_ordered` | `approved` | `ordered` | `ordered_at`, `ordered_by`, optional `ordered_note` |
| `mark_equipment_provided` | `ordered` | `provided` | `provided_at`, `provided_by`, optional `provided_note` |
| `mark_equipment_no_action_needed` | `open`, `approved`, `ordered` | `no_action_needed` | no transition-specific lifecycle field; optional `note` is merged into `notes` |

Example:

```json
{
  "job_id": "2026-05-08T18-00-00Z__mark-equipment-approved",
  "job_type": "mark_equipment_approved",
  "payload": {
    "equipment_id": "eqr_summit_vacuum",
    "actor": "Jordan",
    "note": "Approved replacement vacuum.",
    "occurred_at": "2026-05-08T18:00:00+00:00"
  }
}
```

### Issue transition payload

Required payload fields for all `mark_issue_*` jobs:

- `issue_id`: string
- `actor`: string

Optional payload fields:

- `note`: string
- `occurred_at`: ISO datetime string; defaults to the processor runtime when
  absent

Issue transitions:

| Job type | Valid source statuses | Target status | Lifecycle fields set |
| --- | --- | --- | --- |
| `mark_issue_monitoring` | `open` | `monitoring` | `monitoring_at`, `monitoring_by`, optional `monitoring_note` |
| `mark_issue_resolved` | `open`, `monitoring` | `resolved` | `resolved_at`, `resolved_by`, optional `resolved_note` |
| `mark_issue_open` | `monitoring`, `resolved` | `open` | `open_at`, `open_by`, optional `open_note` |

Example:

```json
{
  "job_id": "2026-05-08T18-00-00Z__mark-issue-resolved",
  "job_type": "mark_issue_resolved",
  "payload": {
    "issue_id": "iss_summit_drain",
    "actor": "Jordan",
    "note": "Maintenance confirmed the drain is clear.",
    "occurred_at": "2026-05-08T18:00:00+00:00"
  }
}
```

#### `mark_record_archived` / `mark_record_unarchived`

Required payload fields for `mark_record_archived` and
`mark_record_unarchived`:

- `record_type`: one of `site_issue`, `supply_need`, `equipment_request`, `visit`
- `record_id`: string; the canonical record id without the type prefix, or the
  full canonical `_id`. Visits must use the full canonical `_id` beginning with
  `visit_`; there is no short visit id form.
- `actor`: string

Optional payload fields:

- `note`: string

Archive transitions:

| Job type | Effect |
| --- | --- |
| `mark_record_archived` | Sets `archived=true`, `archived_at`, and `archived_by` on the canonical record. |
| `mark_record_unarchived` | Sets `archived=false` and clears `archived_at`, `archived_by`, and `archive_note`. |

Archiving a visit removes it from Visit/QC coverage while preserving the full
canonical visit document, including `btq_job_ids`, evidence, and merged evidence
for audit. Use `note` to record why the visit is being retired, for example
`superseded duplicate of visit_705_2026-06-23_abcd1234`.

Example:

```json
{
  "job_id": "2026-06-10T14-00-00Z__mark-record-archived",
  "job_type": "mark_record_archived",
  "payload": {
    "record_type": "site_issue",
    "record_id": "iss_summit_drain",
    "actor": "Jordan",
    "note": "Duplicate issue."
  }
}
```

#### `edit_record_fields`

Edits allowlisted fields on a canonical issue / supply / equipment record.

Required payload fields:

- `record_type`: one of `site_issue`, `supply_need`, `equipment_request`
- `record_id`: string; the canonical record id without the type prefix, or the
  full canonical `_id`
- `fields`: object of allowlisted field updates for the record type (any key not
  allowlisted below is rejected)
- `actor`: string

Allowlisted editable fields by `record_type`:

| `record_type` | Editable fields |
| --- | --- |
| `site_issue` | `site_id`, `title`, `summary`, `priority`, `category`, `resolution_trigger` |
| `supply_need` | `site_id`, `item_name`, `quantity_needed`, `urgency`, `notes` |
| `equipment_request` | `site_id`, `equipment_name`, `reason`, `priority`, `notes` |

Applies the allowlisted fields to the canonical record, sets `updated_at` and
`edited_by`, and re-derives `site_name` when `site_id` changes. Never modifies
`status`, `archived`, or audit fields.

Example:

```json
{
  "job_id": "2026-06-10T14-30-00Z__edit-record-fields",
  "job_type": "edit_record_fields",
  "payload": {
    "record_type": "site_issue",
    "record_id": "iss_summit_drain",
    "fields": { "site_id": "1200", "summary": "Updated summary." },
    "actor": "Greg"
  }
}
```

## Pipeline-internal job types

These job types are **emitted by the automated pipeline** — Whisper audio
transcription and supply-email parsing — not authored by hand in a session.
They are listed here for completeness: they are valid `ALLOWED_JOB_TYPES`
entries, the drift guard checks them, and an author may occasionally need to
recognize one. You should not normally hand-author them.

### `parse_supply_email`

Emitted when a supply-vendor email is captured for parsing.

#### Required payload fields

- `html_path`: string — path to the captured email HTML
- `subject`: string
- `source_email_date`: ISO datetime string

### `personal_journal_entry`

Emitted by the audio pipeline for a personal, non-operational journal dictation.

#### Required payload fields

- `date`: `YYYY-MM-DD`
- `timestamp`: ISO datetime string
- `audio_file`: string
- `body`: string — the transcript body
- `raw_transcript_path`: string

### `voice_memo_note`

Emitted by the audio pipeline for a captured operational voice memo.

#### Required payload fields

- `capture_id`: string
- `timestamp`: ISO datetime string
- `audio_file`: string
- `raw_transcript_path`: string
- `transcript_text`: string

#### Optional payload fields

- `routing_flag`: string
- `site_id` or `site`: string
- `note`: string
- `geolocation`: string or object
- `employees`: list

## Ambiguity Handling

When the input is unclear:

- do not guess the site
- do not guess the employee
- do not invent structured actions from weak signals
- do not use aliases or site IDs in place of canonical site strings
- prefer preserving the information over forcing a wrong job

Preferred fallbacks:

1. unresolved unknown capture
2. targeted `reclassify_unknown` after more context exists
3. non-executable planning/state note outside the queue

## Fact Splitting Rules

Split distinct facts into separate jobs when they represent different operational actions.

Good split:

- “Only one employee has the badge, and we still have two open spots.”
- create:
  - one `flag_access_constraint`
  - one `trigger_recruiting`

Do not split when:

- multiple sentences are clearly part of one single visit anchor
- multiple phrases are just details supporting one operational fact

## Known Validation Gaps

These are current runtime realities, not authoring permissions:

- `queue_spec.py` does not currently validate string type or non-empty content for most required fields outside `visit_create`
- `priority` has no enforced enum except in `log_site_issue`
- optional runtime-consumed fields such as `date`, `open_positions`, and `blocking` are not declared in `queue_spec.py`

Author guidance:

- still provide correct string values for required fields
- use `YYYY-MM-DD` if you provide `date`
- do not rely on undocumented fields

## Proposed Future Job Types

The following job types are backlog items only. They are not executable until
they are added to [queue_spec.py](project/queue_spec.py) and
implemented by deterministic queue writer code. Until then, keep these intents
as reviewed non-executable intent or use the current fallback of
`append_to_note` when an operational note must be preserved.

The previous `log_supply_need` and `log_equipment_request` backlog items are
now executable queue job types. Keep future proposals in this section until
they have both queue-spec validation and deterministic writer code.

## When Not To Use The Queue

Do not author a runtime queue job for:

- employee status tracking as a general management concept
- shift-report generation
- opportunity tracking that has no supported runtime job type
- planning notes
- unresolved interpretation that should stay in the cognition/review layer

Those remain non-executable intent unless promoted into the runtime contract.

## Safe Authoring Checklist

Before creating a job, ask:

1. Is there a supported runtime job type for this action?
2. Do I know every required field with enough certainty?
3. Am I using canonical site-name strings rather than site IDs or aliases?
4. Is this a fact, not a guess?
5. Should this be one job or multiple distinct jobs?
6. If uncertain, should I preserve it as unresolved instead?

## Syncing to BTpipeline for AC

This guide is the canonical source of truth. The Cowork-side operator (AC)
reads a mirror at `BTpipeline/specs/QUEUE_JOB_SPEC.md` (in iCloud). When you
update this file, also run:

```bash
scripts/sync_btpipeline_spec.sh
```

That regenerates the iCloud mirror. CI's
`test_all_executable_job_types_have_a_section_in_authoring_guide` test guards
against new job types landing without a corresponding section here.
