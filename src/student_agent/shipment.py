"""Người 3: dựng timeline và phân tích trách nhiệm giao hàng."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_shipment(
    case: dict[str, Any], context: CaseContext, entity: Finding, order: Finding
) -> Finding:
    """Return shipment_analysis and shipment IDs, with supporting refs."""
    raise NotImplementedError("Người 3 triển khai shipment investigation")
