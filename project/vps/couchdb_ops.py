from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib import parse

from event_pipeline.couchdb import setup_command
from vps.ssh import CommandRunner, couchdb_tunnel, run_command, run_ssh


INSTALL_STEPS = [
    (
        "add signing key",
        "curl -fsSL https://couchdb.apache.org/repo/keys.asc | sudo gpg --batch --no-tty --yes --dearmor -o /usr/share/keyrings/couchdb-archive-keyring.gpg",
    ),
    (
        "add apt repository",
        'echo "deb [signed-by=/usr/share/keyrings/couchdb-archive-keyring.gpg] https://apache.jfrog.io/artifactory/couchdb-deb/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/couchdb.list',
    ),
    ("apt-get update", "sudo apt-get update -qq"),
    (
        "purge partial install",
        "sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y couchdb 2>/dev/null; true",
    ),
    ("preseed standalone mode", 'echo "couchdb couchdb/mode select standalone" | sudo debconf-set-selections'),
    ("preseed bind address", 'echo "couchdb couchdb/bindaddress string 127.0.0.1" | sudo debconf-set-selections'),
    ("preseed cookie", "echo 'couchdb couchdb/cookie string btqcouchdbcookie' | sudo debconf-set-selections"),
    ("preseed nodename", "echo 'couchdb couchdb/nodename string couchdb@127.0.0.1' | sudo debconf-set-selections"),
    ("apt-get install couchdb", "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y couchdb"),
    ("enable service", "sudo systemctl enable couchdb"),
    ("start service", "sudo systemctl start couchdb"),
    (
        "wait for couchdb",
        "for i in $(seq 1 30); do curl -sf http://127.0.0.1:5984/ >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1",
    ),
]


def install_couchdb(remote_host: str, *, runner: CommandRunner = run_command) -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    for label, command in INSTALL_STEPS:
        returncode, output = run_ssh(remote_host, command, runner=runner, use_sudo=False)
        results.append((label, returncode, output))
        if returncode != 0:
            break
    return results


def create_couchdb_admin(
    remote_host: str,
    admin_user: str,
    admin_password: str,
    *,
    runner: CommandRunner = run_command,
) -> tuple[int, str]:
    user = parse.quote(admin_user, safe="")
    password_json = json.dumps(admin_password)
    command = f"curl -sf -X PUT http://127.0.0.1:5984/_node/_local/_config/admins/{user} -d {password_json!r}"
    return run_ssh(remote_host, command, runner=runner, use_sudo=False)


def vps_couchdb_status(remote_host: str, *, runner: CommandRunner = run_command) -> dict[str, str]:
    service_code, service = run_ssh(remote_host, "systemctl is-active couchdb", runner=runner)
    version_code, version = run_ssh(
        remote_host,
        'curl -sf http://127.0.0.1:5984/ | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get(\'version\',\'unknown\'))"',
        runner=runner,
    )
    disk_code, disk = run_ssh(remote_host, "df -h / | tail -1", runner=runner)
    return {
        "service": service if service_code == 0 and service else "unavailable",
        "version": version if version_code == 0 and version else "unavailable",
        "disk": disk if disk_code == 0 and disk else "unavailable",
    }


@contextmanager
def temporary_env(name: str, value: str) -> Iterator[None]:
    original = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original


def run_remote_setup(remote_host: str, *, with_replication: bool = False, skip_migrate: bool = False) -> int:
    with couchdb_tunnel(remote_host) as tunnel_url:
        with temporary_env("BTQ_COUCHDB_URL", tunnel_url):
            return setup_command.run_setup(
                with_replication=with_replication,
                skip_migrate=skip_migrate,
            )
