"""INDEPENDENT VERIFIER gating tests for prompt 397a — bucket-aware R2 credential
selection (secret prefix; local default unchanged).

Authored by the verifier (not the executor). Covers the contract:

* no-prefix parsing is byte-identical to base (bare keys; absence -> blanks; both
  ``:`` and ``=`` styles; quotes/``export`` handled);
* ``key_prefix="btq"`` against a TWO-BLOCK fixture returns the btq pair, NEVER the
  cat-demo (last) block;
* prefix set but prefixed keys absent -> falls back to bare keys;
* prefix set but only ONE of the prefixed pair present -> falls back to bare
  (incomplete pair is not honored);
* whitespace / lone-underscore prefix is treated as "no prefix";
* ``InstanceConfig.r2_secret_prefix`` resolves env ``BTQ_R2_SECRET_PREFIX`` >
  config ``r2.secret_prefix`` > ``""``;
* ``get_media_store("s3")`` threads the configured prefix into the secrets-file
  fallback while explicit config/env creds still win, and incomplete-config still
  errors clearly.

All credential values are SYNTHETIC fakes. No real R2 secret/endpoint/account is
read or written.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import instance_config  # noqa: E402
import media_store  # noqa: E402
from media_store import (  # noqa: E402
    MediaStoreConfigError,
    S3Store,
    _load_r2_credentials,
    get_media_store,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.json"

# Synthetic two-block fixture: btq production block + cat-demo canary block, with
# account-level keys shared/unprefixed. cat-demo deliberately appears LAST so a
# flat last-wins parser (the bug) would return CAT_FAKE_*.
TWO_BLOCK_COLON = (
    "account_id: ACCT_FAKE\n"
    "s3_api: https://acct-fake.r2.cloudflarestorage.invalid\n"
    "btq_access_key: BTQ_FAKE_AK\n"
    "btq_secret_access_key: BTQ_FAKE_SK\n"
    "cat_demo_access_key: CAT_FAKE_AK\n"
    "cat_demo_secret_access_key: CAT_FAKE_SK\n"
)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "cloudflareR2"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# No-prefix parsing is byte-identical to base behavior.
# --------------------------------------------------------------------------- #
def test_no_prefix_colon_style_bare_keys(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, "access_key: AK_c\nsecret_access_key: SK_c\n"))
    assert creds == {"access_key": "AK_c", "secret_access_key": "SK_c"}


def test_no_prefix_equals_style_bare_keys(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, "access_key=AK_e\nsecret_access_key=SK_e\n"))
    assert creds == {"access_key": "AK_e", "secret_access_key": "SK_e"}


def test_no_prefix_quotes_export_comments_handled(tmp_path: Path) -> None:
    text = (
        "# r2 creds\n"
        "; semicolon comment\n"
        "[block]\n"
        'export access_key: "AK_q"\n'
        "export secret_access_key = 'SK_q'\n"
    )
    creds = _load_r2_credentials(_write(tmp_path, text))
    assert creds == {"access_key": "AK_q", "secret_access_key": "SK_q"}


def test_no_prefix_absent_file_returns_blanks(tmp_path: Path) -> None:
    assert _load_r2_credentials(tmp_path / "nope") == {"access_key": "", "secret_access_key": ""}


def test_no_prefix_on_two_block_file_returns_bare_or_blank(tmp_path: Path) -> None:
    # The two-block file has NO bare access_key/secret_access_key, only prefixed.
    # With no prefix the parser must NOT silently leak a prefixed block -> blanks.
    creds = _load_r2_credentials(_write(tmp_path, TWO_BLOCK_COLON))
    assert creds == {"access_key": "", "secret_access_key": ""}


# --------------------------------------------------------------------------- #
# THE CORE FIX: prefix selects the named block, never the last block.
# --------------------------------------------------------------------------- #
def test_prefix_btq_selects_btq_block_not_cat_demo(tmp_path: Path) -> None:
    creds = _load_r2_credentials(_write(tmp_path, TWO_BLOCK_COLON), key_prefix="btq")
    assert creds == {"access_key": "BTQ_FAKE_AK", "secret_access_key": "BTQ_FAKE_SK"}
    # Hard guard: the cat-demo (last) block must NEVER surface for prefix=btq.
    assert creds["access_key"] != "CAT_FAKE_AK"
    assert creds["secret_access_key"] != "CAT_FAKE_SK"


def test_prefix_cat_demo_selects_cat_demo_block(tmp_path: Path) -> None:
    # Symmetry: the selector must key off the prefix, not file order.
    creds = _load_r2_credentials(_write(tmp_path, TWO_BLOCK_COLON), key_prefix="cat_demo")
    assert creds == {"access_key": "CAT_FAKE_AK", "secret_access_key": "CAT_FAKE_SK"}


def test_prefix_equals_style_two_block(tmp_path: Path) -> None:
    text = (
        "btq_access_key=BTQ_E_AK\n"
        "btq_secret_access_key=BTQ_E_SK\n"
        "cat_demo_access_key=CAT_E_AK\n"
        "cat_demo_secret_access_key=CAT_E_SK\n"
    )
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="btq")
    assert creds == {"access_key": "BTQ_E_AK", "secret_access_key": "BTQ_E_SK"}


# --------------------------------------------------------------------------- #
# Fallback paths: prefix set but prefixed keys absent/incomplete -> bare keys.
# --------------------------------------------------------------------------- #
def test_prefix_set_but_prefixed_keys_absent_falls_back_to_bare(tmp_path: Path) -> None:
    text = "access_key: BARE_AK\nsecret_access_key: BARE_SK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="btq")
    assert creds == {"access_key": "BARE_AK", "secret_access_key": "BARE_SK"}


def test_prefix_set_only_access_present_falls_back_to_bare(tmp_path: Path) -> None:
    # Incomplete prefixed pair (only the access key) must NOT be used.
    text = "btq_access_key: BTQ_ONLY_AK\naccess_key: BARE_AK\nsecret_access_key: BARE_SK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="btq")
    assert creds == {"access_key": "BARE_AK", "secret_access_key": "BARE_SK"}


def test_prefix_set_only_secret_present_falls_back_to_bare(tmp_path: Path) -> None:
    text = "btq_secret_access_key: BTQ_ONLY_SK\naccess_key: BARE_AK\nsecret_access_key: BARE_SK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="btq")
    assert creds == {"access_key": "BARE_AK", "secret_access_key": "BARE_SK"}


def test_prefix_set_incomplete_and_no_bare_returns_blanks(tmp_path: Path) -> None:
    # Incomplete prefixed pair AND no bare keys -> blanks (no leak of partial pair).
    text = "btq_access_key: BTQ_ONLY_AK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="btq")
    assert creds == {"access_key": "", "secret_access_key": ""}


def test_whitespace_prefix_treated_as_no_prefix(tmp_path: Path) -> None:
    text = "btq_access_key: BTQ\nbtq_secret_access_key: BTQ_S\naccess_key: BARE_AK\nsecret_access_key: BARE_SK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="   ")
    assert creds == {"access_key": "BARE_AK", "secret_access_key": "BARE_SK"}


def test_lone_underscore_prefix_treated_as_no_prefix(tmp_path: Path) -> None:
    text = "btq_access_key: BTQ\nbtq_secret_access_key: BTQ_S\naccess_key: BARE_AK\nsecret_access_key: BARE_SK\n"
    creds = _load_r2_credentials(_write(tmp_path, text), key_prefix="_")
    assert creds == {"access_key": "BARE_AK", "secret_access_key": "BARE_SK"}


# --------------------------------------------------------------------------- #
# InstanceConfig.r2_secret_prefix resolution: env > config > "".
# --------------------------------------------------------------------------- #
def test_secret_prefix_defaults_blank_on_clean_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BTQ_R2_SECRET_PREFIX", raising=False)
    inst = instance_config.load_instance_config(config_path=EXAMPLE_CONFIG)
    assert inst.r2_secret_prefix == ""


def test_secret_prefix_from_config_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BTQ_R2_SECRET_PREFIX", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text('{"instance": {"r2": {"secret_prefix": "btq"}}}', encoding="utf-8")
    inst = instance_config.load_instance_config(config_path=cfg)
    assert inst.r2_secret_prefix == "btq"


def test_secret_prefix_env_overrides_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BTQ_R2_SECRET_PREFIX", "env_wins")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"instance": {"r2": {"secret_prefix": "config_loses"}}}', encoding="utf-8")
    inst = instance_config.load_instance_config(config_path=cfg)
    assert inst.r2_secret_prefix == "env_wins"


def test_secret_prefix_missing_config_key_defaults_blank(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"instance": {"r2": {"bucket": "b"}}}', encoding="utf-8")
    inst = instance_config.load_instance_config(config_path=cfg)
    assert inst.r2_secret_prefix == ""


# --------------------------------------------------------------------------- #
# get_media_store s3 branch threads the prefix; config/env creds still win.
# --------------------------------------------------------------------------- #
class _MockBoto3:
    def __enter__(self):
        self.client = mock.MagicMock(name="s3_client")
        self.boto3 = mock.MagicMock(name="boto3")
        self.boto3.client.return_value = self.client
        botocore = mock.MagicMock(name="botocore")
        botocore.exceptions = mock.MagicMock(name="botocore.exceptions")
        botocore.exceptions.ClientError = type("ClientError", (Exception,), {})
        self._patcher = mock.patch.dict(
            sys.modules,
            {"boto3": self.boto3, "botocore": botocore, "botocore.exceptions": botocore.exceptions},
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


def _s3_cfg(**over: object) -> SimpleNamespace:
    base = dict(
        media_store="s3",
        r2_endpoint_url="https://acct.r2.invalid",
        r2_bucket="bkt",
        r2_region="auto",
        r2_access_key_id="",
        r2_secret_access_key="",
        r2_secret_prefix="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_s3_threads_configured_prefix_to_secrets_loader() -> None:
    with _MockBoto3() as mb:
        cfg = _s3_cfg(r2_secret_prefix="btq")
        with mock.patch.object(
            media_store,
            "_load_r2_credentials",
            return_value={"access_key": "BTQ_FAKE_AK", "secret_access_key": "BTQ_FAKE_SK"},
        ) as loader:
            store = get_media_store(Path("/tmp"), instance_config=cfg)
        assert isinstance(store, S3Store)
        # The configured prefix must be passed through to the secrets loader.
        _, kwargs = loader.call_args
        assert kwargs.get("key_prefix") == "btq"
        _, ckwargs = mb.boto3.client.call_args
        assert ckwargs["aws_access_key_id"] == "BTQ_FAKE_AK"
        assert ckwargs["aws_secret_access_key"] == "BTQ_FAKE_SK"


def test_s3_blank_prefix_threads_empty_string() -> None:
    with _MockBoto3():
        cfg = _s3_cfg(r2_secret_prefix="")
        with mock.patch.object(
            media_store,
            "_load_r2_credentials",
            return_value={"access_key": "AK", "secret_access_key": "SK"},
        ) as loader:
            get_media_store(Path("/tmp"), instance_config=cfg)
        _, kwargs = loader.call_args
        assert kwargs.get("key_prefix") == ""


def test_s3_explicit_config_creds_win_over_secrets_prefix() -> None:
    with _MockBoto3() as mb:
        cfg = _s3_cfg(
            r2_access_key_id="CFG_AK",
            r2_secret_access_key="CFG_SK",
            r2_secret_prefix="btq",
        )
        # If config creds are honored, the secrets loader must not be touched.
        with mock.patch.object(
            media_store,
            "_load_r2_credentials",
            side_effect=AssertionError("must not read secrets when config creds set"),
        ):
            store = get_media_store(Path("/tmp"), instance_config=cfg)
        assert isinstance(store, S3Store)
        _, ckwargs = mb.boto3.client.call_args
        assert ckwargs["aws_access_key_id"] == "CFG_AK"
        assert ckwargs["aws_secret_access_key"] == "CFG_SK"


def test_s3_incomplete_config_still_errors_clearly_with_prefix() -> None:
    cfg = _s3_cfg(r2_secret_prefix="btq")
    # Prefix set but the (mocked) secrets file resolves to blanks -> clear error.
    with mock.patch.object(
        media_store,
        "_load_r2_credentials",
        return_value={"access_key": "", "secret_access_key": ""},
    ):
        with pytest.raises(MediaStoreConfigError):
            get_media_store(Path("/tmp"), instance_config=cfg)


def test_s3_end_to_end_two_block_secrets_file_selects_btq(tmp_path: Path) -> None:
    # Full path through get_media_store with a REAL synthetic two-block secrets
    # file (not a mocked loader): prefix=btq must produce the btq creds.
    secrets = _write(tmp_path, TWO_BLOCK_COLON)
    with _MockBoto3() as mb:
        cfg = _s3_cfg(r2_secret_prefix="btq")
        with mock.patch.object(
            media_store,
            "_load_r2_credentials",
            wraps=lambda secrets_path=secrets, key_prefix="": _load_r2_credentials(
                secrets, key_prefix=key_prefix
            ),
        ):
            get_media_store(Path("/tmp"), instance_config=cfg)
        _, ckwargs = mb.boto3.client.call_args
        assert ckwargs["aws_access_key_id"] == "BTQ_FAKE_AK"
        assert ckwargs["aws_secret_access_key"] == "BTQ_FAKE_SK"
        assert ckwargs["aws_access_key_id"] != "CAT_FAKE_AK"
