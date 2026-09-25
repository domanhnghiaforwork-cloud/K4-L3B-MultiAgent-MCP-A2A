"""Người 2: xác thực order candidate và customer context."""

from __future__ import annotations

from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_entity(case: dict[str, Any], context: CaseContext) -> Finding:
    """Return entity_resolution, customer_context and resolved_order_ids in facts."""
    evidence_refs: list[str] = []
    warnings: list[str] = []
    actor = "entity-agent"

    candidate_ids: list[str] = list(case.get("candidate_order_ids", []))
    claimed_order_id = case.get("customer_request", {}).get("claimed_order_id")
    if claimed_order_id and claimed_order_id not in candidate_ids:
        candidate_ids.append(claimed_order_id)

    customer_unique_id_hint = case.get("customer_unique_id_hint")
    related_order_ids: list[str] = []
    customer_unique_id: str | None = customer_unique_id_hint

    # 1. Fetch customer history if available
    customer_history_orders: set[str] = set()
    customer_orders: list[dict[str, Any]] = []
    if customer_unique_id_hint:
        try:
            cust_ev = await context.fetch(
                actor, "get_customer_history", customer_unique_id=customer_unique_id_hint
            )
            evidence_refs.append(cust_ev.evidence_ref)
            cust_data = cust_ev.data
            if isinstance(cust_data, dict):
                orders = cust_data.get("orders") or cust_data.get("related_order_ids") or []
                if isinstance(orders, list):
                    for o in orders:
                        oid = o.get("order_id") if isinstance(o, dict) else o
                        if isinstance(oid, str):
                            customer_history_orders.add(oid)
                            if oid not in related_order_ids:
                                related_order_ids.append(oid)
                customer_orders = [o for o in orders if isinstance(o, dict)]
        except Exception as exc:
            warnings.append(f"get_customer_history failed for {customer_unique_id_hint}: {exc}")

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []

    # 2. Check each candidate order
    for candidate in candidate_ids:
        if candidate.startswith("candidate-") or (
            customer_history_orders and candidate not in customer_history_orders
        ):
            rejected_candidates.append(candidate)
            continue
        try:
            order_ev = await context.fetch(actor, "get_order", order_id=candidate)
            evidence_refs.append(order_ev.evidence_ref)
            order_data = order_ev.data
            if isinstance(order_data, dict) and order_data.get("order_id"):
                # If customer history exists, verify candidate matches customer
                if customer_history_orders and candidate not in customer_history_orders:
                    rejected_candidates.append(candidate)
                else:
                    if candidate not in resolved_order_ids:
                        resolved_order_ids.append(candidate)
                    if not customer_unique_id:
                        customer_unique_id = order_data.get("customer_unique_id")
            else:
                rejected_candidates.append(candidate)
        except Exception:
            rejected_candidates.append(candidate)

    # 3. Determine status and confidence
    if resolved_order_ids:
        status = "resolved"
        confidence = 1.0 if len(resolved_order_ids) == 1 else 0.8
    elif candidate_ids:
        status = "not_found"
        confidence = 0.9
    else:
        status = "ambiguous"
        confidence = 0.5

    entity_resolution = {
        "status": status,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "confidence": confidence,
    }

    customer_context = {
        "customer_unique_id": customer_unique_id,
        "related_order_ids": related_order_ids,
    }

    facts = {
        "entity_resolution": entity_resolution,
        "customer_context": customer_context,
        "resolved_order_ids": resolved_order_ids,
        "rejected_candidates": rejected_candidates,
        "customer_unique_id": customer_unique_id,
        "customer_orders": customer_orders,
    }

    return Finding(
        facts=facts,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(warnings),
    )
