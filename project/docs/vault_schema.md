# Entity Schema (`btq_vault` CouchDB database)

This document describes the canonical operational entities the BTQ queue
processor reads and writes. It is not an aspirational schema; it is a
description of what the code stores today.

Canonical operational state lives as typed documents in the `btq_vault`
CouchDB database. CouchDB is the source of truth — there is no Obsidian
markdown projection anymore. Every entity is a JSON document whose `type`
field selects its shape, and whose entity references (`site_id`, `person_id`,
…) make retrieval entity-scoped.

AI bootstrap documents, queue authoring guidance, architecture notes, and agent
instructions are not entities. They are source-controlled repository artifacts
exported to the external iCloud `BTDocs` directory by `./scripts/btq-export-docs`.

## Document Types

The queue processor writes and reads these `btq_vault` document types:

- `location` — a service site/account location, keyed by `site_id`. Carries
  canonical site name, account, operational notes, equipment inventory, and an
  `active` flag.
- `account` — a client account grouping one or more locations.
- `employee` (also written as `person`) — a worker record, keyed by the stable
  `person_id`. Carries name, role, status, primary `job` site, and
  `additional_jobs`.
- `visit` — a same-day site-presence anchor used to link activity to a site and
  date. Carries `visit_key`, site, date, source, confidence, and evidence.
- `visit_gap` — a recorded gap in expected site visits.
- `site_issue` — a structured operational site issue, keyed by `issue_id`.
  Tracks status (`open`, `monitoring`, `resolved`), priority, category, client
  notification (separate from resolution), and resolution metadata.
- `supply_need` — a consumable restock need, keyed by `supply_id`. Status moves
  through `open`, `ordered`, `delivered`, `stocked`, `no_action_needed`.
- `equipment_request` — a durable equipment request, keyed by `equipment_id`.
  Status moves through `open`, `approved`, `ordered`, `provided`, `denied`,
  `no_action_needed`.
- `personnel_event` — an HR/personnel event for an employee (attendance,
  performance, accommodation, disciplinary, recognition, incident), keyed by
  `event_id`.
- `availability_constraint` — a forward-looking human-reported employee
  availability fact, keyed by person, constraint type, and date. Carries
  required provenance (`reported_by` and verbatim `source_text`).
- `journal` — a dated operational journal entry.
- `unknown_capture` — a capture that could not be classified into a supported
  event type, retained for later reclassification.
- `shift_report` — a generated end-of-day operational summary.

## Identity And Retrieval

- Entities are first-class. Every document carries its entity references so an
  index can pull *all* documents for one site or one person — complete and
  exact, not a similarity-search sample.
- Site identity is `site_id` (a string; site/job numbers are identifiers, not
  numbers — always quote them, e.g. `"7050"`).
- Person identity is the stable `person_id`. Names are presentation only and may
  change; `person_id` is the canonical anchor.
- `issue_id`, `supply_id`, `equipment_id`, `event_id`, and availability
  constraint IDs are deterministic from their source facts unless an explicit
  id is supplied, so reprocessing the same queue job updates the same document
  rather than creating a duplicate.

## Site Issue Fields

Issue documents use `type: site_issue` and keep client notification separate
from resolution. Representative fields:

```json
{
  "type": "site_issue",
  "issue_id": "iss_...",
  "site_id": "7050",
  "site": "Summit Wire",
  "account": "Summitsteel",
  "title": "Restroom drain backup and inoperable stall",
  "status": "open",
  "priority": "high",
  "category": "maintenance",
  "source": "field_capture",
  "reported_by": "Tom Walsh",
  "observed_at": "2026-05-06T18:27:03-04:00",
  "created_at": "2026-05-08T20:00:00+00:00",
  "client_notified": true,
  "client_notified_at": "2026-05-08T15:10:00-04:00",
  "client_notified_by": "Jordan",
  "client_notified_method": "email",
  "resolved_at": null,
  "resolution_trigger": "Maintenance confirms the drain is clear and the stall is operable.",
  "resolution_summary": "",
  "related_capture_ids": ["cap-photo-summit-drain"],
  "related_media": ["/media/cap-photo-summit-drain/drain.jpg"],
  "btq_job_ids": ["<computed job id>"]
}
```

