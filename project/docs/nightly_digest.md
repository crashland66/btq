# Nightly Digest

This document defines a practical nightly review artifact for the current BT Pipeline implementation.

It is based on files the system already produces today. It does not assume new extraction logic, new queue job types, or automatic contract expansion.

## Purpose

The current system spreads one day of activity across multiple outputs:

- validated events in `local/events_valid/`
- failed events in `local/events_failed/`
- processed queue jobs in `<runtime_root>/processed/`
- failed queue jobs in `<runtime_root>/failed/`
- canonical CouchDB `btq_vault` entity/evidence changes
- optional Markdown projections under `Journal/`, `Accounts/.../about.md`,
  `People/`, and `Visits/`
- unresolved captures in CouchDB unknown-capture state, with
  `Journal/YYYY-MM-DD-unknown.md` available as projection output

A nightly digest should gather those artifacts into one operator-facing summary so the nightly process can answer:

1. What happened today?
2. What did the system execute?
3. What did not get captured cleanly?
4. Where is the current queue contract awkward or incomplete?

## Current Source Files

Use these as the source of truth for a nightly digest.

### Event artifacts

- `local/events_valid/*.json`
- `local/events_failed/*.json`

These show what the event pipeline believed was a valid operational fact and what it rejected.

### Queue artifacts

- `<runtime_root>/processed/*.json`
- `<runtime_root>/failed/*.json`
- `<runtime_root>/logs/queue_processor/run-*.log`

These show what the runtime actually executed, skipped, or failed.

### Vault outputs

- `Journal/YYYY-MM-DD.md`
- `Journal/YYYY-MM-DD-unknown.md`
- `Accounts/<Account>/Locations/<Site>/about.md`
- `Accounts/<Account>/Locations/<Site>/Visits/YYYY-MM-DD.md`
- `People/*.md`

These show the final operator-facing state after execution.

## Recommended Digest Shape

One nightly digest should be organized into the following sections.

## 1. Daily Summary

Short operator summary of the day:

- number of audio files processed
- number of valid events created
- number of queue jobs processed
- number of queue job failures
- number of unresolved unknown captures remaining

This section is a rollup, not a narrative.

## 2. Events Created Today

List validated events by type and destination impact.

Suggested fields per entry:

- `event_id`
- `type`
- `site`
- `employee` if present
- `details`
- `timestamp`
- whether the event mapped to a queue job

Purpose:

- show the perception layer output for the day
- show what the extractor believed it saw

## 3. Jobs Executed Today

List processed runtime jobs.

Suggested fields per entry:

- `job_id`
- `job_type`
- destination file or target entity
- result: `success` or `skip`
- reason if skipped

Purpose:

- show what the execution layer actually changed
- distinguish event detection from successful execution

## 4. Jobs Failed Today

List failed runtime jobs with concrete failure reasons.

Suggested fields per entry:

- `job_id`
- `job_type`
- failure reason from log or runtime exception
- whether the failure was:
  - invalid site
  - missing target path
  - invalid payload
  - vault structure mismatch
  - other runtime failure

Purpose:

- make queue failures visible during nightly review
- identify routing/schema problems quickly

## 5. Unknown Captures Still Open

Summarize unresolved entries from:

- `Journal/YYYY-MM-DD-unknown.md`

Suggested fields per entry:

- `timestamp`
- `audio_file`
- `retry_count`
- `last_attempted`
- normalized transcript excerpt
- any user-added notes or `#site:` tags

Purpose:

- show what the system preserved but could not classify
- give the nightly process an actionable unresolved list

## 6. Visit Gaps

Collect same-day site activity that produced a `visit_gap` block.

Source:

- site `about.md` files written that day
- `visit_gap` blocks with:
  - `type: visit_gap`
  - `site`
  - `date`
  - `reason: "event_without_visit"`

Purpose:

- show where operational activity was recorded without a same-day visit anchor
- make missing visit coverage explicit without inventing presence

## 7. Events Without Structured Execution

List cases where the system observed something but did not produce a structured action.

Examples in the current system:

- valid events that returned `None` from `event_to_queue`
- unknown captures that remain unresolved
- site observations routed only as freeform note appends

Purpose:

- surface where the current queue contract is narrower than the observed work

## 8. Potential New Structured Actions

This is the feedback-loop section.

It should not invent new job types automatically. It should only identify repeated patterns that suggest the queue contract may be missing something stable.

Good candidates are patterns like:

- repeated freeform employee note appends about calloffs or coverage
- repeated journal notes directed at HR or payroll follow-up
- repeated site-note corrections about access, schedule assumptions, or service windows
- repeated unknown captures that later resolve into the same kind of action

Suggested fields per candidate:

- repeated pattern description
- example source jobs or events
- current workaround used
- why a structured job might be useful

Purpose:

- make contract pressure visible
- support deliberate future promotion into `queue_spec.py`

## Suggested Output Format

The digest does not need to be complicated.

A practical first version could be one markdown file per day:

- `Journal/YYYY-MM-DD-nightly-digest.md`

The current builder writes:

- `Journal/YYYY-MM-DD-digest.md`

With these top-level headings:

- `## Daily Summary`
- `## Events Created Today`
- `## Jobs Executed Today`
- `## Jobs Failed Today`
- `## Unknown Captures Still Open`
- `## Visit Gaps`
- `## Events Without Structured Execution`
- `## Potential New Structured Actions`

## What This Digest Should Not Do

- It should not create queue jobs automatically.
- It should not rewrite the queue contract.
- It should not infer visits that were never created.
- It should not merge cognition and execution into one layer.
- It should not hide failures by summarizing them away.

## Operator Questions This Should Answer

At the end of the night, the operator should be able to answer:

1. Did the system capture the day correctly?
2. Which facts made it into structured execution?
3. Which facts stayed unresolved or freeform?
4. Which failures need immediate correction?
5. Which repeated workarounds suggest a new deterministic job type would help?

## Current Status

This repository can now build the nightly digest in two ways:

- direct command:
  - `./scripts/btq-build-nightly-digest --date YYYY-MM-DD`
- watcher trigger:
  - create `nightly-digest-YYYY-MM-DD.trigger` in the configured runtime `working_dir`
  - the queue watcher will build `Journal/YYYY-MM-DD-digest.md`
  - the trigger file is then moved to `<runtime_root>/completed/working/` or `<runtime_root>/failed/working/`

This document defines the digest shape using artifacts the current implementation already produces.
