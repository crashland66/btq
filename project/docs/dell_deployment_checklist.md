# Dell Processing Node Deployment Checklist

This checklist is for moving the BTQ processing watchers onto the Dell
processing node. It covers the queue watcher and transcription watcher only:

- [btq-queue-watch.service](/Users/operator/btq/project/deploy/systemd/btq-queue-watch.service)
- [btq-transcription-watch.service](/Users/operator/btq/project/deploy/systemd/btq-transcription-watch.service)

The Mac repo at `/Users/operator/btq` remains the source of truth. Do not use a
GitHub remote deployment path for this repo unless a future repo document
explicitly changes that.

## Production Topology

- Processing host: Dell Linux node.
- Runtime root: `/srv/btq/runtime` or the operator-selected
  `@@BTQ_RUNTIME_ROOT@@`.
- Project root: `/srv/btq/project` or the substituted
  `@@BTQ_PROJECT_ROOT@@` source mirror.
- Python: the project virtualenv at `@@BTQ_PYTHON_BIN@@`.
- Service account: `@@BTQ_SERVICE_USER@@:@@BTQ_SERVICE_GROUP@@`.
- Queue watcher service: `btq-queue-watch.service`.
- Transcription watcher service: `btq-transcription-watch.service`.
- CouchDB: configured through `BTQ_COUCHDB_URL`, `BTQ_COUCHDB_USER`, and
  `BTQ_COUCHDB_PASSWORD`. CouchDB `btq_vault` is the canonical operational store.
- Local Dell CouchDB should exist before watcher cutover. The Dell is a full
  CouchDB peer, not just an Ollama/Whisper appliance: it should carry
  `btq_field_captures`, `btq_photo_vision`, `btq_queue`,
  `btq_vault`, and `btq_voice_memos`.

Field-capture SPA deployment remains covered by
[field-capture-production-deployment.md](/Users/operator/btq/project/field_capture/field-capture-production-deployment.md).
This checklist is for the Dell processing node, not the VPS web app.

## Runtime Directories

Create the runtime root before enabling services. The service user must own
directories that watchers write, and the operator/deploy group should have read
access for inspection.

Expected layout:

```text
@@BTQ_RUNTIME_ROOT@@/
  intake/
    outbox/
  claimed/
  processing/
  completed/
  failed/
  queue/
  logs/
  uploads/
```

Use explicit ownership and modes:

```bash
install -d -o @@BTQ_SERVICE_USER@@ -g @@BTQ_SERVICE_GROUP@@ -m 0750 @@BTQ_RUNTIME_ROOT@@
install -d -o @@BTQ_SERVICE_USER@@ -g @@BTQ_SERVICE_GROUP@@ -m 0750 \
  @@BTQ_RUNTIME_ROOT@@/intake/outbox \
  @@BTQ_RUNTIME_ROOT@@/claimed \
  @@BTQ_RUNTIME_ROOT@@/processing \
  @@BTQ_RUNTIME_ROOT@@/completed \
  @@BTQ_RUNTIME_ROOT@@/failed \
  @@BTQ_RUNTIME_ROOT@@/queue \
  @@BTQ_RUNTIME_ROOT@@/logs \
  @@BTQ_RUNTIME_ROOT@@/uploads
```

The runtime root holds operational artifacts and may grow indefinitely. Do not
place it on a small system partition.

## Directory Permissions

Use a dedicated non-login service account:

```bash
useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin \
  --user-group @@BTQ_SERVICE_USER@@
```

The account needs:

- read and execute access to `@@BTQ_PROJECT_ROOT@@`;
- execute access to `@@BTQ_PYTHON_BIN@@`;
- read/write access to `@@BTQ_RUNTIME_ROOT@@`;
- network access to the CouchDB endpoint for canonical `btq_vault` mutations;
- read access to model cache locations used by Whisper and Ollama.

Do not grant broader write access to unrelated application trees. If the CouchDB
connection is unclear, stop before starting the queue watcher.

## Environment Variables

