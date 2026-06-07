from __future__ import annotations

from btq_vault import projection_watcher


def test_main_requires_projection_dir(monkeypatch, capsys) -> None:
    monkeypatch.delenv(projection_watcher.DEFAULT_OUTPUT_DIR_ENV, raising=False)

    assert projection_watcher.main() != 0

    captured = capsys.readouterr()
    assert projection_watcher.DEFAULT_OUTPUT_DIR_ENV in captured.err
