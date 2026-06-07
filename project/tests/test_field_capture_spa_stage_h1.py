from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FieldCaptureSPAStageH1Tests(unittest.TestCase):
    def read_asset(self, relative_path: str) -> str:
        return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    def test_camera_input_has_capture_environment(self) -> None:
        html = self.read_asset("field_capture/public/index.html")
        self.assertIn('id="cameraInput"', html)
        self.assertIn('capture="environment"', html)
        self.assertIn('id="fileInput"', html)

    def test_camera_inputs_in_photo_button_row(self) -> None:
        html = self.read_asset("field_capture/public/index.html")
        self.assertIn('class="photo-button-row"', html)
        self.assertIn("cameraInput", html)
        self.assertIn("fileInput", html)

    def test_recent_site_key_constant_defined(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("RECENT_SITE_KEY", app_js)

    def test_recent_site_persisted_on_site_change(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("localStorage.setItem", app_js)
        self.assertIn("RECENT_SITE_KEY", app_js)

    def test_recent_site_read_in_render_sites(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("localStorage.getItem", app_js)
        self.assertIn("RECENT_SITE_KEY", app_js)


if __name__ == "__main__":
    unittest.main()
