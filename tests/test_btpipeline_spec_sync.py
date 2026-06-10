"""Drift guard: the human-facing queue authoring spec must stay in sync with
``queue_spec.py`` (the executable contract).

``project/docs/queue_authoring_guide.md`` is the canonical human/AI authoring
reference. ``BTpipeline/specs/QUEUE_JOB_SPEC.md`` (iCloud) is a generated
mirror of it, produced by ``scripts/sync_btpipeline_spec.sh``. The guide has
drifted from ``queue_spec.py`` before (the documented job-type list once
claimed "7" while the contract exposed ~30), which is exactly what these tests
prevent. CLAUDE.md asserts these "must remain in sync"; this is the
enforcement.

The guard checks, against ``ALLOWED_JOB_TYPES`` / ``JOB_SCHEMAS`` as the source
of truth:

1. **Forward coverage** — every executable job type has a documentation
   section in the guide.
2. **Reverse coverage** — no job-type-looking heading documents a type that no
   longer exists in ``ALLOWED_JOB_TYPES`` (catches a removed type left behind
   in the docs).
3. **Required fields** — every required field in ``JOB_SCHEMAS[type]`` is named
   in that type's section (catches a field added in code but never documented).
4. **Mirror** — the sync script reproduces the guide byte-for-byte under the
   banner, so the iCloud mirror cannot silently diverge from the repo source.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from queue_spec import ALLOWED_JOB_TYPES, JOB_SCHEMAS


REPO_ROOT = Path(__file__).resolve().parents[1]
AUTHORING_GUIDE = REPO_ROOT / "project" / "docs" / "queue_authoring_guide.md"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_btpipeline_spec.sh"
BANNER = (
    "> **This file is a generated mirror of "
    "`project/docs/queue_authoring_guide.md` in the BT-Pipeline repository. "
    "Do not hand-edit. Updates land via `scripts/sync_btpipeline_spec.sh`.**"
)

# A job type is documented either by its own ``##``/``###`` heading or, for the
# status-transition family, by a row in the "Status-transition jobs" tables.
_STATUS_TRANSITION_PREFIX = ("mark_supply_", "mark_equipment_", "mark_issue_")


def _guide_text() -> str:
    return AUTHORING_GUIDE.read_text(encoding="utf-8")


def _has_job_type_heading(text: str, job_type: str) -> bool:
    return re.search(rf"^#{{2,4}}[^\n]*`{re.escape(job_type)}`", text, re.MULTILINE) is not None


def _status_transition_section(text: str) -> str:
    match = re.search(
        r"^## Status-transition jobs\n(?P<section>.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("section") if match else ""


def _has_status_transition_documentation(text: str, job_type: str) -> bool:
    return f"| `{job_type}` |" in _status_transition_section(text)


def _section_for(text: str, job_type: str) -> str:
    """Return the documentation text scoped to a single job type.

    For a type with its own heading, the section runs from that heading to the
    next heading of the same-or-higher level. For the status-transition family
    (no per-type heading), the shared "Status-transition jobs" section is used.
    """
    heading = re.search(
        rf"^(?P<hashes>#{{2,4}})[^\n]*`{re.escape(job_type)}`[^\n]*$",
        text,
        re.MULTILINE,
    )
    if heading is None:
        if job_type.startswith(_STATUS_TRANSITION_PREFIX):
            return _status_transition_section(text)
        return ""
    level = len(heading.group("hashes"))
    start = heading.start()
    # End at the next heading whose level is <= this type's heading level.
    nxt = re.search(rf"^#{{1,{level}}} ", text[heading.end():], re.MULTILINE)
    end = heading.end() + nxt.start() if nxt else len(text)
    return text[start:end]


def test_all_executable_job_types_have_a_section_in_authoring_guide() -> None:
    """Forward coverage: every ALLOWED_JOB_TYPES entry is documented."""
    guide_text = _guide_text()

    missing = []
    for job_type in sorted(ALLOWED_JOB_TYPES):
        if _has_job_type_heading(guide_text, job_type):
            continue
        if job_type.startswith(_STATUS_TRANSITION_PREFIX) and _has_status_transition_documentation(
            guide_text, job_type
        ):
            continue
        missing.append(job_type)

    assert missing == [], (
        "Job types in queue_spec.py ALLOWED_JOB_TYPES but undocumented in "
        f"queue_authoring_guide.md: {missing}. Add a section, then run "
        "scripts/sync_btpipeline_spec.sh."
    )


def test_no_documented_job_type_is_missing_from_queue_spec() -> None:
    """Reverse coverage: no documented type lingers after removal from the spec."""
    guide_text = _guide_text()

    # Every ``##``/``###`` heading that quotes a snake_case identifier is
    # treated as a documented job type and must still exist in the contract.
    documented = {
        token
        for token in re.findall(r"^#{2,3} [^\n]*`([a-z][a-z0-9_]*_[a-z0-9_]+)`", guide_text, re.MULTILINE)
    }
    stale = sorted(documented - set(ALLOWED_JOB_TYPES))
    assert stale == [], (
        "Job types documented in queue_authoring_guide.md but absent from "
        f"queue_spec.py ALLOWED_JOB_TYPES: {stale}. Remove the stale section."
    )


def test_required_fields_are_documented_for_each_job_type() -> None:
    """Each required field in JOB_SCHEMAS is named in that type's section."""
    guide_text = _guide_text()

    problems: list[str] = []
    for job_type in sorted(ALLOWED_JOB_TYPES):
        section = _section_for(guide_text, job_type)
        if not section:
            problems.append(f"{job_type}: no documentation section found")
            continue
        for field in JOB_SCHEMAS[job_type]:
            if f"`{field}`" not in section:
                problems.append(f"{job_type}: required field `{field}` not documented in its section")

    assert problems == [], (
        "Required fields drifted from queue_spec.py JOB_SCHEMAS:\n  "
        + "\n  ".join(problems)
    )


def test_sync_script_produces_byte_identical_output_to_source(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["BTPIPELINE_DIR"] = str(tmp_path)

    subprocess.run([str(SYNC_SCRIPT)], cwd=REPO_ROOT, env=env, check=True)

    expected = BANNER + "\n\n" + AUTHORING_GUIDE.read_text(encoding="utf-8")
    actual = (tmp_path / "specs" / "QUEUE_JOB_SPEC.md").read_text(encoding="utf-8")

    assert actual == expected
