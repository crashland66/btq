from __future__ import annotations

import pytest

from action_validator import ValidationError, validate_actions


def valid_action() -> dict:
    return {
        "type": "update_file",
        "target": "project/demo/index.html",
        "description": "Update demo booking flow copy.",
        "payload": {"change": "Replace vague CTA with Book a consult."},
    }


def test_valid_actions_pass() -> None:
    actions = [valid_action()]

    assert validate_actions(actions) == actions


def test_vague_descriptions_fail() -> None:
    action = valid_action()
    action["description"] = "Improve site"

    with pytest.raises(ValidationError) as exc:
        validate_actions([action])

    assert "action 1: description contains vague phrase 'improve'" in exc.value.errors


def test_empty_payload_fails() -> None:
    action = valid_action()
    action["payload"] = {}

    with pytest.raises(ValidationError) as exc:
        validate_actions([action])

    assert "action 1: payload has no actionable fields for update_file" in exc.value.errors


def test_duplicate_actions_fail() -> None:
    actions = [valid_action(), valid_action()]

    with pytest.raises(ValidationError) as exc:
        validate_actions(actions)

    assert "action 2: duplicate action for type and target" in exc.value.errors


def test_invalid_targets_fail() -> None:
    action = valid_action()
    action["target"] = "/"

    with pytest.raises(ValidationError) as exc:
        validate_actions([action])

    assert "action 1: target is too generic" in exc.value.errors


def test_generate_agents_txt_requires_structured_payload() -> None:
    action = {
        "type": "generate_agents_txt",
        "target": "/agents.txt",
        "description": "Generate agents.txt capability declaration.",
        "payload": {"capabilities": ["booking-review"]},
    }

    assert validate_actions([action]) == [action]
