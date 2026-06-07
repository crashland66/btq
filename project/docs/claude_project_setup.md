# BTQ Queue-Authoring Project Setup

Use this document to configure a **scoped** Claude project that authors BTQ queue jobs only — no dev work, no runtime access.

The development machine also holds a git checkout of the BTQ source at `/path/to/btq/`, and a Cowork session there is the active development environment. The deployed runtime install lives at `/path/to/runtime/btq/`. Both exist; they are different surfaces for different work.

This document is for a different Claude project: a queue-authoring-only scope where Claude has no source-repo access and no runtime access. Its role is narrow:

- understand field-operation intent
- create executable queue-job JSON files
- place those JSON files in the iCloud BTpipeline outbox
- never edit the BTQ runtime code
- never write directly into the operational vault as a substitute for a queue job

Validation and execution stay on the runtime install. Dev work happens in a separate Cowork session against the dev checkout and is out of scope for this document.

## Handoff Directory

The queue-authoring assistant should write completed jobs to:

```text
~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/outbox/
```

The BTQ queue watcher on the repository machine stages files from that iCloud outbox into its local runtime queue and processes them.

If Claude cannot write to that directory directly, have it produce one JSON object per job and clearly label the intended filename. A human can then place the files in the outbox.

## Claude Project Instructions

Paste this section into the Claude project instructions.

```text
You are authoring BTQ queue jobs for field operations.

Important boundary:
- This Claude project is intentionally scoped to queue authoring only. You do not have, and should not assume, access to the BTQ source or the BTQ runtime install on any machine.
- You must not run BTQ project Python files.
- Your output is queue-job JSON only, staged to the iCloud outbox when file access is available.

Outbox:
- Write completed executable jobs to:
  ~/Library/Mobile Documents/com~apple~CloudDocs/BTpipeline/outbox/
- Use one JSON file per job.
- Use filenames shaped like:
  YYYY-MM-DDTHH-MM-SSZ__short-descriptive-slug.json
- Do not overwrite an existing outbox file. If a filename already exists, choose a more specific slug or add a numeric suffix.

Core queue rules:
- Only author supported job types listed in the project knowledge.
- Always include top-level job_id, job_type, and payload.
- For add_person, include a stable top-level idempotency_key when possible, such as ehub-567.
- Do not invent job types.
- Do not invent payload fields and assume the runtime will use them.
- Do not put observation or observations fields in queue jobs.
- Split distinct operational facts into separate jobs.
- Prefer unresolved capture or a plain journal append over guessing.
- If the user asks to add, create, onboard, or register a new employee/person, use add_person.
- Do not convert onboarding requests into append_to_note jobs.
- Do not invent vault paths for people records.
- Use canonical site-name strings, not site IDs, aliases, or vault paths.
- Use employee name strings for employee fields.
- If the site or employee is uncertain, do not create a structured job that requires that value.
- Use date values as YYYY-MM-DD.
- Treat queue jobs as executable requests, not notes, plans, or speculation.

When unsure:
- Ask for clarification if the missing fact is required.
- If clarification is not available, preserve the information as a non-structured note only when a supported append_to_note target is known.
- Otherwise produce a human-readable "not executable" note explaining what is missing.
- Site-resolved audio memos that do not map to a more specific deterministic
  event may be preserved as `type: site_audio_memo` appends to the known site
  note. This is preservation of observational context, not person/entity
  creation.

Never do these:
- Do not edit files under People, Accounts, Journal, or other vault folders directly as a replacement for a queue job.
- Do not create jobs for general planning, employee status tracking, opportunity tracking, or unsupported business logic.
- Do not use numeric site IDs as the site field.
- Do not claim a job was processed. The repository-side queue processor determines that.
```

## Supported Executable Job Types

The runtime currently accepts these job types:

- `append_to_note`
- `trigger_recruiting`
- `remove_from_schedule`
- `flag_access_constraint`
- `flag_retention_risk`
- `add_person`
- `reclassify_unknown`
- `visit_create`
- `parse_supply_email`
- `personal_journal_entry`
- `photo_capture`

For Claude-authored operational jobs, the safest common set is:

- `append_to_note`
- `trigger_recruiting`
- `remove_from_schedule`
- `flag_access_constraint`
- `flag_retention_risk`
- `add_person`
- `reclassify_unknown`
- `visit_create`

Avoid `personal_journal_entry`, `parse_supply_email`, and `photo_capture` unless the input exactly matches those contracts.

## Minimal Queue Contract

Every job is a JSON object:

```json
{
  "job_id": "2026-05-01T14-30-00Z__short-descriptive-slug",
  "job_type": "append_to_note",
  "payload": {}
}
```

`job_id` should match the filename stem when practical.

## Job Type Reference

### append_to_note

Use when the exact vault-relative target path is known and the action is a plain note append.

Do not use for onboarding, person creation, new-hire registration, or employee entity creation. Use `add_person` for those.

Required payload fields:

- `path`: string
- `content`: string
- `destination`: one of `missed`, `journal_unknown`, `site_note`, `journal`, `employee_note`

Example:

```json
{
  "job_id": "2026-05-01T14-30-00Z__journal-note-glenco-orientation",
  "job_type": "append_to_note",
  "payload": {
    "path": "Journal/2026-05-01.md",
    "content": "Chase Lynch started orientation today at Glenwood High School.",
    "destination": "journal"
  }
}
```

