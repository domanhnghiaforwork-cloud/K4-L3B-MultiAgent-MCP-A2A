"""Người 5: kiểm chứng kết quả độc lập trước khi finalize."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def verify_output(
    case: dict[str, Any],
    context: CaseContext,
    findings: dict[str, Finding],
    output: dict[str, Any],
) -> None:
    """Raise on schema or semantic inconsistency; do not silently change output."""
    raise NotImplementedError("Người 5 triển khai independent verification")
