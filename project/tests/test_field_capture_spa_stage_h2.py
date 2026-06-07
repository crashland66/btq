from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FieldCaptureSPAStageH2Tests(unittest.TestCase):
    def read_asset(self, relative_path: str) -> str:
        return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    def test_success_screen_element_in_html(self) -> None:
        html = self.read_asset("field_capture/public/index.html")
        self.assertIn('id="successScreen"', html)
        self.assertIn('id="successDetail"', html)
        self.assertIn('id="submitAnotherButton"', html)

    def test_submit_summary_element_in_html(self) -> None:
        html = self.read_asset("field_capture/public/index.html")
        self.assertIn('id="submitSummary"', html)

    def test_show_success_screen_function_in_app_js(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("showSuccessScreen", app_js)

    def test_reset_to_form_function_in_app_js(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("resetToForm", app_js)
        self.assertIn("submitAnotherButton", app_js)

    def test_update_submit_summary_function_in_app_js(self) -> None:
        app_js = self.read_asset("field_capture/public/app.js")
        self.assertIn("updateSubmitSummary", app_js)
        self.assertIn("submitSummary", app_js)


if __name__ == "__main__":
    unittest.main()
