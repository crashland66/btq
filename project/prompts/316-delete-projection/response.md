# Prompt 316 Response

## Handler teardown

- `handlers/site_flags_notes.py`
  - Deleted `render_site_issue_markdown(...)`, the `final_text` job-id frontmatter loop, `issue_path.parent.mkdir(...)`, `_shared.atomic_write_text(issue_path, final_text)`, and the `updated {issue_path}` print from `log_site_issue`.
  - Deleted the upfront `resolve_site_about_path(...)` / `site_issue_path(...)` resolution used only for the site-issue projection write.
  - Deleted the append-to-note projection append/write block: `final_text = append_markdown_block(...)`, job-id frontmatter loop, target `.md` mkdir/write, and `updated {target_path}` print.
  - Deleted `about.md` Operational Notes projection append/write from location content append, including `resolve_site_about_path(...)`, read, `append_to_markdown_section(...)` into `final_text`, job-id frontmatter loop, and atomic write.
  - Deleted employee note projection append/write from employee content append, including employee file resolution, read, `append_to_markdown_section(...)` into `final_text`, job-id frontmatter loop, and atomic write.
  - Kept `apply_canonical_rmw(...)` for site issues, append-to-note, location content, employee content, visit-gap creation, canonical `content` appends, skip/move/log behavior, and `write_mutation_evidence(...)`.

- `handlers/supplies_equipment.py`
  - Deleted `render_supply_need_markdown(...)` / `render_equipment_request_markdown(...)` calls, projection payloads, `existing_text` reads, job-id frontmatter loops, `.parent.mkdir(...)`, `_shared.atomic_write_text(...)`, and path-based `updated` prints for supply needs and equipment requests.
  - Deleted upfront `resolve_site_about_path(...)` and child projection path resolution for supply/equipment entity Markdown.
  - Deleted the `about.md` atomic write from `update_site_equipment`.
  - Kept canonical supply/equipment entity RMW writes, the `render_site_equipment_section(...)` canonical content build, `_shared.patch_canonical_location_content(...)`, evidence writes, and move/log behavior.

- `handlers/supplies_equipment_transitions.py`
  - Deleted transition rerender/write blocks for supply and equipment Markdown, including `render_*_markdown(...)`, projection payload, existing file read, job-id frontmatter loop, and `_shared.atomic_write_text(...)`.
  - Deleted transition-time projection path lookup from handler execution.
  - Kept canonical transition validation, `apply_canonical_rmw(...)`, status/timestamp/actor/note field mutations, evidence writes, and move/log behavior.

- `handlers/people.py`
  - Deleted `render_personnel_event_markdown(...)`, projection payload, existing event file read, job-id frontmatter loop, event `.md` mkdir/write, and path-based `updated` print.
  - Deleted personnel-event projection path resolution from handler execution.
  - Deleted `render_person_markdown(...)` call from `add_person`; kept canonical employee doc creation and idempotency ledger behavior.
  - Deleted recruiting close `about.md` Operational Notes projection append/write and person `## Schedule Changes` projection append/write.
  - Deleted `set_entity_status` frontmatter projection path resolution and `.md` rewrite.
  - Kept canonical employee/personnel-event writes, recruiting/site and schedule canonical `content` appends, status canonical RMW, evidence writes, idempotency, skip/move/log behavior.

- `handlers/visits.py`
  - Deleted the `BTQ_VAULT_MARKDOWN_WRITE` gated visit `.md` block, including site path resolution, Visits dir mkdir, visit file read, job-id frontmatter, atomic write, and projection updated print.
  - Deleted photo-capture journal `.md` read/append/job-id/write block and path-based updated print.
  - Kept canonical visit upsert, canonical photo journal RMW content append, photo attachment writes, evidence writes, and move/log behavior.

- `handlers/misc.py`
  - Deleted voice-memo projection target path helpers, projection target storage, `_project_voice_memo_note_target(...)`, `_prepare_voice_memo_note_target(...)`, `append_voice_memo_note_to_target(...)`, and all voice-memo `.md` writes.
  - Kept `voice_memo_person_link(...)` and its `resolve_person_vault_path(...)` use because it builds canonical note wikilink content.
  - Deleted personal journal `.md` write to `personal_vault_root`.
  - Replaced `persist_supply_order(...)` handler use with direct JSON record write, removing the supply-order Markdown remnant while keeping the canonical supply_order doc and JSON artifact.
  - Kept canonical voice memo note RMW, personal journal RMW, retarget/promote JSON artifact writes, supply-order canonical RMW, quarantine JSON writes, evidence writes where present, and move/log behavior.

- `handlers/unknowns.py`
  - Deleted frozen unknown `.md` creation/update from `record_unknown_capture`.
  - Deleted `reclassify_unknown` job marker write to unknown `.md` and its stale Markdown marker skip.
  - Kept canonical unknown_capture RMW, evidence write for created unknown docs, unknown reclassification transcript write (`*.normalized.txt`) because it is an input artifact, generated queue job JSON writes because they are runtime queue inputs, and canonical resolve/attempt updates.

## Projection watcher and CLI

- Deleted `project/btq_vault/projection_watcher.py`.
- Removed `btq project-vault` subcommand registration and `handle_project_vault(...)` from `project/btq_cli/vault.py`.
- Removed the stale `btq project-vault` instruction from freeze marker text.
- Left `markdown_export`, `markdown-export` CLI, `export_all`, and `render_*_markdown` function definitions intact.

## Tests

- Retargeted direct handler `.md` assertions to canonical doc `content` or canonical fields for append-to-note, voice memo notes, photo capture journals, personal journal entries, unknown capture/reclassification, set-entity-status, durable queue, site issue/supply/equipment/personnel-event creation, and production-default behavior.
- Retargeted `about.md` Operational Notes assertions to `location_<id>.content`.
- Retargeted person `## Schedule Changes` and status assertions to `employee_<id>.content` or canonical employee fields.
- Removed projection watcher tests.
- Kept export-backed Markdown assertions that flow through `run_jobs` plus `project_markdown_exports` / `markdown_export.export_all`.

## Not run

- Did not run `git`, `pytest`, `ruff`, `uv`, or `pip`.
- Did not touch real CouchDB.
