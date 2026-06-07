from __future__ import annotations

from typing import Any

from action_validator import validate_actions


ACTION_JOB_TYPES = {
    "update_file": "update_file",
    "add_file": "create_file",
    "create_file": "create_file",
    "generate_agents_txt": "generate_agents_txt",
    "http_call": "external_call",
}


class SkillQueueValidationError(Exception):
    pass


def map_actions_to_queue(actions: list, source: str = "skill:unknown:unknown") -> list:
    if not isinstance(actions, list):
        raise SkillQueueValidationError("actions must be a list.")
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise SkillQueueValidationError(f"action {index} must be a mapping.")
        if action.get("type") not in ACTION_JOB_TYPES:
            raise SkillQueueValidationError(f"unknown action type for action {index}: {action.get('type')}")
    actions = validate_actions(actions)
    jobs: list[dict[str, Any]] = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise SkillQueueValidationError(f"action {index} must be a mapping.")
        action_type = action.get("type")
        target = action.get("target")
        payload = action.get("payload")
        if action_type not in ACTION_JOB_TYPES:
            raise SkillQueueValidationError(f"unknown action type for action {index}: {action_type}")
        if not isinstance(target, str) or not target.strip():
            raise SkillQueueValidationError(f"action {index} target must be a non-empty string.")
        if not isinstance(action.get("description"), str) or not action["description"].strip():
            raise SkillQueueValidationError(f"action {index} description must be a non-empty string.")
        if not isinstance(payload, dict):
            raise SkillQueueValidationError(f"action {index} payload must be a mapping.")
        jobs.append(
            {
                "job_type": ACTION_JOB_TYPES[action_type],
                "target": target,
                "payload": payload,
                "source": source,
            }
        )
    return jobs
