"""Người 5: phát hiện và phân giải mâu thuẫn giữa các nguồn."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def resolve_conflicts(
    case: dict[str, Any], context: CaseContext, findings: dict[str, Finding]
) -> Finding:
    """Return data_conflicts and selected-source facts, with supporting refs."""
    raise NotImplementedError("Người 5 triển khai conflict resolution")
