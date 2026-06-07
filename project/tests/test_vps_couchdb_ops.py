from __future__ import annotations

import contextlib
import os
import subprocess
import unittest
from collections.abc import Iterator
from unittest import mock

from vps import couchdb_ops


class VpsCouchdbOpsTests(unittest.TestCase):
    def test_install_couchdb_all_steps_pass(self) -> None:
        with mock.patch("vps.couchdb_ops.run_ssh", return_value=(0, "")) as run_ssh_mock:
            results = couchdb_ops.install_couchdb("host", runner=mock.Mock())

        self.assertEqual(len(results), 12)
        self.assertTrue(all(code == 0 for _label, code, _output in results))
        self.assertEqual([label for label, _code, _output in results], [label for label, _command in couchdb_ops.INSTALL_STEPS])
        self.assertEqual(run_ssh_mock.call_count, 12)

    def test_install_couchdb_stops_on_failure(self) -> None:
        with mock.patch("vps.couchdb_ops.run_ssh", side_effect=[(0, ""), (0, ""), (1, "apt error")]) as run_ssh_mock:
            results = couchdb_ops.install_couchdb("host", runner=mock.Mock())

        self.assertEqual(len(results), 3)
        self.assertEqual(results[-1], ("apt-get update", 1, "apt error"))
        self.assertEqual(run_ssh_mock.call_count, 3)

    def test_create_couchdb_admin(self) -> None:
        with mock.patch("vps.couchdb_ops.run_ssh", return_value=(0, "")) as run_ssh_mock:
            result = couchdb_ops.create_couchdb_admin("host", "admin", "secret", runner=mock.Mock())

        self.assertEqual(result, (0, ""))
        command = run_ssh_mock.call_args.args[1]
        self.assertIn("_node/_local/_config/admins/admin", command)
        self.assertIn("curl -sf -X PUT", command)

    def test_vps_couchdb_status(self) -> None:
        with mock.patch(
            "vps.couchdb_ops.run_ssh",
            side_effect=[
                (0, "active"),
                (0, "3.3.3"),
                (0, "/dev/sda1  50G  10G  40G 20% /"),
            ],
        ):
            status = couchdb_ops.vps_couchdb_status("host", runner=mock.Mock())

        self.assertEqual(status["service"], "active")
        self.assertEqual(status["version"], "3.3.3")
        self.assertEqual(status["disk"], "/dev/sda1  50G  10G  40G 20% /")

    def test_run_remote_setup_sets_and_restores_env(self) -> None:
        @contextlib.contextmanager
        def fake_tunnel(_remote_host: str) -> Iterator[str]:
            yield "http://127.0.0.1:15984"

        with mock.patch.dict(os.environ, {"BTQ_COUCHDB_URL": "sentinel"}, clear=False):
            with mock.patch("vps.couchdb_ops.couchdb_tunnel", side_effect=fake_tunnel):
                with mock.patch("vps.couchdb_ops.setup_command.run_setup", return_value=0) as setup_mock:
                    result = couchdb_ops.run_remote_setup("host", with_replication=True, skip_migrate=True)

            self.assertEqual(os.environ["BTQ_COUCHDB_URL"], "sentinel")

        self.assertEqual(result, 0)
        setup_mock.assert_called_once_with(with_replication=True, skip_migrate=True)


if __name__ == "__main__":
    unittest.main()