Allowed issue status values are `open`, `monitoring`, and `resolved`.
`client_notified` records communication only; it does not mean the issue is
resolved.

## Supply Need Fields

Supply documents use `type: supply_need`. Allowed status values are `open`,
`ordered`, `delivered`, `stocked`, and `no_action_needed`; allowed urgency
values are `low`, `normal`, `high`, and `critical`. Optional order/delivery/
stocked fields (`ordered_at`, `ordered_by`, `delivered_at`, `stocked_at`, …)
are written as the record advances.

## Equipment Request Fields

Equipment documents use `type: equipment_request`. Allowed status values are
`open`, `approved`, `ordered`, `provided`, `denied`, and `no_action_needed`;
allowed priority values are `low`, `normal`, `high`, and `urgent`. Optional
approval/denial/order/provided fields are written as the record advances.

## Personnel Event Fields

Personnel-event documents use `type: personnel_event`. They carry `employee`
(resolved to a `person_id`), `event_type`, `severity`, `status`, `summary`,
`occurred_at`, `reported_by`, optional `related_site`, and client-notification
and resolution fields. The `event_id` is deterministic from employee, event
type, occurrence time, and reporter.

## Availability Constraint Fields

Availability-constraint documents use `type: availability_constraint`. They
carry `person_id`, `constraint_type`, `date`, `reported_by`, and the verbatim
human `source_text`; `related_site` and `reported_at` are optional context.
Allowed `constraint_type` values are `unavailable_date` and
`last_working_day`.

The document ID is deterministic from the canonical person, constraint type,
and `YYYY-MM-DD` date unless an explicit `event_id` is supplied. Reporter and
report time are provenance, not identity, so re-reporting the same person/kind/
date updates the same document and preserves `created_at`.

`last_working_day` is represented as its own dated availability constraint. It
does not change the employee document's `status`, `status_date`, `job`, or
schedule fields; any future status transition must be a separate deterministic
writer job.

## Employee Fields

Employee documents use `type: employee` (legacy) or `type: person`
(writer-created). Representative fields:

- `person_id` — canonical stable identity anchor
- `name`, with `first` / `last` compatibility fields for normal personal names
- `role`
- `status`, `status_date`
- `job` — the primary assigned work site (kept singular during the transition
  because staffing reports and lookups depend on it)
- `additional_jobs` — optional list of secondary assigned work sites

Effective authorized site membership is `job` plus `additional_jobs`, with
duplicates ignored. Legacy `sites: [...]` remains readable as a fallback.

`add_person` creates the canonical employee document and fails safely on
duplicate employee ID or normalized name collision. Keyed `add_person` replay
uses an append-only mutation-key ledger; the same key plus the same payload is a
no-op, the same key plus a different payload fails.

## Location Fields

Location documents use `type: location` and are keyed by `site_id`. The queue
processor only treats a record as a valid location when it carries:

- `type: location`
- either `job: "<site_id>"` or `site_id: "<site_id>"`
- usually `location: <canonical site name>`

`location` records also hold operational notes and the current site equipment
inventory (replaced as a whole by `update_site_equipment`).

Location records may carry first-class reference links in `urls`. Missing
`urls` is equivalent to an empty list. Each entry is deterministic operator
context, not scraped or trusted operational data:

- `url` — required HTTP(S) URL
- `label` — optional human label
- `kind` — one of `official_location_page`, `client_homepage`, `maps`,
  `portal`, `document`, or `other`
- `status` — one of `reference`, `verified`, `stale`, or `deprecated`;
  omitted status defaults to `reference`
- `last_verified_at`, `last_verified_by`, `verification_note` — optional
  operator-provided verification metadata

