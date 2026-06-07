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
