from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "btq-clean-local"


def run_clean_local(tmp_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["BTQ_CLEAN_ROOT"] = str(tmp_root)
    return subprocess.run(
        [str(SCRIPT), *args],
        check=True,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_clean_local_dry_run_deletes_nothing(tmp_path: Path) -> None:
    pycache = tmp_path / "pkg" / "__pycache__"
    pycache.mkdir(parents=True)
    pyc = pycache / "x.pyc"
    pyc.write_bytes(b"compiled")
    ds_store = tmp_path / ".DS_Store"
    ds_store.write_bytes(b"finder")

    result = run_clean_local(tmp_path, "--dry-run")

    assert pyc.exists()
    assert ds_store.exists()
    assert "__pycache__" in result.stdout
    assert "x.pyc" in result.stdout
    assert ".DS_Store" in result.stdout


def test_clean_local_apply_removes_caches(tmp_path: Path) -> None:
    pycache = tmp_path / "pkg" / "__pycache__"
    pycache.mkdir(parents=True)
    pyc = pycache / "x.pyc"
    pyc.write_bytes(b"compiled")
    ds_store = tmp_path / ".DS_Store"
    ds_store.write_bytes(b"finder")
    kept = tmp_path / "tracked-looking.py"
    kept.write_text("print('keep')\n", encoding="utf-8")

    run_clean_local(tmp_path, "--apply")

    assert not pycache.exists()
    assert not pyc.exists()
    assert not ds_store.exists()
    assert kept.exists()


def test_clean_local_apply_without_venv_flag_keeps_venv(tmp_path: Path) -> None:
    venv = tmp_path / "project" / ".venv"
    venv.mkdir(parents=True)
    marker = venv / "marker"
    marker.write_text("keep\n", encoding="utf-8")

    run_clean_local(tmp_path, "--apply")

    assert marker.exists()
