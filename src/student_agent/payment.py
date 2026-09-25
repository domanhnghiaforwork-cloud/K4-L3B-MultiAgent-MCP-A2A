"""Người 4: đối soát thanh toán, capture và refund."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_payment(
    case: dict[str, Any], context: CaseContext, entity: Finding, order: Finding
) -> Finding:
    """Return payment_analysis and money facts; policy decides the final refund."""
    raise NotImplementedError("Người 4 triển khai payment/refund investigation")
