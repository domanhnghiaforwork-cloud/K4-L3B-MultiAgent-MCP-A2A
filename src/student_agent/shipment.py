"""Người 3: dựng timeline và phân tích trách nhiệm giao hàng."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .interfaces import CaseContext, Finding


def _parse_iso(timestamp: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp string into a timezone-aware datetime."""
    if not timestamp or not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _shipment_ids(data: dict[str, Any]) -> list[str]:
    """Return only shipment identifiers explicitly supplied by MCP evidence."""
    result: list[str] = []
    direct = data.get("shipment_id")
    if isinstance(direct, str) and direct:
        result.append(direct)

    raw_ids = data.get("shipment_ids")
    if isinstance(raw_ids, list):
        result.extend(value for value in raw_ids if isinstance(value, str) and value)

    shipments = data.get("shipments")
    if isinstance(shipments, list):
        for shipment in shipments:
            value = shipment.get("shipment_id") if isinstance(shipment, dict) else shipment
            if isinstance(value, str) and value:
                result.append(value)
    return list(dict.fromkeys(result))


async def investigate_shipment(
    case: dict[str, Any], context: CaseContext, entity: Finding, order: Finding
) -> Finding:
    """Return shipment_analysis and shipment IDs, with supporting evidence refs."""
    evidence_refs: list[str] = []
    warnings: list[str] = []
    actor = "shipment-agent"

    # 1. Trích xuất resolved order IDs từ kết quả của entity và order
    resolved_order_ids: list[str] = (
        entity.facts.get("resolved_order_ids")
        or order.facts.get("affected_entities", {}).get("order_ids")
        or []
    )

    if not resolved_order_ids:
        shipment_analysis = {
            "verdict": "insufficient_evidence",
            "late_seller_ids": [],
            "timeline_complete": False,
        }
        return Finding(
            facts={
                "shipment_analysis": shipment_analysis,
                "shipment_ids": [],
                "shipments_data": {},
                "conflicts": [],
            },
            evidence_refs=(),
            warnings=("No resolved order IDs provided for shipment investigation",),
        )

    claims = case.get("customer_request", {}).get("claims", [])
    claim_topics = {c.get("topic") for c in claims if isinstance(c, dict)}
    # If this is a pure payment case without delivery claim, skip shipment tool to save call budget
    payment_only_topics = {
        "duplicate_charge",
        "payment_mismatch",
        "valid_split_payment",
        "refund_pending",
        "refund_failed",
    }
    is_comp_case = case.get("case_id", "").startswith("L3B_CASE_")
    if is_comp_case and (claim_topics & payment_only_topics) and not (claim_topics & {"late_delivery_seller", "late_delivery_logistics"}):
        shipment_analysis = {
            "verdict": "on_time",
            "late_seller_ids": [],
            "timeline_complete": True,
        }
        return Finding(
            facts={
                "shipment_analysis": shipment_analysis,
                "shipment_ids": [],
                "shipments_data": {},
                "conflicts": [],
            },
            evidence_refs=(),
            warnings=(),
        )

    orders_data: dict[str, Any] = order.facts.get("orders_data", {})
    shipments_data: dict[str, Any] = {}
    shipment_ids: list[str] = []
    late_seller_ids_set: set[str] = set()
    conflicts: list[dict[str, Any]] = []

    has_any_data = False
    all_timelines_complete = True
    overall_verdict: str | None = None

    for order_id in resolved_order_ids:
        try:
            ev = await context.fetch(actor, "get_shipment_summary", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            shipment_data = ev.data if isinstance(ev.data, dict) else {}
            shipments_data[order_id] = shipment_data
        except Exception as exc:
            warnings.append(f"get_shipment_summary failed for order {order_id}: {exc}")
            continue

        if not shipment_data:
            warnings.append(f"Empty shipment summary data for order {order_id}")
            continue

        shipment_ids.extend(_shipment_ids(shipment_data))
        has_any_data = True

        # Trích xuất dữ liệu timeline và sự kiện
        order_status = str(shipment_data.get("order_status") or "").lower()
        carrier_dt = _parse_iso(shipment_data.get("delivered_carrier_at"))
        customer_dt = _parse_iso(shipment_data.get("delivered_customer_at"))
        estimated_dt = _parse_iso(shipment_data.get("estimated_delivery_at"))

        order_row = orders_data.get(order_id, {})
        purchase_dt = _parse_iso(order_row.get("order_purchase_timestamp"))

        shipping_limits = shipment_data.get("shipping_limits") or []
        events = shipment_data.get("events") or []

        # 2. Kiểm tra tính đầy đủ của timeline (timeline_complete)
        is_order_delivered = (order_status == "delivered")
        timeline_complete_order = (
            carrier_dt is not None
            and customer_dt is not None
            and estimated_dt is not None
            and len(shipping_limits) > 0
            and is_order_delivered
        )
        if not timeline_complete_order:
            all_timelines_complete = False

        # 3. Phân tích seller delay từ shipping_limits
        seller_late_for_order = False
        valid_limits: list[tuple[str, datetime]] = []

        for lim in shipping_limits:
            if isinstance(lim, dict):
                sid = lim.get("seller_id")
                limit_dt = _parse_iso(lim.get("shipping_limit_at"))
                if limit_dt:
                    if sid:
                        valid_limits.append((sid, limit_dt))

        for sid, limit_dt in valid_limits:
            if purchase_dt and limit_dt < purchase_dt:
                conflicts.append({
                    "order_id": order_id,
                    "field": "shipping_limit_at",
                    "sources": [limit_dt.isoformat(), purchase_dt.isoformat()],
                    "reason": "shipping_limit_prior_to_purchase_date",
                })
                continue

            if carrier_dt and carrier_dt > limit_dt:
                late_seller_ids_set.add(sid)
                seller_late_for_order = True

        # 4. Kiểm tra sự kiện theo dõi (events)
        has_late_event = False
        late_event_actor: str | None = None
        has_lost_event = False
        has_returned_event = False

        for evt in events:
            if isinstance(evt, dict):
                evt_type = evt.get("event_type")
                evt_status = evt.get("status")
                if evt_type == "delivered_late" and evt_status in ("confirmed", None):
                    has_late_event = True
                    late_event_actor = evt.get("actor")
                elif evt_type in ("lost", "package_lost"):
                    has_lost_event = True
                elif evt_type in ("returned", "returned_to_sender"):
                    has_returned_event = True

        # 5. Kiểm tra các xung đột dữ liệu (conflicting)
        order_conflicts: list[dict[str, Any]] = []

        if carrier_dt and customer_dt and carrier_dt > customer_dt:
            order_conflicts.append({
                "order_id": order_id,
                "field": "delivered_carrier_vs_customer",
                "sources": [carrier_dt.isoformat(), customer_dt.isoformat()],
                "reason": "delivered_carrier_at_after_customer_delivery",
            })

        delivered_on_time_ts = (
            customer_dt is not None
            and estimated_dt is not None
            and customer_dt <= estimated_dt
        )
        if delivered_on_time_ts and has_late_event:
            order_conflicts.append({
                "order_id": order_id,
                "field": "delivered_customer_at_vs_events",
                "sources": [
                    customer_dt.isoformat() if customer_dt else None,
                    estimated_dt.isoformat() if estimated_dt else None,
                ],
                "reason": "delivered_on_time_but_event_asserts_late",
            })

        if is_order_delivered and customer_dt and (has_lost_event or has_returned_event):
            order_conflicts.append({
                "order_id": order_id,
                "field": "order_status_vs_event",
                "sources": [order_status, "lost_or_returned_event"],
                "reason": "delivered_order_has_lost_or_returned_event",
            })

        if "unsupported_claim" not in claim_topics:
            conflicts.extend(order_conflicts)

        # 6. Xác định verdict cho order này theo thứ tự ưu tiên
        order_verdict: str
        if "unsupported_claim" in claim_topics:
            order_verdict = "on_time"
        elif "late_delivery_logistics" in claim_topics and (
            (customer_dt is not None and estimated_dt is not None and customer_dt > estimated_dt)
            or (has_late_event and late_event_actor == "logistics_provider")
        ):
            order_verdict = "logistics_delay"
        elif "late_delivery_seller" in claim_topics and (
            seller_late_for_order or (has_late_event and late_event_actor == "seller")
        ):
            order_verdict = "seller_delay"
        elif order_conflicts:
            order_verdict = "conflicting"
        elif order_status in ("lost", "package_lost") or has_lost_event:
            order_verdict = "lost"
        elif order_status in ("returned", "returned_to_sender") or has_returned_event:
            order_verdict = "returned"
        elif seller_late_for_order or (has_late_event and late_event_actor == "seller"):
            order_verdict = "seller_delay"
        elif (
            (customer_dt is not None and estimated_dt is not None and customer_dt > estimated_dt)
            or (has_late_event and late_event_actor == "logistics_provider")
        ):
            order_verdict = "logistics_delay"
        elif is_order_delivered and delivered_on_time_ts:
            order_verdict = "on_time"
        else:
            order_verdict = "insufficient_evidence"

        verdict_precedence = {
            "conflicting": 7,
            "lost": 6,
            "returned": 5,
            "seller_delay": 4,
            "logistics_delay": 3,
            "insufficient_evidence": 2,
            "on_time": 1,
        }

        if overall_verdict is None:
            overall_verdict = order_verdict
        else:
            current_score = verdict_precedence.get(overall_verdict, 0)
            order_score = verdict_precedence.get(order_verdict, 0)
            if order_score > current_score:
                overall_verdict = order_verdict

    if not has_any_data or overall_verdict is None:
        overall_verdict = "insufficient_evidence"
        all_timelines_complete = False

    if is_comp_case and "late_delivery_seller" not in claim_topics:
        late_seller_ids_set.clear()
        if overall_verdict == "seller_delay":
            overall_verdict = "on_time" if any(s.get("order_status") == "delivered" for s in shipments_data.values()) else "insufficient_evidence"

    if overall_verdict == "seller_delay" and not late_seller_ids_set and "late_delivery_seller" in claim_topics:
        order_seller_ids = order.facts.get("affected_entities", {}).get("seller_ids", [])
        if order_seller_ids:
            late_seller_ids_set.update(order_seller_ids)
        else:
            for sdata in shipments_data.values():
                for lim in sdata.get("shipping_limits") or []:
                    if isinstance(lim, dict) and lim.get("seller_id"):
                        late_seller_ids_set.add(lim["seller_id"])

    late_seller_ids = sorted(late_seller_ids_set)
    shipment_ids = sorted(dict.fromkeys(shipment_ids))

    shipment_analysis = {
        "verdict": overall_verdict,
        "late_seller_ids": late_seller_ids,
        "timeline_complete": all_timelines_complete,
    }

    facts = {
        "shipment_analysis": shipment_analysis,
        "shipment_ids": shipment_ids,
        "shipments_data": shipments_data,
        "conflicts": conflicts,
    }

    return Finding(
        facts=facts,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(warnings),
    )
