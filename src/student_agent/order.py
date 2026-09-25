"""Người 2: kiểm tra order, item, seller và product đã xác thực."""

from __future__ import annotations

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

    for order_id in resolved_order_ids:
        # 1. get_order
        try:
            ev = await context.fetch(actor, "get_order", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            orders_data[order_id] = ev.data
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
        except Exception as exc:
            warnings.append(f"get_order_items failed for {order_id}: {exc}")

        # 3. get_sellers
        try:
            ev = await context.fetch(actor, "get_sellers", order_id=order_id)
            evidence_refs.append(ev.evidence_ref)
            sellers_data[order_id] = ev.data
            sellers_list = (
                ev.data.get("sellers", [])
                if isinstance(ev.data, dict)
                else (ev.data if isinstance(ev.data, list) else [])
            )
            for s in sellers_list:
                sid = s.get("seller_id") if isinstance(s, dict) else s
                if sid and sid not in all_seller_ids:
                    all_seller_ids.append(sid)
        except Exception as exc:
            warnings.append(f"get_sellers failed for {order_id}: {exc}")

        # 4. get_product_context
        if include_product:
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
    }

    return Finding(
        facts=facts,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        warnings=tuple(warnings),
    )
