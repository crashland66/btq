from __future__ import annotations

import configparser
from pathlib import Path


SYSTEMD_DIR = Path("project/deploy/systemd")
QUEUE_UNIT = SYSTEMD_DIR / "btq-queue-watch.service"
TRANSCRIPTION_UNIT = SYSTEMD_DIR / "btq-transcription-watch.service"
UNIT_PATHS = (QUEUE_UNIT, TRANSCRIPTION_UNIT)


def load_unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def test_systemd_unit_files_exist() -> None:
    for path in UNIT_PATHS:
        assert path.exists()


def test_systemd_units_have_required_sections() -> None:
    for path in UNIT_PATHS:
        unit = load_unit(path)
        assert unit.has_section("Unit")
        assert unit.has_section("Service")
        assert unit.has_section("Install")
        assert unit["Service"]["ExecStart"].strip()
        assert unit["Service"]["Restart"].strip()


def test_systemd_units_use_placeholders_not_absolute_paths() -> None:
    for path in UNIT_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text
        assert "@@BTQ_PROJECT_ROOT@@" in text
        assert "@@BTQ_PYTHON_BIN@@" in text


def test_systemd_queue_unit_runs_queue_watch_module() -> None:
    unit = load_unit(QUEUE_UNIT)
    assert "-m queue_processor.watch" in unit["Service"]["ExecStart"]


def test_systemd_transcription_unit_runs_transcription_module() -> None:
    unit = load_unit(TRANSCRIPTION_UNIT)
    assert "-m transcription_pipeline.main" in unit["Service"]["ExecStart"]
