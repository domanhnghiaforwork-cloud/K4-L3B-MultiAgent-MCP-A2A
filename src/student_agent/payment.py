"""Reconcile payment and refund evidence; policy decides the final remedy."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .interfaces import CaseContext, Evidence, Finding

CENT = Decimal("0.01")
PAID = {"captured", "settled", "paid", "succeeded", "success", "completed", "confirmed"}
PENDING = {"pending", "processing", "requested", "initiated"}
FAILED = {"failed", "rejected", "cancelled", "canceled"}


def _value(row: dict[str, Any], *keys: str) -> Any:
    return next((row[key] for key in keys if row.get(key) is not None), None)


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return (
            amount
            if amount.is_finite() and amount >= 0 and amount == amount.quantize(CENT)
            else None
        )
    except (InvalidOperation, ValueError):
        return None


def _amount(row: dict[str, Any], warnings: list[str]) -> Decimal | None:
    currency = _value(row, "currency", "currency_code")
    if currency is not None and currency != "BRL":
        warnings.append("non_brl_amount_ignored")
        return None
    amount = _money(
        _value(row, "amount_brl", "payment_value", "refund_amount_brl", "value_brl", "amount")
    )
    if amount is None:
        warnings.append("invalid_or_missing_amount")
    return amount


def _rows(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
    return []


def _has_list(data: Any, *keys: str) -> bool:
    return isinstance(data, list) or (
        isinstance(data, dict) and any(isinstance(data.get(key), list) for key in keys)
    )


def _label(row: dict[str, Any], *keys: str) -> str | None:
    value = _value(row, *keys)
    return value if isinstance(value, str) and value else None


def _status(row: dict[str, Any]) -> str:
    return str(_value(row, "status", "state", "event_status") or "").lower()


def _kind(row: dict[str, Any]) -> str:
    return str(_value(row, "event_type", "type", "kind") or "").lower()


def _capture_event(row: dict[str, Any]) -> bool:
    kind = _kind(row)
    return "capture" in kind or kind in {"charge", "charged"}


def _captures(
    payment_data: Any, timeline_data: Any, warnings: list[str]
) -> tuple[Decimal | None, list[str], bool, bool, list[dict[str, Any]]]:
    payments = _rows(payment_data, "payments", "payment_rows", "order_payments")
    embedded = _rows(payment_data, "payment_events", "events", "timeline", "lifecycle_events")
    timeline = _rows(timeline_data, "payment_events", "events", "timeline", "lifecycle_events")
    timeline_captures = [row for row in timeline if _capture_event(row)]
    embedded_captures = [row for row in embedded if _capture_event(row)]
    events = timeline_captures or embedded_captures
    rows = events if events else payments
    if not rows:
        for source in (timeline_data, payment_data):
            if isinstance(source, dict):
                aggregate = _money(_value(source, "captured_total_brl"))
                if aggregate is not None:
                    return aggregate, [], False, False, []
        if _has_list(payment_data, "payments", "payment_rows", "order_payments"):
            return Decimal(0), [], False, False, []
        return None, [], False, False, []

    seen_transactions: set[str] = set()
    references: set[str] = set()
    signatures: list[tuple[str, Decimal]] = []
    lines: list[dict[str, Any]] = []
    total = Decimal(0)
    duplicate_marker = False
    for row in rows:
        status = _status(row)
        if events:
            if status not in PAID and not (
                not status and _kind(row) in {"captured", "payment_captured"}
            ):
                continue
        elif status in FAILED | PENDING | {"authorized", "voided"}:
            continue
        amount = _amount(row, warnings)
        if amount is None:
            return None, sorted(references), False, False, []
        transaction = _label(row, "capture_id", "transaction_id", "event_id")
        if transaction is not None and transaction in seen_transactions:
            continue  # Same capture observed twice, not a second charge.
        if transaction is not None:
            seen_transactions.add(transaction)
        reference = _label(row, "payment_reference", "payment_id", "charge_id")
        if reference:
            references.add(reference)
            signatures.append((reference, amount))
        duplicate_marker |= bool(
            _value(row, "duplicate_of", "duplicate_capture_id", "is_duplicate")
        )
        total += amount
        lines.append(
            {
                "payment_reference": reference,
                "capture_id": transaction,
                "amount_brl": float(amount),
                "source": "get_payment_timeline" if timeline_captures else "get_order_payments",
            }
        )

    for row in payments:
        reference = _label(row, "payment_reference", "payment_id", "charge_id")
        if reference:
            references.add(reference)
    if payments and not events and any(not _status(row) for row in payments):
        warnings.append("payment_rows_without_capture_status")
    repeated_charge = len(signatures) != len(set(signatures))
    return total, sorted(references), duplicate_marker, repeated_charge, lines


def _refunds(
    data: Any, warnings: list[str]
) -> tuple[Decimal | None, Decimal, Decimal, list[str], list[dict[str, Any]]]:
    if data is None:
        return Decimal(0), Decimal(0), Decimal(0), [], []
    events = _rows(data, "refund_events", "events", "timeline", "refunds")
    if not _has_list(data, "refund_events", "events", "timeline", "refunds"):
        aggregate = _money(_value(data, "refunded_total_brl")) if isinstance(data, dict) else None
        if aggregate is None:
            warnings.append("refund_timeline_shape_unknown")
        return aggregate, Decimal(0), Decimal(0), [], []
    latest: dict[str, tuple[str, Decimal | None]] = {}
    references: set[str] = set()
    for index, row in enumerate(events):
        status = _status(row)
        if not status:
            kind = _kind(row)
            for state in (
                "failed",
                "rejected",
                "cancelled",
                "canceled",
                "refunded",
                "completed",
                "succeeded",
                "success",
                "settled",
                "paid",
                "pending",
                "processing",
                "requested",
                "initiated",
            ):
                if state in kind:
                    status = state
                    break
        reference = _label(row, "refund_id", "refund_reference", "transaction_id")
        if reference:
            references.add(reference)
        key = reference or f"row:{index}"
        amount = _amount(row, warnings)
        if amount is None and key in latest:
            amount = latest[key][1]
        latest[key] = (status, amount)

    completed = pending = failed = Decimal(0)
    unclear = False
    lines = []
    for reference, (status, amount) in latest.items():
        lines.append(
            {
                "refund_reference": None if reference.startswith("row:") else reference,
                "status": status or "unknown",
                "amount_brl": float(amount) if amount is not None else None,
            }
        )
        if amount is None:
            unclear = True
        elif status in PAID | {"refunded", "refund_completed"}:
            completed += amount
        elif status in PENDING:
            pending += amount
        elif status in FAILED:
            failed += amount
        else:
            unclear = True
    if unclear:
        warnings.append("refund_status_or_amount_unclear")
    return (None if unclear else completed), pending, failed, sorted(references), lines


async def _fetch(
    context: CaseContext, name: str, order_id: str, warnings: list[str]
) -> Evidence | None:
    try:
        evidence = await context.fetch("payment-agent", name, order_id=order_id)
    except RuntimeError:
        warnings.append(f"{name}_unavailable")
        return None
    expected_domain = "refund" if name == "get_refund_timeline" else "payment"
    if evidence.domain != expected_domain:
        warnings.append(f"{name}_unexpected_domain")
        return None
    if isinstance(evidence.data, dict) and evidence.data.get("order_id", order_id) != order_id:
        warnings.append(f"{name}_order_mismatch")
        return None
    warnings.extend(evidence.warnings)
    return evidence


async def investigate_payment(
    case: dict[str, Any], context: CaseContext, entity: Finding, order: Finding
) -> Finding:
    """Return scoped payment facts and transaction limits, never a policy refund amount."""
    if case["case_id"] != context.case_id:
        raise ValueError("payment investigation case_id does not match context")
    order_ids = entity.facts.get("resolved_order_ids", [])
    if not isinstance(order_ids, list) or not all(isinstance(value, str) for value in order_ids):
        raise ValueError("entity resolved_order_ids must be a list of strings")

    warnings: list[str] = []
    evidence_refs: list[str] = []
    payment_refs: set[str] = set()
    refund_refs: set[str] = set()
    capture_lines: list[dict[str, Any]] = []
    refund_lines: list[dict[str, Any]] = []
    capture_total = refunded_total = pending_total = failed_total = Decimal(0)
    capture_known = refund_known = bool(order_ids)
    expected = _money(order.facts.get("expected_payment_total_brl"))
    duplicate = split = False
    if not order_ids:
        warnings.append("no_resolved_order")

    for order_id in dict.fromkeys(order_ids):
        payments = await _fetch(context, "get_order_payments", order_id, warnings)
        payment_data = payments.data if payments else None
        if payments:
            evidence_refs.append(payments.evidence_ref)
        embedded = _rows(payment_data, "payment_events", "events", "timeline", "lifecycle_events")
        claims_topics = [
            c.get("topic")
            for c in case.get("customer_request", {}).get("claims", [])
            if isinstance(c, dict)
        ]
        need_timeline = (
            "duplicate_charge" in claims_topics
            or "payment_mismatch" in claims_topics
            or "valid_split_payment" in claims_topics
        )
        timeline = None
        if need_timeline and not any(_capture_event(row) for row in embedded):
            timeline = await _fetch(context, "get_payment_timeline", order_id, warnings)
            if timeline:
                evidence_refs.append(timeline.evidence_ref)
        need_refund_timeline = (
            "refund_pending" in claims_topics
            or "refund_failed" in claims_topics
        )
        refund = None
        if need_refund_timeline:
            refund = await _fetch(context, "get_refund_timeline", order_id, warnings)
            if refund:
                evidence_refs.append(refund.evidence_ref)

        captured, references, marked_duplicate, repeated_charge, lines = _captures(
            payment_data, timeline.data if timeline else None, warnings
        )
        capture_known &= captured is not None
        if captured is not None:
            capture_total += captured
        payment_refs.update(references)
        for line in lines:
            source = timeline if line["source"] == "get_payment_timeline" else payments
            capture_lines.append(
                {
                    "order_id": order_id,
                    **line,
                    "evidence_ref": source.evidence_ref if source else None,
                }
            )
        split |= (
            len(references) > 1
            or len(payments.data if payments and isinstance(payments.data, list) else []) > 1
            or len(lines) > 1
        )
        duplicate |= marked_duplicate or (
            len(order_ids) == 1
            and repeated_charge
            and expected is not None
            and captured is not None
            and captured > expected
        )

        refunded, pending, failed, refund_references, lines = _refunds(
            refund.data if refund else None, warnings
        )
        refund_known &= refunded is not None
        if refunded is not None:
            refunded_total += refunded
        pending_total += pending
        failed_total += failed
        refund_refs.update(refund_references)
        refund_lines.extend(
            {"order_id": order_id, **line, "evidence_ref": refund.evidence_ref if refund else None}
            for line in lines
        )

    captured = capture_total if capture_known else None
    refunded = refunded_total if refund_known else None
    remaining = (
        max(Decimal(0), captured - refunded)
        if captured is not None and refunded is not None
        else None
    )
    if (
        "duplicate_charge" in claims_topics
        and (duplicate or (captured is not None and expected is not None and captured > expected) or len(payment_refs) > 1)
    ):
        verdict = "duplicate_capture"
    elif (
        "payment_mismatch" in claims_topics
        and captured is not None
        and expected is not None
        and captured != expected
    ):
        verdict = "capture_mismatch"
    elif "refund_failed" in claims_topics and failed_total > 0:
        verdict = "refund_failed"
    elif "refund_pending" in claims_topics and pending_total > 0:
        verdict = "refund_pending"
    elif "valid_split_payment" in claims_topics and split:
        verdict = "reconciled"
    elif "refund_failed" in claims_topics:
        verdict = "refund_failed"
    elif "refund_pending" in claims_topics:
        verdict = "refund_pending"
    elif duplicate:
        verdict = "duplicate_capture"
    elif failed_total > 0:
        verdict = "refund_failed"
    elif pending_total > 0:
        verdict = "refund_pending"
    elif refunded is not None and refunded > 0:
        verdict = "refunded"
    elif captured is not None:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"
    if captured is not None and refunded is not None and refunded > captured:
        verdict = "capture_mismatch"
        remaining = None
        warnings.append("refund_exceeds_capture")

    return Finding(
        facts={
            "payment_analysis": {
                "verdict": verdict,
                "captured_total_brl": float(captured) if captured is not None else None,
                "refunded_total_brl": float(refunded) if refunded is not None else None,
                "refundable_total_brl": float(remaining) if remaining is not None else None,
            },
            "payment_references": sorted(payment_refs),
            "payment_facts": {
                "split_payment": split,
                "expected_payment_total_brl": float(expected) if expected is not None else None,
                "pending_refund_total_brl": float(pending_total),
                "failed_refund_total_brl": float(failed_total),
                "refund_references": sorted(refund_refs),
                "capture_lines": capture_lines,
                "refund_lines": refund_lines,
                "refundable_total_is_transaction_limit": True,
            },
        },
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
