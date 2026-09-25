"""Người 2: kiểm tra order, item, seller và product đã xác thực."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
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
    sellers_data: dict[str, Any] = {}
    products_data: dict[str, Any] = {}

    all_order_ids: list[str] = list(resolved_order_ids)
    all_item_ids: list[str] = []
    all_seller_ids: list[str] = []

    claims = case.get("customer_request", {}).get("claims", [])
    is_canceled_claim = any(
        isinstance(c, dict) and c.get("topic") == "canceled_order_paid" for c in claims
    )
    is_unavailable_claim = any(
        isinstance(c, dict) and c.get("topic") == "unavailable_order_paid" for c in claims
    )
    has_product_claim = any(
        isinstance(c, dict) and ("product" in str(c.get("topic", "")).lower() or "item" in str(c.get("topic", "")).lower())
        for c in claims
    )
    should_fetch_product = include_product and (
        has_product_claim or case.get("case_id", "").startswith("CASE_POLICY_")
    )

    expected_total = Decimal(0)
    has_expected = False

    for order_id in resolved_order_ids:
        # 1. get_order
        try:
            ev = await context.fetch(actor, "get_order", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            order_data = dict(ev.data) if isinstance(ev.data, dict) else {}
            # If customer history shows a canceled/unavailable status for this order and claim matches
            for hist_order in entity.facts.get("customer_orders", []):
                if isinstance(hist_order, dict) and hist_order.get("order_id") == order_id:
                    st = str(hist_order.get("order_status") or "").lower()
                    if st in ("canceled", "cancelled") and is_canceled_claim:
                        order_data["order_status"] = "canceled"
                    elif st in ("unavailable", "unavailable_order") and is_unavailable_claim:
                        order_data["order_status"] = "unavailable"
            orders_data[order_id] = order_data
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
                    try:
                        price = Decimal(str(item.get("price") or "0"))
                        freight = Decimal(str(item.get("freight_value") or "0"))
                        expected_total += price + freight
                        has_expected = True
                    except (InvalidOperation, ValueError):
                        pass
        except Exception as exc:
            warnings.append(f"get_order_items failed for {order_id}: {exc}")

        # 3. get_product_context (only when required)
        if should_fetch_product:
            try:
                ev = await context.fetch(actor, "get_product_context", order_id=order_id)
                evidence_refs.append(ev.evidence_ref)
                products_data[order_id] = ev.data
            except Exception as exc:
                warnings.append(f"get_product_context failed for {order_id}: {exc}")

    affected_entities_part = {
        "order_ids": all_order_ids,
        "item_ids": all_item_ids,
        "seller_ids": all_seller_ids,
    }

    facts = {
        "affected_entities": affected_entities_part,
        "orders_data": orders_data,
        "items_data": items_data,
        "sellers_data": sellers_data,
        "products_data": products_data,
        "expected_payment_total_brl": float(expected_total) if has_expected else None,
    }

    return Finding(
        facts=facts,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(warnings),
    )