The systemd templates carry placeholder `Environment=` lines. Substitute values
before installing them under `/etc/systemd/system`.

Required:

- `PATH`: minimal executable path for Python, ffmpeg, git, and local model
  tools. Start with `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` and add GPU
  or package-manager paths only when needed.
- `PYTHONUNBUFFERED=1`: keeps watcher logs timely.
- `BTQ_COUCHDB_URL`: CouchDB endpoint reachable from the Dell.
- `BTQ_COUCHDB_USER`: CouchDB user for replication/intake operations.
- `BTQ_COUCHDB_PASSWORD`: CouchDB password. Keep the substituted unit file root
  readable only if credentials are embedded directly.
- `BTQ_RUNTIME_ROOT`: runtime artifact root.

Operator-set placeholders:

- `@@BTQ_PROJECT_ROOT@@`: checked-out BTQ project root on the Dell.
- `@@BTQ_PYTHON_BIN@@`: virtualenv Python path.
- `@@BTQ_SERVICE_USER@@` and `@@BTQ_SERVICE_GROUP@@`: service account.
- `@@BTQ_LOG_DIR@@`: usually `@@BTQ_RUNTIME_ROOT@@/logs`.
- `@@BTQ_TRANSCRIPTION_WORKER_COUNT_ARGS@@`: empty for the current default of
  two workers, or `--worker-count N` after sizing the Dell.

Optional environment for adjacent processing:

- `BTQ_FIELD_CAPTURE_VISION_MODEL=qwen2.5vl:7b`
- `BTQ_OLLAMA_URL=http://127.0.0.1:11434`

## CouchDB Peer Setup

Install CouchDB on the Dell before enabling BTQ services. Bind it to the
tailnet/private interface only, create an admin user, then from the BTQ checkout
run:

```bash
BTQ_COUCHDB_URL=http://127.0.0.1:5984 \
BTQ_COUCHDB_USER=... \
BTQ_COUCHDB_PASSWORD=... \
./scripts/btq setup-couchdb --skip-migrate
```

Then configure replication from the Pro checkout with Pro, VPS, and Dell
credentials:

```bash
BTQ_PRO_COUCHDB_URL=http://127.0.0.1:5984 \
BTQ_PRO_COUCHDB_USER=... \
BTQ_PRO_COUCHDB_PASSWORD=... \
BTQ_VPS_COUCHDB_URL=http://203.0.113.10:5984 \
BTQ_VPS_COUCHDB_USER=... \
BTQ_VPS_COUCHDB_PASSWORD=... \
BTQ_DELL_COUCHDB_URL=http://10.0.0.10:5984 \
BTQ_DELL_COUCHDB_USER=... \
BTQ_DELL_COUCHDB_PASSWORD=... \
./scripts/btq setup-couchdb --with-replication --skip-migrate
```

Do not start the Dell queue watcher until replication health is green and the
Mac/Pro queue watcher is stopped for cutover. Two active queue processors
watching the same writable queue can race each other.

## Model Availability

Verify models before starting the watchers.

Whisper:

```bash
sudo -u @@BTQ_SERVICE_USER@@ @@BTQ_PYTHON_BIN@@ - <<'PY'
import whisper
whisper.load_model("large-v3")
print("whisper large-v3 ok")
PY
```

Use the configured Whisper model if it differs from `large-v3`.

Vision model:

```bash
ollama pull qwen2.5vl:7b
ollama list | grep 'qwen2.5vl:7b'
```

The Stage D units do not start the field-capture vision watcher, but the Dell
should have the model present before the processing-node cutover so photo
semantics can be enabled without another provisioning pass.

Voice semantic benchmark models:

```bash
ssh deploy@10.0.0.10
ollama pull qwen3:1.7b
ollama pull qwen3:4b
ollama pull gemma3:4b
# optional
ollama pull phi4-mini
```

From the Pro, verify Dell Ollama is reachable:

```bash
curl http://10.0.0.10:11434/api/tags
```

