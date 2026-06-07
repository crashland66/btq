# Ops Dashboard Help

## Approve a candidate

Prefer the CLI when reviewing a batch from a terminal session or when the browser is unavailable.

```bash
./scripts/btq review-field-capture-candidate --candidate-id <candidate_id> --status approved
```

UI path: `/candidates`.

## Reject a candidate

Prefer the CLI when you need to script or document a rejection outside the dashboard.

```bash
./scripts/btq review-field-capture-candidate --candidate-id <candidate_id> --status rejected
```

UI path: `/candidates`.

## Mark a candidate's client informed

Prefer the CLI when updating communication status while already working from a terminal.

```bash
./scripts/btq field-capture-client-informed --candidate-id <candidate_id> --method email
```

UI path: `/candidates`.

## Generate an approved draft from a candidate

Prefer the CLI when generating all currently approved drafts as a maintenance pass.

```bash
./scripts/btq generate-approved-drafts --dry-run
./scripts/btq generate-approved-drafts
```

UI path: `/drafts?candidate_id=<candidate_id>&action=generate`.

## Stage an approved draft into the queue

Prefer the CLI when staging all validated drafts during a controlled operations window.

```bash
./scripts/btq stage-approved-drafts --dry-run
./scripts/btq stage-approved-drafts
```

UI path: `/drafts?draft_id=<draft_id>`.

## Retry a failed photo-vision sidecar

Prefer the CLI when you need to force a local vision retry immediately instead of waiting for the watcher.

```bash
./scripts/btq describe-field-photos --replace-failed --photo-asset-id <photo_asset_id>
```

UI path: `/failed?sidecar_id=<photo_asset_id>`.

## Browse captures by site and date

Prefer the CLI when exporting or scripting a runtime inspection.

```bash
./scripts/btq review-dashboard --channel field_capture
```

UI path: `/captures`.

## Inspect a failed queue job

Prefer the CLI when correlating a failed job with queue processor logs.

```bash
./scripts/btq review-dashboard --channel field_capture
```

UI path: `/failed?job_id=<job_id>`.