URL changes are canonical queue mutations via `set_site_url`; dashboard controls
stage that job and never write the `location` document directly. Stored URLs do
not update hours, addresses, access notes, or any other operational field.

Location records may also carry `operational_calendars`, an optional list of
strict calendars keyed by stable `calendar_id`. Missing
`operational_calendars` is equivalent to an empty list. A
`set_site_operational_calendar` queue job upserts one matching entry in place,
appends a new ID, or removes one ID through canonical CouchDB read-modify-write;
it does not rewrite unrelated calendars or location fields. Expired calendars
remain stored as history, and readers determine whether a calendar is stale.

Each calendar has:

- `schema_version`: integer `1`
- `calendar_id`, `label`, and installed IANA `timezone`
- `status`: `verified`, `reference`, or `stale`
- strict `valid_from` and `valid_through` `YYYY-MM-DD` coverage dates
- required strict timezone-aware ISO `last_verified_at` and nonblank
  `last_verified_by` provenance
- `source`: strict `kind` and `title`, a strict timezone-aware ISO
  `retrieved_at` no later than `last_verified_at`, plus at least one absolute
  HTTP(S) `page_url` or `document_url`; URLs cannot contain credentials or
  fragments
- `events`: a list of unique stable `event_id` entries within the coverage
  dates
- optional `note` for source ambiguity or undated source information

Calendar events carry strict `start_date` and `end_date`, a label, and these
independent enum fields:

- `kind`: `first_student_day`, `final_student_day`, `no_student_day`,
  `school_break`, `teacher_in_service`, `early_dismissal`,
  `holiday_dismissal`, `snow_makeup_reserved`,
  `flexible_instruction_reserved`, or `informational`
- `student_status`: `in_session`, `no_students`, `early_dismissal`, or
  `unknown`
- `facility_status`: `open`, `closed`, or `unknown`
- `bt_service_impact`: `normal`, `no_service`, `modified`, `confirm`, or
  `unknown`

An event may also carry strict local `dismissal_time` (`HH:MM`) and a text
`note`. Student schedule, facility status, and B&T service impact are separate
facts: `no_students` must never be interpreted as `no_service`. Source URLs are
provenance, not a promise of scraping, refresh, or automatic monitoring.
`facility_hours`, service schedules, billing fields, and location prose remain
separate contracts and are not changed by operational-calendar jobs.

## Site Routing And The Registry

Runtime site routing is registry-driven, separate from the entity store.

- When `BTQ_COUCHDB_URL` is set, the site registry is read from the `btq_sites`
  CouchDB database; local/dev routing falls back to the checked-in registry in
  [event_pipeline/sites.py](/Users/operator/btq/project/event_pipeline/sites.py)
  only when CouchDB is not configured.
- Each registry entry defines a canonical site name, a string `site_id`, and the
  aliases used by transcript site resolution.
- A canonical `location` document can exist in `btq_vault` and still not route
  until the site is present in the active registry. Adding a location does not
  make it immediately routable unless the registry is also refreshed.

## What The System Writes vs What Humans Maintain

System-written or system-updated documents:

- `journal` entries and `unknown_capture` records
- `location` documents (issues, supplies, equipment, equipment inventory,
  recruiting state appended in place)
- `visit` and `visit_gap` records
- `employee` documents for supported employee jobs

Usually human/operator-seeded via the ops dashboard or registry tooling:

- `location` records and the `btq_sites` registry entries
- `account` records
- `employee` records

The system updates these documents; it does not scaffold a full account/location
tree from scratch. Missing or incorrect seed records will cause some queue jobs
to fail.

## Fragile Areas

Current coupling points implied by code more than enforced by explicit schema
validation:

- `location` documents must carry valid enough fields for site validation
- site routing must stay aligned with the active `btq_sites` registry
- people resolution depends on `person_id` plus name fields
- unknown-capture reclassification depends on the stored capture shape
- journal and visit dates use UTC in the current implementation
