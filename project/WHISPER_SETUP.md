# Whisper Pipeline

This watcher runs `transcription_pipeline.main` in watch mode using the paths defined in the repository `config.json`.

Run it with:

```sh
./scripts/btq-verify-environment
./scripts/whisper-watch
```

Install it as a background login service with:

```sh
./scripts/install-whisper-launch-agent
```

Useful options:

```sh
./scripts/whisper-watch --once
./scripts/whisper-watch --model small
./scripts/whisper-watch --poll-seconds 10
```

Before enabling the background service on a fresh machine, run:

```sh
./scripts/btq-verify-environment
```

What it stores:

- Main log path: configured by `transcription_log_path` in `config.json`
- Claimed audio: `<local_runtime_dir>/claimed/audio/`
- Completed audio: configured by `audio_archive_dir` in `config.json`
- Failed audio: `<local_runtime_dir>/failed/audio/`
- Generated queue jobs: atomically staged into `<local_runtime_dir>/queue/` for `queue_processor.watch`
- `launchd` stdout: configured by `whisper_launchd_stdout_log` in `config.json`
- `launchd` stderr: configured by `whisper_launchd_stderr_log` in `config.json`

Worker lifecycle:

- `transcription_pipeline.main` stays as the lightweight watcher/parent.
- For each active transcription job, the parent starts `python -m transcription_pipeline.worker`.
- The worker loads Whisper, transcribes the claimed audio, writes a serialized transcript result, and exits.
- Process teardown is intentional. Whisper/PyTorch allocations can remain resident in a long-running Python process even after deleting model objects, especially with large Apple Silicon workloads. Letting the worker process exit gives macOS a reliable boundary for reclaiming model memory and keeps idle watcher RAM low.
- Stable files are handled sequentially for now. If multiple audio files arrive together, the watcher claims and processes them one at a time instead of starting a worker pool.

Logging:

- Parent log records worker start, worker exit code, worker wall time, captured worker stdout/stderr, and parent max RSS when available.
- Worker log records process start, model load duration, transcription duration, successful output write, exit status, and max RSS when available.

Notes:

- The transcription watcher stages queue jobs only; the queue watcher is responsible for draining runtime queue jobs and writing canonical `btq_vault` state.
- If transcription-side processing fails after claim, the local audio is moved to failed storage for manual inspection.
- Legacy `<audio>.<ext>.processed` sidecars are treated as skip markers and archived with the matching audio so stale markers are not left in the inbox.
- The background `launchd` job includes the configured `ffmpeg_path_prefix` plus the standard macOS shell path.

Future optimization paths:

- Warm worker mode: keep a worker alive briefly while the inbox is actively draining, then exit after an idle timeout.
- Adaptive worker persistence: use recent arrival rate or backlog size to decide whether to reuse a loaded model for another file.
- Batching: send a claimed batch to one worker so the model loads once for several files.
- Worker pools: run multiple isolated workers only if throughput becomes more important than RAM pressure.