Then run the non-mutating voice semantic eval harness from the Pro checkout:

```bash
cd /Users/operator/btq
# The Ollama text client disables reasoning by default for this eval path.
# Add --enable-thinking only when deliberately benchmarking reasoning mode.
PYTHONPATH=project python3 -m voice_memo.semantic_eval \
  --engine ollama \
  --url http://10.0.0.10:11434 \
  --model qwen3:1.7b \
  --output /tmp/btq-voice-semantic-qwen3-1.7b.jsonl

PYTHONPATH=project python3 -m voice_memo.semantic_eval \
  --engine ollama \
  --url http://10.0.0.10:11434 \
  --model qwen3:4b \
  --output /tmp/btq-voice-semantic-qwen3-4b.jsonl

PYTHONPATH=project python3 -m voice_memo.semantic_eval \
  --engine ollama \
  --url http://10.0.0.10:11434 \
  --model gemma3:4b \
  --output /tmp/btq-voice-semantic-gemma3-4b.jsonl
```

Do not switch production `BTQ_VOICE_MEMO_SEMANTIC_ENGINE=ollama` until the eval
results have been reviewed. The Dell should return model JSON only; the Pro
continues to own validation, review artifacts, queue staging, and CouchDB
writes.

## Storage

Keep disk headroom visible before and after cutover:

```bash
df -h @@BTQ_RUNTIME_ROOT@@
du -sh @@BTQ_RUNTIME_ROOT@@/uploads @@BTQ_RUNTIME_ROOT@@/completed @@BTQ_RUNTIME_ROOT@@/failed
```

Growth drivers:

- uploaded media under `uploads/`;
- completed source audio and derived artifacts under `completed/`;
- failed queue and processing artifacts under `failed/`;
- watcher stdout/stderr logs under `logs/`;
- local model caches for Whisper and Ollama.

The runtime tree is evidence and recovery material. Do not delete it during a
service deploy. Archive or prune only through an explicit maintenance task.

## Log Rotation

The units write stdout and stderr to `@@BTQ_LOG_DIR@@`. Install logrotate for
those files, or configure journald size limits if the units are changed to use
the journal.

Example logrotate snippet:

```text
@@BTQ_LOG_DIR@@/btq-queue-watch.*.log @@BTQ_LOG_DIR@@/btq-transcription-watch.*.log {
  daily
  rotate 14
  compress
  missingok
  notifempty
  copytruncate
  su @@BTQ_SERVICE_USER@@ @@BTQ_SERVICE_GROUP@@
}
```

If using journald instead, set host-level size limits in `journald.conf` and
change the unit output to `journal`.

## Health Checks

After starting services:

```bash
systemctl status --no-pager btq-queue-watch.service
systemctl status --no-pager btq-transcription-watch.service
journalctl -u btq-queue-watch.service -n 50 --no-pager
journalctl -u btq-transcription-watch.service -n 50 --no-pager
tail -n 50 @@BTQ_LOG_DIR@@/btq-queue-watch.out.log
tail -n 50 @@BTQ_LOG_DIR@@/btq-transcription-watch.out.log
```

Look for heartbeat-style pass logs, claim attempts, queue drain activity, and
transcription scan activity. Then run read-only operational checks:

```bash
cd @@BTQ_PROJECT_ROOT@@
./scripts/btq queue-status
./scripts/btq inspect-runtime
```

If `queue-status` reports growing queue depth while the queue watcher is active,
stop and inspect the failed artifacts before continuing the cutover.

## Hard Safety Rules

- Do not deploy or enable Dell services until runtime root and CouchDB
  connection are explicit.
- Do not start both Mac and Dell queue watchers against the same writable queue
  during cutover.
- Do not print raw CouchDB credentials in shared logs.
- Do not mutate canonical `btq_vault` state from raw intake, uploads, images,
  transcripts, or AI summaries. Only deterministic writer code may mutate
  canonical state.
- Do not change business logic while fixing server launch or environment
  issues.
