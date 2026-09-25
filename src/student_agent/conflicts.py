"""Resolve observable discrepancies between case input and MCP specialist facts."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding

RAW_SOURCES = {
    "discrepancy_in_shipping_limits_exceeds_30_days": (
        "get_shipment_summary.earliest_shipping_limit",
        "get_shipment_summary.latest_shipping_limit",
    ),
    "shipping_limit_prior_to_purchase_date": (
        "get_order.purchase_timestamp",
        "get_shipment_summary.shipping_limit_at",
    ),
    "delivered_carrier_at_after_customer_delivery": (
        "get_shipment_summary.delivered_carrier_at",
        "get_shipment_summary.delivered_customer_at",
    ),
    "delivered_on_time_but_event_asserts_late": (
        "get_shipment_summary.delivery_timestamps",
        "get_shipment_summary.tracking_events",
    ),
    "delivered_order_has_lost_or_returned_event": (
        "get_shipment_summary.order_status",
        "get_shipment_summary.tracking_events",
    ),
}


def _status(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    raw = value.get("order_status") or value.get("status")
    return raw.lower() if isinstance(raw, str) and raw else None


def _append(
    rows: list[dict[str, Any]],
    *,
    field: str,
    sources: tuple[str, str],
    selected: str | None,
    code: str,
) -> None:
    if not field or sources[0] == sources[1]:
        return
    candidate = {
        "field": field[:100],
        "sources": list(sources),
        "selected_source": selected,
        "resolution_code": code,
    }
    if not any(
        row["field"] == candidate["field"] and row["sources"] == candidate["sources"]
        for row in rows
    ):
        rows.append(candidate)


async def resolve_conflicts(
    case: dict[str, Any], context: CaseContext, findings: dict[str, Finding]
) -> Finding:
    """Return schema-shaped conflicts and source choices; do not invent evidence."""
    if case.get("case_id") != context.case_id:
        raise ValueError("conflict investigation case_id does not match context")
    required = ("entity", "order", "shipment", "payment")
    if any(not isinstance(findings.get(name), Finding) for name in required):
        raise ValueError("conflict resolver requires entity, order, shipment, payment findings")
    for name in required:
        context.require_refs(findings[name].evidence_refs)
    entity, order, shipment, payment = (findings[name] for name in required)
    rows: list[dict[str, Any]] = []
    refs: list[str] = []

    # Customer supplied IDs are hints. A different MCP-verified order takes precedence.
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    resolved = entity.facts.get("resolved_order_ids", [])
    if (
        isinstance(claimed, str)
        and isinstance(resolved, list)
        and resolved
        and claimed not in resolved
        and entity.evidence_refs
    ):
        _append(
            rows,
            field="claimed_order_id",
            sources=("customer_request", "mcp_order"),
            selected="mcp_order",
            code="claim_candidate_rejected",
        )
        refs.extend(entity.evidence_refs)

    # The order row is the authoritative source for the order's status.
    order_rows = order.facts.get("orders_data", {})
    shipment_rows = shipment.facts.get("shipments_data", {})
    if isinstance(order_rows, dict) and isinstance(shipment_rows, dict):
        for order_id in resolved if isinstance(resolved, list) else []:
            order_status = _status(order_rows.get(order_id))
            shipment_status = _status(shipment_rows.get(order_id))
            if order_status and shipment_status and order_status != shipment_status:
                _append(
                    rows,
                    field=f"{order_id}.order_status",
                    sources=("get_order", "get_shipment_summary"),
                    selected="get_order" if order.evidence_refs else None,
                    code="authoritative_order_status",
                )
                refs.extend(order.evidence_refs)
                refs.extend(shipment.evidence_refs)

    # Contradictions within a shipment response remain unresolved unless an
    # independent order timestamp invalidates a pre-purchase shipping limit.
    raw_conflicts = shipment.facts.get("conflicts", [])
    if isinstance(raw_conflicts, list) and shipment.evidence_refs:
        for raw in raw_conflicts:
            if not isinstance(raw, dict):
                continue
            reason = raw.get("reason")
            sources = RAW_SOURCES.get(reason)
            if sources is None:
                continue
            field_name = raw.get("field")
            if not isinstance(field_name, str) or not field_name:
                continue
            order_id = raw.get("order_id")
            field = f"{order_id}.{field_name}" if isinstance(order_id, str) else field_name
            selected = None
            if reason == "shipping_limit_prior_to_purchase_date" and order.evidence_refs:
                selected = sources[0]
                refs.extend(order.evidence_refs)
            _append(
                rows,
                field=field,
                sources=sources,
                selected=selected,
                code=reason,
            )
            refs.extend(shipment.evidence_refs)

    # Keep the public schema's five-conflict limit and reference only emitted facts.
    rows = rows[:5]
    if not rows:
        refs = []
    selected_sources = {
        row["field"]: row["selected_source"] for row in rows if row["selected_source"] is not None
    }
    return Finding(
        facts={
            "data_conflicts": rows,
            "selected_sources": selected_sources,
            "unresolved_fields": [row["field"] for row in rows if row["selected_source"] is None],
        },
        evidence_refs=tuple(dict.fromkeys(refs)),
    )
