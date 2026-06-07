from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Callable, Iterator


DEFAULT_REMOTE_HOST = os.environ.get("BTQ_VPS_SSH_TARGET", "deploy@vps.example.com")
DEFAULT_COUCHDB_TUNNEL_PORT = 15984
COUCHDB_REMOTE_PORT = 5984


class VpsError(Exception):
    pass


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def run_ssh(
    remote_host: str,
    command: str,
    *,
    runner: CommandRunner = run_command,
    use_sudo: bool = False,
) -> tuple[int, str]:
    remote_command = f"sudo {command}" if use_sudo else command
    result = runner(["ssh", remote_host, remote_command])
    output = (result.stderr or "").strip() or (result.stdout or "").strip()
    return result.returncode, output


@contextlib.contextmanager
def couchdb_tunnel(remote_host: str, local_port: int = DEFAULT_COUCHDB_TUNNEL_PORT) -> Iterator[str]:
    proc = subprocess.Popen(
        [
            "ssh",
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=10",
            "-L",
            f"{local_port}:127.0.0.1:{COUCHDB_REMOTE_PORT}",
            remote_host,
        ]
    )
    ready = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                ready = True
                break
        except OSError:
            time.sleep(0.5)
    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        raise VpsError(f"CouchDB SSH tunnel to {remote_host} did not become ready on port {local_port}")

    try:
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
