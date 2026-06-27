from __future__ import annotations


def defect_taxonomy_prompt_block(qc_category: object) -> str:
    """Return the optional QC defect taxonomy prompt block.

    Phase A1 intentionally provides only the prompt seam. Per-category taxonomy
    content is added in A2.
    """
    _ = qc_category
    return ""
