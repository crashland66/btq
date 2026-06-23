# Agent Operating Rules

- Always use `./scripts/*` wrappers for project tooling.
- Do not run `project/*.py` files directly unless a repository document explicitly says that direct execution is supported.
- Stop immediately on command failure for commands that mutate state, deploy/restart services, touch production/runtime data, expose or handle credentials, use sudo/root escalation without explicit approval, contact unknown remote hosts, or leave an ambiguous partial deploy/runtime state.
- Read-only diagnostic failures are investigation results, not automatic hard stops. Continue carefully after reporting/interpreting expected diagnostic failures such as `pgrep` no-match, `curl` connection refused against a known local service, missing local config files, missing optional tools, empty search results, or absent local processes.
- When a non-diagnostic command fails, report the failing command, exit code, stdout, and stderr.
- Do not infer paths from the launcher working directory.
- Do not change business logic while fixing environment or launch issues.

## Repo topology

- The repo root is wherever this AGENTS.md is read from; never hardcode a path.
- Deployment topology is installation-specific. Treat configured remotes, processing nodes, and hosting targets as runtime environment details, not repository defaults.
- Do not initiate deploys, service restarts, or remote pushes unless explicitly asked.

## Testing

- The Python venv lives at `project/.venv/` (matched by `scripts/btq-verify-environment`).
- Run tests via `python -m pytest`, not bare `pytest`. The `-m` form puts the repo root on `sys.path` so cross-test imports like `from tests.test_ops_dashboard import request_text` resolve; bare `pytest` does not.
- If `python` is not on `PATH` (e.g. a sandbox shell that doesn't auto-activate the venv), invoke as `project/.venv/bin/python -m pytest`. Same `-m` semantics; venv-relative interpreter.

## Architecture / Pipeline Rules

- Before implementing a new intake or processing path, inspect existing channel patterns and reuse the common BTQ spine: raw asset preservation -> metadata artifact -> local processing -> raw derived artifact -> semantic layer -> action candidates -> approved mutation job.
- Do not create a parallel format, runtime directory, retry model, failure model, artifact shape, or CLI style unless the existing pattern cannot support the new channel.
- When adding a new channel, first identify the closest existing pattern and summarize it before coding.
- LLMs may interpret, summarize, classify, and propose changes, but only deterministic writer code may mutate the operational vault.
- Do not create vault mutations directly from raw intake, transcripts, uploads, images, emails, PDFs, WhatsApp exports, or AI summaries.
- Field capture upload must remain fast, non-blocking, and evidence-first.
- Transcription, semantic cleanup, client-safe summaries, and vault mutations must remain post-upload processing layers.
- Viewer routes should remain read-only unless a task explicitly says otherwise.
- Prefer local processing for sensitive operational data.
- Do not hardwire cloud APIs for transcription, semantic cleanup, or classification.
- Media serving must remain constrained to the configured upload directory.

## Operator Context Resolution

- Before any cross-account or multi-site analysis, resolve the operator's account set from CouchDB; do not derive ownership from spreadsheets, Roadmap entries, Drive folders, or recently discussed examples.
- Before acting on a person or account name, resolve it to a canonical identity first.
- If a lookup is ambiguous, present the candidates and require operator choice; never guess.
- CouchDB is the canonical ownership source; never fall back to spreadsheet-derived ownership when the resolver is available.
- Use `project/event_pipeline/context_resolver.py` for read-only resolution: `resolve_account`, `resolve_person`, and `operator_context_snapshot`. It resolves accounts by id/name/alias, people by name/id, and returns an operator snapshot of accounts plus people labeled `direct`, `assigned`, or `both`.
- Use `./scripts/btq-context` for operator context checks: `--account <id|name|alias>`, `--person <name|id>`, `--manager <operator> --type accounts|employees`, or `--manager <operator>` for the full snapshot. JSON is the default output; ambiguous or not-found lookups exit 2 and still print candidates when available.
- Ownership means the operator employee's own `site_ids` account set plus employees related by `manager` or account assignment. Sites have no manager field. Resolution must be operator-aware, not hardcoded to Greg.
- Example: for "give me a read on all of my accounts", run `./scripts/btq-context --manager <operator>` first, then analyze only the resolved account set.
- Example: for "which of my sites are over supply budget?" or "which of my employees are attached to accounts over labor budget?", resolve the operator's accounts or people first, then join that canonical context to the external budget or timekeeping data.

## Agent Workspace

- The agent workspace is a local, non-canonical working bench under the gitignored runtime root. CouchDB remains the source of truth; operational changes still go through validated queue jobs and CouchDB processors.
- Workspace material may reference CouchDB doc ids, document revs, and queue job ids, but it is never the source of truth.
- Use `./scripts/btq-agent-workspace init --operator <op> --project <proj>` as the onboarding action to create the workspace tree. The core module is `project/event_pipeline/agent_workspace.py`.
- Use `./scripts/btq-agent-workspace path --operator <op> --project <proj> [--format json|text]` to discover the workspace path; do not hardcode or infer it from the launcher working directory.
- Use `./scripts/btq-agent-workspace new-run --operator <op> --project <proj> --slug <slug>` to create a dated run bundle at `runs/<date>-<slug>/`.
- `context/` holds resolved account, person, and operator snapshots from CouchDB, timestamped with source doc ids and revs, such as output from `project/event_pipeline/context_resolver.py` or `./scripts/btq-context`.
- `drafts/` holds pre-submit artifacts such as queue job JSON, email drafts, and analysis drafts.
- `evidence/` holds extracted spreadsheet summaries, OCR snippets, and links to local source files.
- `handoffs/` holds notes for the planner, executor, or another Codex context.
- `runs/<date>-<slug>/` holds a session bundle tying evidence, drafts, decisions, and final queue or job outcomes together.
- Three-store rule: workspace = durable local agent scratch and working material; CouchDB = canonical operational state for accounts, people, sites, and queue results, mutated only via queue jobs; ai-methodology inbox = design items, planning specs, and prompt arcs.
- Never put secrets, credentials, Gmail content, or private spreadsheet contents into the open-source repo; keep that material in the operator workspace.
- Example: for an account analysis, run `new-run`, resolve the operator/account context with `./scripts/btq-context` into `context/`, put spreadsheet or OCR extracts in `evidence/`, stage draft queue JSON in `drafts/`, submit the real change through a validated queue job so CouchDB mutates, then leave a `handoffs/` note for the next context.
