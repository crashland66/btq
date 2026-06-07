from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from vps import ssh


class VpsSshTests(unittest.TestCase):
    def test_run_ssh_success(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="ok\n", stderr=""))

        result = ssh.run_ssh("host", "command", runner=runner)

        self.assertEqual(result, (0, "ok"))
        runner.assert_called_once_with(["ssh", "host", "command"])

    def test_run_ssh_prefers_stderr(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], 1, stdout="out\n", stderr="err\n"))

        result = ssh.run_ssh("host", "command", runner=runner)

        self.assertEqual(result, (1, "err"))

    def test_run_ssh_use_sudo(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr=""))

        ssh.run_ssh("host", "command", runner=runner, use_sudo=True)

        self.assertEqual(runner.call_args.args[0], ["ssh", "host", "sudo command"])

    def test_couchdb_tunnel_opens_and_closes(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None
        connection = mock.Mock()
        connection.__enter__ = mock.Mock(return_value=connection)
        connection.__exit__ = mock.Mock(return_value=None)

        with mock.patch("vps.ssh.subprocess.Popen", return_value=proc) as popen_mock:
            with mock.patch("vps.ssh.socket.create_connection", return_value=connection):
                with ssh.couchdb_tunnel("host") as url:
                    self.assertEqual(url, "http://127.0.0.1:15984")

        popen_args = popen_mock.call_args.args[0]
        self.assertIn("-L", popen_args)
        self.assertIn("15984:127.0.0.1:5984", popen_args)
        proc.terminate.assert_called_once()

    def test_couchdb_tunnel_raises_if_not_ready(self) -> None:
        proc = mock.Mock()
        proc.poll.return_value = None

        with mock.patch("vps.ssh.subprocess.Popen", return_value=proc):
            with mock.patch("vps.ssh.socket.create_connection", side_effect=ConnectionRefusedError):
                with mock.patch("vps.ssh.time.sleep", return_value=None):
                    with mock.patch("vps.ssh.time.monotonic", side_effect=[0, 0, 11]):
                        with self.assertRaises(ssh.VpsError):
                            with ssh.couchdb_tunnel("host"):
                                pass

        proc.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
