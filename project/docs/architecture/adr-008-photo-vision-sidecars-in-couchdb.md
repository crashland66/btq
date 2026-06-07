# ADR-008 Photo-Vision Sidecars Become CouchDB Documents

## Status

Proposed (2026-05-21). Implements step 3 of the ADR-007 migration path — "the
next new entity type lands CouchDB-first." Staged and incremental; no consumer
is cut over big-bang. Supersedes nothing; extends ADR-007.

## Context

Photo-vision sidecars are JSON metadata artifacts written one-per-image by
`field_capture/photo_vision.py` (`completed_payload` / `failed_payload`) to
`runtime/field_capture/photo_vision/{photo_asset_id}.json`. There are ~2,400 of
them. They are the BTQ spine's "raw derived artifact / semantic layer" for the
photo channel: model description, `area_guess`, `visible_objects`,
`possible_conditions`, `possible_issues`, `confidence`, `needs_human_review`,
free-text `warnings`, plus site/capture/provenance.

They are the only major derived artifact that lives in **neither** the vault
**nor** CouchDB — disk-only JSON. Two needs now make that a problem:

1. **The data is not searchable.** The ops dashboard scans every sidecar into a
   5-second-cached in-memory list (`load_photo_vision_sidecars`), guarded by a
   lock so the cold scan of all 2,400 files does not spike RSS past the
   watchdog. Nothing queries sidecar *content* — `captures.py` filters captures
   by site/date/has-flags but never by `description` or `visible_objects`.
2. **Image-quality signals are weak.** The vision prompt asks the model to lower
   `confidence` for blurry/obscured images, set `needs_human_review`, and emit
   free-text `warnings` like `"image is dark"` / `"subject partly out of
   frame"`. But `warnings[]` is unstructured free text that also carries the
   advisory boilerplate and failure-closed messages; wording is inconsistent and
   not reliable to filter on.

ADR-007 named "the next new entity type" as the first CouchDB-native slice.
Photo-vision sidecars qualify: a derived artifact, never hand-edited, with a
concrete pull (search) and a concrete pain (no query surface).

## Decision

Photo-vision sidecars become first-class CouchDB documents in a new
`btq_photo_vision` database. CouchDB becomes the **queryable surface** for
sidecar data; search and the dashboard read it via Mango `_find`.

Each sidecar gains a normalized, enumerated **`quality` block** — a structured
replacement for guessing at free-text `warnings` — generated two ways:

- **New captures:** the vision prompt is extended with a closed-enum
  `quality_flags` key; the model reports flags directly (`source: "model"`).
- **Existing captures:** a deterministic `derive_quality()` keyword-maps the
  free-text `warnings[]` plus `confidence` / `needs_human_review`
  (`source: "derived"`). This flags all ~2,400 existing images with **zero
  re-inference** — re-running them would cost ~7 days of CPU vision inference on
  the Dell.

The disk JSON sidecar is **retained** as the durable raw artifact. Photo-vision
runs on the Pro and must stay evidence-first and non-blocking (AGENTS.md): the
sidecar write must succeed even when CouchDB is briefly unreachable. So the disk
write stays authoritative for *durability*; the CouchDB PUT is a best-effort
write-through, and a reconciler converges any gaps. CouchDB is the source of
truth for *queries*; disk remains the source of truth for *writes* until the
remaining disk consumers (auto-retry in `pipeline_watcher.py`, `failed.py`) are
migrated in a later step. This keeps the slice incremental per ADR-007.

## Options Considered

### Option A — Dashboard in-memory search over the disk scan

Add a `/photos` section that token-matches the existing cached sidecar list. No
new infrastructure. Rejected as the *destination* (kept as the fallback): it
does not advance the ADR-007 migration, gives no field-level query, and the
whole-directory scan does not scale. It survives as the graceful-degradation
path when CouchDB is unreachable.

### Option B — SQLite FTS5 index rebuilt from the sidecar directory

Ranked full-text, scales well. Rejected: introduces a *new* storage engine and
a separate index artifact to keep in sync, against ADR-007's explicit reasoning
for consolidating onto CouchDB (Option D there).

### Option C — Sidecars become CouchDB documents (chosen)

Promotes a real entity into the CouchDB substrate the rest of the AI loop is
moving toward. Gets Mango queries, the `_changes` feed, and entity-scoped
retrieval for free. Cost: another database to back up, and Mango has no native
ranked full-text.

### CouchDB-only (no disk retention) — rejected for now

Cutting the disk sidecar immediately would break `pipeline_watcher.py`
auto-retry and `failed.py`, which read disk sidecars today, and would make a
CouchDB outage during capture a data-loss event. Write-through with disk
retained is the incremental path; the disk artifact is retired only after its
consumers move (a later ADR-007 step).

## Document Schema — `btq_photo_vision`

One document per photo asset. `_id` is the deterministic `photo_asset_id`, so a
sidecar replacement (failed→retry, judgment-language rewrite) is an idempotent
overwrite of the same document.

```text
_id                 photo_asset_id            natural key — idempotent PUT
doc_type            "photo_vision_sidecar"
schema_version      1
status              completed | failed | skipped
generated_at        ISO 8601

# entity refs — ADR-007: every doc carries its refs for entity-scoped retrieval
site_id             string
capture_id          string
photo_asset_id      string
photo_id            string
submitter_id        string (when resolvable)

# vision output (unchanged field meanings)
description         string
area_guess          string
submitted_area      string
submitted_phase     string
visible_objects     [string]
possible_conditions [string]
possible_issues     [string]
confidence          number 0.0-1.0 | null
needs_human_review  bool
warnings            [string]   retained verbatim for back-compat

# NEW — normalized quality block
quality
  analyzable        bool
  severity          ok | degraded | unusable
  flags             [enum]     see enum below
  confidence        number | null   (copy of model confidence)
  needs_human_review bool
  source            "model" | "derived"

# search aid
search_text         lowercased concat of description + visible_objects +
                    possible_conditions + possible_issues + area_guess

# model + provenance
model_name, model_provider, source_image_hash
provenance          { intake_json_path, image_media_url, image_filename, ... }
error               { type, message, can_retry, ... }   (failed only)
```