- Do not repair runtime permissions by making the tree world-writable.
- Stop if model availability, runtime ownership, or the CouchDB connection is
  ambiguous.

## Install Steps

1. Build and verify from the Mac source of truth.

   ```bash
   cd /Users/operator/btq
   project/.venv/bin/python -m pytest tests -q
   project/.venv/bin/python -m pytest project/tests -q
   ./scripts/lint-markdown
   ```

1. Copy or mirror the repo to the Dell source location. Do not copy runtime
   artifacts, local virtualenvs, caches, or `.git` internals unless the operator
   has explicitly chosen that transfer method.

1. Create the service user and runtime directories using the commands above.

1. Install Python dependencies and create the Dell virtualenv at
   `@@BTQ_PYTHON_BIN@@`.

1. Verify Whisper and `qwen2.5vl:7b` model availability.

1. Substitute placeholders in the systemd unit templates. This prompt ships
   templates, not an installer script, so use a reviewed substitution command or
   editor. Example:

   ```bash
   sed \
     -e 's|@@BTQ_PROJECT_ROOT@@|/srv/btq/project|g' \
     -e 's|@@BTQ_PYTHON_BIN@@|/srv/btq/project/.venv/bin/python|g' \
     -e 's|@@BTQ_SERVICE_USER@@|btq|g' \
     -e 's|@@BTQ_SERVICE_GROUP@@|btq|g' \
     -e 's|@@BTQ_RUNTIME_ROOT@@|/srv/btq/runtime|g' \
     -e 's|@@BTQ_LOG_DIR@@|/srv/btq/runtime/logs|g' \
     -e 's|@@BTQ_PATH@@|/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin|g' \
     -e 's|@@BTQ_COUCHDB_URL@@|http://127.0.0.1:5984|g' \
     -e 's|@@BTQ_COUCHDB_USER@@|REDACTED|g' \
     -e 's|@@BTQ_COUCHDB_PASSWORD@@|REDACTED|g' \
     -e 's|@@BTQ_TRANSCRIPTION_WORKER_COUNT_ARGS@@|--worker-count 4|g' \
     project/deploy/systemd/btq-transcription-watch.service \
     > /etc/systemd/system/btq-transcription-watch.service
   ```

   Repeat for `btq-queue-watch.service`. For the queue unit, omit the
   `@@BTQ_TRANSCRIPTION_WORKER_COUNT_ARGS@@` substitution.

1. Restrict substituted unit files if they contain credentials:

   ```bash
   chown root:root /etc/systemd/system/btq-queue-watch.service \
     /etc/systemd/system/btq-transcription-watch.service
   chmod 0640 /etc/systemd/system/btq-queue-watch.service \
     /etc/systemd/system/btq-transcription-watch.service
   ```

1. Load and start the services:

   ```bash
   systemctl daemon-reload
   systemctl enable --now btq-queue-watch.service
   systemctl enable --now btq-transcription-watch.service
   ```

1. Run the health checks above and inspect the runtime tree.

## Rollback

If the Dell cutover fails verification, stop the Dell watchers first:

```bash
systemctl stop btq-transcription-watch.service
systemctl stop btq-queue-watch.service
systemctl disable btq-transcription-watch.service
systemctl disable btq-queue-watch.service
```

Confirm no Dell watcher process remains:

```bash
pgrep -af 'queue_processor.watch|transcription_pipeline.main' || true
```

Then fail back to the Mac watchers:

```bash
cd /Users/operator/btq
./scripts/install-whisper-launch-agent
```

(The file-queue launch agent is retired — the CouchDB queue watcher is the
sole queue processor; install it from
`project/field_capture/launchagents/com.btq.couchdb-queue-watcher.plist`.)

Because the Mac remains the replication peer in the Stage E topology, this is a
real fallback path. After failback, run:

```bash
./scripts/btq queue-status
./scripts/btq inspect-runtime
```

Preserve the Dell runtime tree for diagnosis. Do not delete failed artifacts or
logs until the failure has been understood.
