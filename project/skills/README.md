# Skill Library

A BTQ skill is a reusable AI capability stored as plain files: metadata, versioned prompts, notes, and optional fixtures. Skills are deterministic prompt assets. They do not call model APIs, mutate canonical operational state, or write runtime state.

Skills are versioned so prompt changes can be reviewed, tested, and selected explicitly. The `current_version` in `skill.json` is the default for normal use, while older versions remain available for comparison or repeatability.

## Structure

Each skill lives under `project/skills/<skill-id>/`:

```text
skill.json
v1.md
v2.md
notes.md
fixtures/
  sample_input.md
  expected_shape.md
```

`skill.json` is the index record. It declares the skill id, display name, description, current version, available versions, tags, accepted input types, expected output types, origin, and status.

## Commands

List skills:

```sh
./scripts/btq skill list
```

Show metadata and the selected prompt path:

```sh
./scripts/btq skill show web-review
./scripts/btq skill show web-review --version v2
```

Compose a prompt from a skill and input file:

```sh
./scripts/btq skill run web-review --input path/to/input.md
./scripts/btq skill run web-review --version v2 --input path/to/input.md --out path/to/output.md
./scripts/btq skill run web-review --version v2 --input path/to/input.md --structured
./scripts/btq skill run web-review --version v2 --input path/to/input.md --structured --to-queue-dry-run
```

Validate the library:

```sh
./scripts/btq skill validate
./scripts/btq skill validate web-review
```

## Adding a Skill

1. Create `project/skills/<skill-id>/`.
2. Add `skill.json` with all required metadata fields.
3. Add one markdown prompt file for each listed version.
4. Set `current_version` to one of the listed versions.
5. Add `notes.md` for source context, decisions, and maintenance notes.
6. Add fixtures when useful.
7. Run `./scripts/btq skill validate`.

## Read-Only Boundary

The skill runner is prompt-composition only for now. It reads skill files and an input file, combines them, and writes the composed prompt to stdout unless `--out` is explicitly provided. This is different from queue mutations: queue jobs pass through the BTQ evidence, review, and deterministic writer layers before any operational vault change. Skills are reusable reasoning prompts, not mutation jobs.

### agents.txt Integration

When the input file content is a URL beginning with `http://` or `https://`, the runner attempts to fetch `<url>/agents.txt` with a 2-second timeout. If the file is available and valid YAML, it is parsed and injected at the top of the composed prompt under `## Agents Context (if available)`.

If `agents.txt` is missing, unreachable, or invalid YAML, composition continues and the injected context is `None`. This optional fetch is the only network access in the skill runner.

### Structured Output Mode

`--structured` appends a dual-mode output instruction to the composed prompt. The normal markdown output remains unchanged, and the model is additionally asked to produce a deterministic YAML block shaped like:

```yaml
actions:
  - type: <action_type>
    target: <file|endpoint|resource>
    description: <short description>
    payload:
      <arbitrary structured fields>
```

This is queue-ready prompt guidance only. The runner saves the prompt as-is and does not parse, execute, enqueue, or mutate anything.

### Queue Bridge (Dry Run)

`--to-queue-dry-run` reads a structured `actions:` YAML block from composed structured output and maps it into queue-shaped job previews. It is available only with `--structured`.

Mapping:

- `update_file` -> `update_file`
- `add_file` -> `create_file`
- `http_call` -> `external_call`

Each preview job includes `job_type`, `target`, `payload`, and `source` in the form `skill:<skill-id>:<version>`. Unknown action types and malformed YAML fail with clear validation errors. Empty actions produce a warning and no jobs.

The bridge is dry-run only. It does not write queue files, execute HTTP calls, mutate the vault, or hand jobs to the queue processor. Use it for human verification before any future approved mutation path.

### Determinism Guarantee

Determinism matters because skills may become reusable queue-adjacent assets. The same input, skill version, and `agents.txt` content should produce byte-identical prompts. The runner avoids timestamps and random values, uses sorted YAML keys for injected context, and preserves stable prompt composition order.