### `quality.flags` enum (closed set)

`blurry`, `motion_blur`, `out_of_frame`, `partly_obscured`, `too_dark`,
`too_bright`, `glare`, `low_resolution`, `contains_people`, `unanalyzable`.

### `quality.severity` derivation

- **`unusable`** — `status` is `failed`/`malformed`; OR model reported
  `analyzable: false`; OR `confidence < 0.3` with an empty `description`.
- **`degraded`** — any `quality.flags` entry; OR `needs_human_review` is true;
  OR `confidence < 0.6`.
- **`ok`** — none of the above.

`derive_quality()` is the single generator. It reads model-supplied
`quality_flags` when present (`source: "model"`); otherwise it keyword-maps
`warnings[]` — e.g. `dark`→`too_dark`, `blur`/`blurry`→`blurry`,
`out of frame`/`partly out`→`out_of_frame`, `obscured`→`partly_obscured`,
`glare`/`reflection`→`glare`, `bright`/`overexposed`→`too_bright`,
`person`/`people`→`contains_people` (`source: "derived"`). It runs at sidecar
write time (persisted into both the disk JSON and the CouchDB doc) and is also
computed lazily by the dashboard summary, so the existing ~2,400 disk sidecars
gain a `quality` block with no re-inference and no backfill dependency.

## Mango Indexes And Retrieval

Indexes on `btq_photo_vision`:

- `["site_id", "generated_at"]` — entity-scoped pull of every photo for one
  site (the ADR-007 retrieval primitive).
- `["quality.severity", "generated_at"]` — the "show me the bad images" query.
- `["status", "generated_at"]` — failed / malformed sweeps.
- `["capture_id"]` — per-capture grouping for the capture detail view.

Free-text: Mango has no ranked full-text. The `search_text` field plus a
`$regex` selector, evaluated *within* a site/severity/status-narrowed selector,
covers substring search at this volume (thousands of docs). True ranked FTS
(a Lucene-backed `couchdb-search` design doc) is a deferred option, taken only
if substring search proves insufficient — recorded here so it is a known lever,
not a surprise.

## Migration Path

Incremental. Each stage is independently shippable; stage 1 delivers user value
before any CouchDB work exists.

1. **Quality block, local-only.** Add `derive_quality()` and the `quality` dict
   to the disk sidecar payloads and the dashboard summary; extend the vision
   prompt with `quality_flags`. No CouchDB. Fully reversible. The dashboard can
   already filter by `quality.severity` over the in-memory list.
2. **CouchDB schema + writer.** Create `btq_photo_vision`, a document builder, an
   idempotent PUT writer (fetch current `_rev`, retry once on 409 conflict), and
   the Mango indexes. Provisioning script alongside the existing CouchDB
   provisioning.
3. **Write-through.** `process_photo_assets()` issues a best-effort CouchDB PUT
   after the disk write. Non-blocking — a PUT failure is logged and never fails
   the pipeline; the disk sidecar remains authoritative.
4. **Backfill + reconcile.** A script (or a pass folded into
   `pipeline_watcher.py`) pushes the ~2,400 existing disk sidecars into CouchDB
   and keeps disk and CouchDB converged.
5. **Search.** A new `/photos` dashboard section: free-text query plus filters
   for site, `area_guess`, `quality.severity`, specific flag, `status`,
   confidence range, and date. Reads CouchDB `_find`; falls back to the disk
   scan (Option A) when CouchDB is unreachable — the cached, graceful-degradation
   pattern established by the voice-memo intake state on the health page. The
   health page gains `quality.severity` counts.

## Risks

- **CouchDB unreachable during capture.** Mitigated by design: the disk write is
  authoritative, the CouchDB PUT is best-effort, and the stage-4 reconciler
  converges any gap.
- **`_rev` conflicts on re-PUT** (sidecar replacement, backfill re-run).
  Mitigated: the writer fetches the current `_rev` before PUT and retries once
  on HTTP 409.
- **`$regex` Mango search is unranked** and scans within the selector.
  Acceptable at current volume; documented above as revisitable.
- **Another database to back up.** Folds into the backup-discipline risk ADR-007
  already records; `btq_photo_vision` must be added to whatever backup routine
  ADR-007 step calls for.
- **Vision-prompt change is a behavior change to a probabilistic stage.** The
  `quality_flags` key is additive and optional; existing keys are unchanged.
  Worth a benchmark pass on `gemma4:26b` before the prompt change ships.
- **Credentials.** `btq_photo_vision` reuses the existing `BTQ_COUCHDB_*` env
  auth — no new secret surface beyond the plaintext-plist exposure ADR-007
  already notes.

## Consequences

Sidecar data becomes queryable by site, capture, area, status, and image
quality, and substring-searchable by description and detected objects. Low-
quality images (blurry, out-of-frame, too-dark, unanalyzable) become a
first-class, filterable category instead of free text. The photo channel joins
field capture and voice memo as CouchDB-native, leaving only the markdown vault
outside — consistent with ADR-007's end state.

In exchange, `btq_photo_vision` becomes a component whose availability and
backup matter, and ranked full-text search remains a deferred capability rather
than a delivered one.
