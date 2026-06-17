"""Regression: _load_r2_credentials must parse both `key=value` (env-style) and
`key: value` (colon-style) secrets files. The operator's real secrets/cloudflareR2
uses the colon form, which the original env-only parser silently skipped (caught by
the live R2 round-trip smoke for 391)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_store import _load_r2_credentials  # noqa: E402


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "cloudflareR2"
    p.write_text(text, encoding="utf-8")
    return p


def test_colon_style(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, "access_key: AK_colon\nsecret_access_key: SK_colon\n"))
    assert creds == {"access_key": "AK_colon", "secret_access_key": "SK_colon"}


def test_equals_style(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, "access_key=AK_eq\nsecret_access_key=SK_eq\n"))
    assert creds == {"access_key": "AK_eq", "secret_access_key": "SK_eq"}


def test_leftmost_separator_wins_when_value_contains_other(tmp_path: Path) -> None:
    # colon-delimited line whose value contains '=' must not split on the '='
    creds = _load_r2_credentials(_write(tmp_path, "access_key: AB=CD\nsecret_access_key: EF=GH\n"))
    assert creds == {"access_key": "AB=CD", "secret_access_key": "EF=GH"}


def test_absent_file_returns_blanks(tmp_path: Path) -> None:
    creds = _load_r2_credentials(tmp_path / "does-not-exist")
    assert creds == {"access_key": "", "secret_access_key": ""}


def test_quotes_and_comments_tolerated(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, '# r2\naccess_key: "AK_q"\nsecret_access_key = \'SK_q\'\n'))
    assert creds == {"access_key": "AK_q", "secret_access_key": "SK_q"}
