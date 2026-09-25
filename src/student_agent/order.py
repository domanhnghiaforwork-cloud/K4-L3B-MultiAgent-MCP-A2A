"""Người 2: kiểm tra order, item, seller và product đã xác thực."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .interfaces import CaseContext, Finding


async def investigate_order(
    case: dict[str, Any], context: CaseContext, entity: Finding
) -> Finding:
    """Return affected_entities and order/product facts, with supporting refs."""
    evidence_refs: list[str] = []
    warnings: list[str] = []
    actor = "order-agent"

    resolved_order_ids: list[str] = entity.facts.get("resolved_order_ids", [])
    scope = case.get("investigation_scope", {})
    include_product = scope.get("include_product_context", True)

    orders_data: dict[str, Any] = {}
    items_data: dict[str, Any] = {}

    all_order_ids: list[str] = list(resolved_order_ids)
    all_item_ids: list[str] = []
    all_seller_ids: list[str] = []
    expected_total = Decimal("0.00")
    has_expected_total = False

    for order_id in resolved_order_ids:
        # 1. get_order
        try:
            ev = await context.fetch(actor, "get_order", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            orders_data[order_id] = dict(ev.data) if isinstance(ev.data, dict) else ev.data
            cust_orders = entity.facts.get("customer_orders", [])
            claims_topics = [
                c.get("topic")
                for c in case.get("customer_request", {}).get("claims", [])
                if isinstance(c, dict)
            ]
            for co in cust_orders:
                if co.get("order_id") == order_id:
                    st = str(co.get("order_status") or "").lower()
                    if "canceled_order_paid" in claims_topics and st in ("canceled", "cancelled"):
                        if isinstance(orders_data.get(order_id), dict):
                            orders_data[order_id]["order_status"] = "canceled"
                    elif "unavailable_order_paid" in claims_topics and st in ("unavailable", "unavailable_order"):
                        if isinstance(orders_data.get(order_id), dict):
                            orders_data[order_id]["order_status"] = "unavailable"
        except Exception as exc:
            warnings.append(f"get_order failed for {order_id}: {exc}")

        # 2. get_order_items
        try:
            ev = await context.fetch(actor, "get_order_items", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            items_data[order_id] = ev.data
            items_list = (
                ev.data.get("items", [])
                if isinstance(ev.data, dict)
                else (ev.data if isinstance(ev.data, list) else [])
            )
            for item in items_list:
                if isinstance(item, dict):
                    item_id = str(
                        item.get("order_item_id")
                        or item.get("item_id")
                        or item.get("product_id", "")
                    )
                    if item_id and item_id not in all_item_ids:
                        all_item_ids.append(item_id)
                    seller_id = item.get("seller_id")
                    if seller_id and seller_id not in all_seller_ids:
                        all_seller_ids.append(seller_id)
                    price_str = item.get("price") or item.get("price_brl")
                    freight_str = item.get("freight_value") or item.get("freight_brl") or item.get("freight")
                    if price_str is not None:
                        try:
                            price_val = Decimal(str(price_str))
                            freight_val = Decimal(str(freight_str)) if freight_str is not None else Decimal("0.00")
                            expected_total += price_val + freight_val
                            has_expected_total = True
                        except Exception:
                            pass
        except Exception as exc:
            warnings.append(f"get_order_items failed for {order_id}: {exc}")

    affected_entities_part = {
        "order_ids": all_order_ids,
        "item_ids": all_item_ids,
        "seller_ids": all_seller_ids,
    }

    facts = {
        "affected_entities": affected_entities_part,
        "orders_data": orders_data,
        "items_data": items_data,
        "expected_payment_total_brl": float(expected_total) if has_expected_total else None,
    }

    return Finding(
        facts=facts,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(warnings),
    )
