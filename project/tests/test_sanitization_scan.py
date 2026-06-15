"""Gating test for 382 — the public-repo sanitization scan (the leak gate).

Drives scripts/sanitization-scan via --stdin: real leaks reject, the sandbox identity
+ public URL + config-key-names pass (no false positives), inline exemption works, and
the scan is self-clean (doesn't trip on its own pattern strings).
"""

import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN = REPO / "scripts" / "sanitization-scan"


def scan(content: str) -> int:
    return subprocess.run(
        [str(SCAN), "--stdin", "--source-name", "test"],
        input=content, text=True, capture_output=True,
    ).returncode


class SanitizationScanTest(unittest.TestCase):
    def test_rejects_real_leaks(self) -> None:
        # Each fixture is a leak-shaped string; the trailing comment exempts the
        # source line from the scanner (these are deliberate fixtures), while the
        # string value passed to scan() has no such comment, so it still rejects.
        leaks = [
            'x = "/Users/jdoe/secret/file.json"',  # sanitization-ok: scanner gate test fixture
            'TOKEN = "fc_EXAMPLEexampleEXAMPLEexample00"',  # sanitization-ok: scanner gate test fixture
            'host = "bt-gateway-01.deer-fujita.ts.net"',  # sanitization-ok: scanner gate test fixture
            'peer = "100.124.107.7"',  # sanitization-ok: scanner gate test fixture
        ]
        for content in leaks:
            self.assertNotEqual(scan(content), 0, content)

    def test_passes_clean_and_sandbox_content(self) -> None:
        for label, content in {
            "sandbox identity": 'site_id = "SANDBOX"  # Sandy Sandbox / Sandbox Site',
            "public url": 'URL = "https://fc.gregstoltz.com/api/session"',
            "config key name": 'cfg = data.get("couchdb_password")',
        }.items():
            self.assertEqual(scan(content), 0, f"should pass (no false positive): {label}")

    def test_inline_exemption(self) -> None:
        self.assertEqual(scan('p = "/Users/jdoe/x"  # sanitization-ok: doc example'), 0)

    def test_scan_is_self_clean(self) -> None:
        for f in ("SANITIZATION.md", "scripts/sanitization-scan",
                  "scripts/sanitization-allow.txt", ".github/workflows/sanitization.yml"):
            self.assertEqual(scan((REPO / f).read_text(encoding="utf-8")), 0,
                             f"scan must not trip on its own file: {f}")


if __name__ == "__main__":
    unittest.main()