### add_person

Use when adding, creating, onboarding, or registering a new employee/person.

Required payload fields:

- `name`: full display name string
- `role`: role/title string

Optional payload fields:

- `employee_id`: numeric string
- `employment_type`: string such as `part_time`
- `status`: string such as `active`
- `job`: primary assigned work-site id
- `additional_jobs`: list of secondary assigned work-site ids
- `assignments`: list of assignment objects with `job`, `account`, `location`, and `shift`
- `contact`: object with `phone` and `email`, each string or null
- `metadata`: object with `source`

Optional top-level field:

- `idempotency_key`: strongly recommended, for example `ehub-567`

Do not include any vault path. The BTQ writer creates `People/<Name>.md` internally and generates the permanent `person_id`.

Example:

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

### trigger_recruiting

Use for a site-level staffing shortage or open-position signal.

Required payload fields:

- `site`: canonical site name string
- `priority`: string
- `details`: string

Optional runtime-consumed fields:

- `date`: `YYYY-MM-DD`
- `open_positions`: integer or stringifiable value

Example:

```json
{
  "job_id": "2026-05-01T14-35-00Z__recruiting-western-gas-openings",
  "job_type": "trigger_recruiting",
  "payload": {
    "site": "Western Gas Transmission",
    "priority": "high",
    "details": "Two openings remain on site.",
    "date": "2026-05-01",
    "open_positions": 2
  }
}
```

### remove_from_schedule

Use when an employee has resigned or should be removed from schedule coverage, and both employee and site are known.

Required payload fields:

- `employee`: employee name string
- `site`: canonical site name string

Optional runtime-consumed fields:

- `date`: `YYYY-MM-DD`

Example:

```json
{
  "job_id": "2026-05-01T14-40-00Z__remove-peter-nash-western-gas",
  "job_type": "remove_from_schedule",
  "payload": {
    "employee": "Peter Nash",
    "site": "Western Gas Transmission",
    "date": "2026-05-01"
  }
}
```

### flag_access_constraint

Use for keys, badges, entry dependencies, locked areas, or other site access blockers.

Required payload fields:

- `site`: canonical site name string
- `details`: string

Optional runtime-consumed fields:

- `date`: `YYYY-MM-DD`
- `blocking`: boolean or truthy/falsy value

Example:

```json
{
  "job_id": "2026-05-01T14-45-00Z__access-western-gas-main-badge",
  "job_type": "flag_access_constraint",
  "payload": {
    "site": "Western Gas Transmission",
    "details": "Only one employee has the badge that can open the main entry.",
    "date": "2026-05-01",
    "blocking": true
  }
}
```

### flag_retention_risk

Use for a site-linked retention concern for a named employee.

Required payload fields:

- `employee`: employee name string
- `site`: canonical site name string
- `details`: string

Optional runtime-consumed fields:

- `date`: `YYYY-MM-DD`

Example:

```json
{
  "job_id": "2026-05-01T14-50-00Z__retention-peter-nash-western-gas",
  "job_type": "flag_retention_risk",
  "payload": {
    "employee": "Peter Nash",
    "site": "Western Gas Transmission",
    "details": "May leave if the evening workload stays the same.",
    "date": "2026-05-01"
  }
}
```

### reclassify_unknown

Use when a specific unknown journal file should be rescanned by the runtime.

Required payload fields:

- `path`: vault-relative path to the unknown journal file

Example:

```json
{
  "job_id": "2026-05-01T14-55-00Z__reclassify-unknown-2026-05-01",
  "job_type": "reclassify_unknown",
  "payload": {
    "path": "Journal/2026-05-01-unknown.md"
  }
}
```

### visit_create

Use when physical site presence is clearly implied and the site is known.

Do not use for phone calls, planned future visits, or uncertain evidence.

Required payload fields:

- `site`: canonical site name string
- `confidence`: `high` or `medium`
- `source`: non-empty string
- `evidence`: non-empty string

Example:

```json
{
  "job_id": "2026-05-01T15-00-00Z__visit-western-gas",
  "job_type": "visit_create",
  "payload": {
    "site": "Western Gas Transmission",
    "confidence": "high",
    "source": "claude_project",
    "evidence": "I was at Western Gas Transmission this afternoon."
  }
}
```

## Person Creation and File Movement

Current runtime note:

- `add_person` is the supported executable job type for creating People notes.
- There is no supported executable job type named `create_person`, `move_file`, or `move_files`.
- Claude must not invent file-move jobs.
- If the desired action is to move vault files, Claude should produce a non-executable request for human review unless a future BTQ queue contract adds that job type.

Recommended non-executable format:

```text
Not executable as a BTQ queue job yet.

Requested action:
- Move file: old/path.md -> new/path.md

Missing runtime support:
- No supported queue job type currently moves arbitrary vault files.
```

## Recommended Claude Project Knowledge Files

Add these as project knowledge when possible:

1. This setup document.
2. A copy of the current queue authoring guide.
3. A short list of canonical site names.
4. A short list of known employee names, if you want Claude to resolve people without guessing.

Keep site and employee lists current. Claude should treat missing names as uncertainty, not an invitation to invent records.

## Repository-Side Processing

On the BTQ repository machine, process outbox jobs with the existing wrappers:

```bash
cd /Users/operator/btq
./scripts/btq-dry
./scripts/btq-run
```

The queue watcher may also process outbox jobs automatically if installed and running.
