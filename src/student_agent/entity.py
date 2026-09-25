"""Người 2: xác thực order candidate và customer context."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_entity(case: dict[str, Any], context: CaseContext) -> Finding:
    """Return entity_resolution, customer_context and resolved_order_ids in facts."""
    raise NotImplementedError("Người 2 triển khai entity/customer investigation")
