"""Người 2: kiểm tra order, item, seller và product đã xác thực."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_order(
    case: dict[str, Any], context: CaseContext, entity: Finding
) -> Finding:
    """Return affected_entities and order/product facts, with supporting refs."""
    raise NotImplementedError("Người 2 triển khai order/product investigation")
