"""Người 5: tổng hợp findings và policy evidence thành output L3B."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def decide_policy(
    case: dict[str, Any], context: CaseContext, findings: dict[str, Finding]
) -> dict[str, Any]:
    """Return the complete L3B output; include only refs fetched in this case."""
    raise NotImplementedError("Người 5 triển khai policy decision và output assembly")
