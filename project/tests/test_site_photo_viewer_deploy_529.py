"""Independent gates for the four-tree deploy integration (prompt 529).

Source-contract assertions on the two deploy scripts. The live proof is the
deploy itself (the aggregate script's drift report); these gates pin the
structural invariants so a later edit cannot silently drop the fourth tree.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AGGREGATE = REPO_ROOT / "scripts" / "deploy-all-apps-on-vps"
VIEWER_SCRIPT = REPO_ROOT / "scripts" / "deploy-site-photo-viewer-on-vps"


def _aggregate_text() -> str:
    return AGGREGATE.read_text()


def test_both_scripts_parse_and_are_executable() -> None:
    for script in (AGGREGATE, VIEWER_SCRIPT):
        subprocess.run(["bash", "-n", str(script)], check=True)
        assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_viewer_is_a_first_class_app_everywhere() -> None:
    text = _aggregate_text()
    # dir + service mappings
    assert 'viewer) echo "${VIEWER_APP_DIR}"' in text
    assert 'viewer) echo "${VIEWER_SERVICE}"' in text
    # dispatch + validation
    assert "viewer) deploy_viewer ;;" in text
    assert "viewer|fc|photos|admin)" in text
    # drift loop covers all four
    assert re.search(r"for app in viewer fc photos admin; do", text)


def test_viewer_health_signature_is_checked_on_loopback() -> None:
    text = _aggregate_text()
    assert "127.0.0.1:8084/api/health" in text
    assert '"app": "site_photo_viewer"' in text


def test_default_order_proves_release_on_viewer_first_photos_last() -> None:
    text = _aggregate_text()
    match = re.search(r"APPS=\((viewer[^)]+)\)", text)
    assert match is not None
    order = match.group(1).split()
    assert order[0] == "viewer"
    assert order[-1] == "photos"


def test_viewer_deploy_delegates_to_dedicated_script_and_stamps() -> None:
    text = _aggregate_text()
    body = text.split("deploy_viewer() {", 1)[1].split("\n}", 1)[0]
    assert "deploy-site-photo-viewer-on-vps" in body
    assert "write_stamp viewer" in body


def test_viewer_public_url_comes_from_machine_local_deploy_env() -> None:
    text = _aggregate_text()
    assert 'VIEWER_PUBLIC_URL="${SITE_PHOTO_VIEWER_PUBLIC_URL:-}"' in text


def test_dedicated_script_smokes_public_unauthenticated_401() -> None:
    text = VIEWER_SCRIPT.read_text()
    assert "401" in text
    assert "SITE_PHOTO_VIEWER_PUBLIC_URL" in text
    assert "systemctl" in text or "SYSTEMCTL" in text
