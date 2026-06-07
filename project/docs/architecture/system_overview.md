# System Overview

BTQ converts field inputs into operational memory. The primary path is:

```text
field-capture PWA / voice-memo PWA / voice transcription
  -> capture_ingest
  -> raw preservation + metadata/sidecars
  -> local runtime processing
  -> semantic layer
  -> action candidates/drafts
  -> approved mutation job
  -> queue processor / handlers
  -> CouchDB canonical state + evidence
  -> optional Markdown projection/export (`btq markdown-export`)
  -> processed or failed queue archive
```

## Key Components

```text
project/transcription_pipeline/main.py
  Audio watcher/scanner, Whisper transcription, personal-journal detection,
  event pipeline invocation, local job emission, queue staging.

project/event_pipeline/
  domain_resolver.py  Normalizes domain vocabulary.
  extractor.py        Rule-based event extraction from normalized text.
  enricher.py         Consolidates staffing and site-observation events.
  validator.py        Validates event schema and writes failed events.
  main.py             Orchestrates transcript -> raw/enriched/valid/failed events.

project/event_to_queue/
  adapter.py          Converts validated events into queue job payloads.

project/queue_spec.py
  Queue job type registry and minimum required-field validation.

project/queue_processor/
  main.py             Deterministic job handlers and canonical writer boundary.
  idempotency.py      Job-id hashing and frontmatter marker handling.
  watch.py            Outbox staging, working triggers, and queue draining loop.

project/field_capture/
  Field-capture intake app. It shares the capture_ingest path and preserves raw
  media/metadata before local processing and reviewed queue jobs.

project/voice_memo/
  Voice-memo intake app. It shares the capture_ingest path and writes raw voice
  memo records for transcription-side processing.

project/io_atomic.py
  Atomic text writes and cross-device/iCloud-aware safe move helper.

project/nightly_digest_builder.py
  Deterministic-ish daily synthesis from journals, reports, events, jobs,
  unknown captures, and visit gaps.
```

## Directory Map

```text
BTpipeline/                         iCloud transport area
  inbox/voice/                      voice memo ingress
  outbox/                           manually authored queue job ingress
  working/                          trigger files, e.g. nightly digest requests

~/btq_runtime/                      local non-iCloud runtime area
  claimed/                          files claimed from transport
  processing/                       active working triggers
  queue/                            queue jobs ready for deterministic processing
  processed/                        successfully handled job files
  failed/                           rejected job files and failed runtime inputs
  completed/                        completed ingress artifacts
  temp/                             atomic staging/lock area
  logs/                             watcher and processor logs

repo/local/                         local generated artifacts
  audio_processing/                 audio staging and transcript sidecars
  events_raw/
  events_enriched/
  events_valid/
  events_failed/
  queue_jobs/                       generated job JSON before runtime staging
  logs/                             per-audio process logs
  state/last_site.json              short-lived site context fallback

CouchDB
  btq_vault                         canonical operational entities + evidence
  btq_field_captures                field-capture ingress documents
  btq_voice_memos                   voice-memo ingress documents

vault/                              optional Markdown projection/export
  Accounts/.../Locations/...        site notes, visits, supplies
  Journal/                          daily journals, unknown captures, digests
  People/                           employee notes

personal_vault/
  Journal/                          personal journal entries only
```

## Component Interaction Map

```text
             +-------------------------+
             | iCloud voice inbox      |
             +-----------+-------------+
                         |
                         v
             +-------------------------+
             | transcription_pipeline  |
             +-----------+-------------+
                         |
       +-----------------+------------------+
       |                                    |
       v                                    v
 personal_journal_entry             event_pipeline
       |                             raw -> enriched -> valid/failed
       |                                    |
       +-----------------+------------------+
                         v
                 event_to_queue
                         |
                         v
              local queue_jobs/*.json
                         |
                         v
              runtime queue/*.json
                         |
                         v
                queue_processor / handlers
                         |
       +-----------------+------------------+
       |                                    |
       v                                    v
 CouchDB canonical state             processed/failed
      + evidence
        |
        v
 optional Markdown projection/export
```

## AI Interaction Boundaries

Whisper transcription is probabilistic even with `temperature=0.0`; the event extractor is deterministic code over that transcript. Current extraction is rule-based, not an LLM planner. The architectural boundary still matters: upstream capture may be lossy or wrong, but downstream mutation is constrained to known job types and deterministic handlers.

AI or future AI agents should not directly edit canonical state. They may generate queue jobs matching `queue_spec.py`, but the queue processor and handlers own validation, idempotency checks, canonical CouchDB writes through `btq_vault` / `canonical_rmw`, and evidence preservation. Markdown output is an opt-in projection/export, not the mutation boundary.

## Operational Flow

The queue watcher is the operational hub. Each pass processes working triggers, stages outbox jobs, then drains the runtime queue. The transcription watcher separately detects stable audio files and stages generated jobs into the same runtime queue. This creates a staged file-based architecture with multiple producers and one intended deterministic consumer.
