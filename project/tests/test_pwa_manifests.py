from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_PASTE_CONTEXT = (
    "If you don't have an access token, this is a demo of an internal field-operations "
    "tool. Real users get a tokenized link from their administrator."
)


class PWAManifestTests(unittest.TestCase):
    def load_manifest(self, relative_path: str) -> dict[str, object]:
        path = PROJECT_ROOT / relative_path
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_has_maskable_icon(self, manifest: dict[str, object]) -> None:
        icons = manifest.get("icons")
        self.assertIsInstance(icons, list)
        self.assertTrue(
            any(isinstance(icon, dict) and icon.get("purpose") == "maskable" and icon.get("src") == "/assets/icon-512-maskable.png" for icon in icons),
            "manifest must include the Android maskable 512 icon",
        )

    def assert_robots_disallow_all(self, relative_path: str) -> None:
        robots = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        self.assertEqual(robots, "User-agent: *\nDisallow: /\n")

    def assert_has_noindex_meta(self, relative_path: str) -> None:
        html = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        self.assertIn('<meta name="robots" content="noindex, nofollow"', html)

    def test_field_capture_manifest_is_valid_and_maskable(self) -> None:
        manifest = self.load_manifest("field_capture/public/manifest.webmanifest")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assert_has_maskable_icon(manifest)

    def test_voice_memo_manifest_is_valid_and_maskable(self) -> None:
        manifest = self.load_manifest("voice_memo/public/manifest.webmanifest")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assert_has_maskable_icon(manifest)

    def test_pwa_robots_files_disallow_all(self) -> None:
        self.assert_robots_disallow_all("field_capture/public/robots.txt")
        self.assert_robots_disallow_all("voice_memo/public/robots.txt")

    def test_pwa_index_pages_are_noindex(self) -> None:
        self.assert_has_noindex_meta("field_capture/public/index.html")
        self.assert_has_noindex_meta("voice_memo/public/index.html")

    def test_token_paste_context_is_identical_across_pwas(self) -> None:
        field_app = (PROJECT_ROOT / "field_capture/public/app.js").read_text(encoding="utf-8")
        voice_app = (PROJECT_ROOT / "voice_memo/public/app.js").read_text(encoding="utf-8")
        self.assertEqual(field_app.count(TOKEN_PASTE_CONTEXT), 1)
        self.assertEqual(voice_app.count(TOKEN_PASTE_CONTEXT), 1)

    def test_voice_memo_recorder_supports_pause_resume(self) -> None:
        app_js = (PROJECT_ROOT / "voice_memo/public/app.js").read_text(encoding="utf-8")

        self.assertIn("state.recorder.pause()", app_js)
        self.assertIn("state.recorder.resume()", app_js)
        self.assertIn("state.recorder?.state === 'paused'", app_js)
        self.assertIn("audioElapsedBeforePause", app_js)


if __name__ == "__main__":
    unittest.main()
