# Security Policy

## Reporting a Vulnerability

Please report security issues privately by email to **greg@example.com**.
Do not open a public issue for security reports.

Include where relevant:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- affected component(s) and version/commit.

You can expect an acknowledgement within a few business days. This is an
alpha-stage, single-operator project, so response times are best-effort.

## Scope and Status

BT Pipeline is a local-first, single-operator field-operations pipeline. The
trust boundaries are intentional and load-bearing:

- The queue processor (`project/queue_processor/main.py`) is the only path that
  performs canonical writes; AI output is interpretation, not authority.
- Capture and transcription run locally; vision/transcription inference is
  local-only and must not call external AI services.

Operator-supplied secrets and environment-specific values (bearer tokens,
credentials, hostnames, absolute paths, customer/employee names) are provided
at runtime via configuration and environment variables and must not be
committed to the repository.
