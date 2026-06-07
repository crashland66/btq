# Web Review Demo

This demo walks through the deterministic BTQ skill flow without calling a model or mutating the vault.

Run it from the repository root:

```sh
./scripts/demo_web_review.sh
```

## What Happens

1. The `web-review` skill composes a structured prompt from `project/demo/web_review_demo.md`.
2. The composed prompt is written to `/tmp/demo_output.md`.
3. Queue-shaped dry-run output is written to `/tmp/demo_queue.json`.
4. The guarded executor runs in `auto-safe` mode. Because the demo prompt contains no real generated `actions:` block, no queue jobs are executed.
5. The script finds the latest journal produced by the structured skill run.
6. Replay runs against that journal with `--diff`.

## Expected Outputs

- `/tmp/demo_output.md` contains the composed web review prompt.
- `/tmp/demo_queue.json` contains the mapped queue preview, usually `[]` for this deterministic prompt-only demo.
- `runtime/journal/` contains replayable JSON journals for the structured skill runs.
- Terminal output includes `## Queue Preview`, `## Execution Journal`, and `## Replay`.

## Safety

The demo uses the existing skill system only. It does not call external model APIs, does not execute external calls, does not write queue files, and does not mutate the vault.
