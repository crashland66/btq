from __future__ import annotations


STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_FAILED})


def is_terminal_status(status: object) -> bool:
    return status in TERMINAL_STATUSES

