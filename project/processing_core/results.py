from __future__ import annotations


def result_counts(*names: str) -> dict[str, int]:
    return {name: 0 for name in names}


def semantic_result_counts() -> dict[str, int]:
    return result_counts("discovered", "skipped", "completed", "failed")

