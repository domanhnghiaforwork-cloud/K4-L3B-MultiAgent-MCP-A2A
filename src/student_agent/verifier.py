"""Independent schema, provenance, and cross-field checks before finalization."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .contracts import Contracts
from .interfaces import CaseContext, Finding

SCHEMAS = Path(__file__).resolve().parents[2] / "contracts" / "schemas"
ISSUE_PARTY = {
    "duplicate_charge": "payment_provider",
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "platform",
    "refund_failed": "payment_provider",
    "payment_mismatch": "payment_provider",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "refund_pending": "payment_provider",
    "valid_split_payment": "customer",
    "unsupported_claim": "customer",
}
PAYMENT_ISSUES = {
    "duplicate_charge": "duplicate_capture",
    "payment_mismatch": "capture_mismatch",
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
}
SHIPMENT_ISSUES = {
    "late_delivery_seller": "seller_delay",
    "late_delivery_logistics": "logistics_delay",
}


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid financial amount") from exc
    if not result.is_finite() or result < 0:
        raise ValueError("financial amount must be finite and nonnegative")
    return result


def _ids(value: Any) -> set[str]:
    return set(value) if isinstance(value, (tuple, list)) else set()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


async def verify_output(
    case: dict[str, Any],
    context: CaseContext,
    findings: dict[str, Finding],
    output: dict[str, Any],
) -> None:
    """Raise on malformed, cross-case, unsupported, or inconsistent output."""
    _require(case.get("case_id") == context.case_id, "verifier case_id does not match context")
    _require(isinstance(output, dict), "verifier requires an output object")
    _require(output.get("case_id") == context.case_id, "output case_id does not match context")
    Contracts(SCHEMAS).validate_output(output, "policy output")
    required = ("entity", "order", "shipment", "payment", "conflicts")
    _require(
        all(isinstance(findings.get(name), Finding) for name in required),
        "verifier requires all five specialist findings",
    )
    entity, order, shipment, payment, conflicts = (findings[name] for name in required)

    output_refs = set(output["evidence_refs"])
    context.require_refs(output["evidence_refs"])
    for name in required:
        context.require_refs(findings[name].evidence_refs)
        _require(
            set(findings[name].evidence_refs) <= output_refs,
            f"{name} evidence was omitted from output",
        )
    for claim in output.get("claim_assessments", []):
        context.require_refs(claim["evidence_refs"])
        _require(set(claim["evidence_refs"]) <= output_refs, "claim references omitted evidence")
    input_claims = {
        claim.get("claim_id")
        for claim in case.get("customer_request", {}).get("claims", [])
        if isinstance(claim, dict)
    }
    claim_ids = [row["claim_id"] for row in output.get("claim_assessments", [])]
    _require(len(claim_ids) == len(set(claim_ids)), "duplicate claim assessment")
    _require(set(claim_ids) <= input_claims, "assessment refers to an unknown claim")

    for field, finding, key in (
        ("entity_resolution", entity, "entity_resolution"),
        ("customer_context", entity, "customer_context"),
        ("shipment_analysis", shipment, "shipment_analysis"),
        ("payment_analysis", payment, "payment_analysis"),
    ):
        _require(
            output[field] == finding.facts.get(key), f"{field} differs from specialist finding"
        )
    _require(
        output["data_conflicts"] == conflicts.facts.get("data_conflicts", [])[:5],
        "data_conflicts differs from conflict finding",
    )
    for row in output["data_conflicts"]:
        selected = row["selected_source"]
        _require(
            selected is None or selected in row["sources"], "selected conflict source is unknown"
        )
    if output["data_conflicts"]:
        _require(bool(conflicts.evidence_refs), "data conflict has no supporting evidence")

    affected = output["affected_entities"]
    resolved = _ids(entity.facts.get("resolved_order_ids", []))
    rejected = _ids(entity.facts.get("rejected_candidates", []))
    order_entities = order.facts.get("affected_entities", {})
    _require(isinstance(order_entities, dict), "order finding has invalid affected_entities")
    _require(_ids(affected["order_ids"]) == resolved, "affected orders differ from resolved orders")
    _require(not (_ids(affected["order_ids"]) & rejected), "rejected candidate is affected")
    _require(_ids(affected["item_ids"]) <= _ids(order_entities.get("item_ids")), "unknown item ID")
    _require(
        _ids(affected["seller_ids"]) <= _ids(order_entities.get("seller_ids")), "unknown seller ID"
    )
    _require(
        _ids(affected["payment_references"]) <= _ids(payment.facts.get("payment_references")),
        "unknown payment reference",
    )
    _require(
        _ids(affected["shipment_ids"]) <= _ids(shipment.facts.get("shipment_ids")),
        "unknown shipment ID",
    )
    if affected["item_ids"] or affected["seller_ids"]:
        _require(bool(order.evidence_refs), "order entities have no evidence")
    if affected["payment_references"]:
        _require(bool(payment.evidence_refs), "payment references have no evidence")
    if affected["shipment_ids"]:
        _require(bool(shipment.evidence_refs), "shipment IDs have no evidence")

    issue = output["assessment"]["primary_issue"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    expected_party = ISSUE_PARTY.get(issue)
    if expected_party is not None:
        _require(
            any(p["party_type"] == expected_party for p in parties),
            "responsibility conflicts with primary issue",
        )
    if issue == "late_delivery_seller":
        _require(
            any(
                p["party_type"] == "seller" and p["party_id"] in affected["seller_ids"]
                for p in parties
            ),
            "seller responsibility must name an affected seller",
        )
    if issue in PAYMENT_ISSUES:
        _require(
            payment.facts.get("payment_analysis", {}).get("verdict") == PAYMENT_ISSUES[issue]
            and bool(payment.evidence_refs),
            "payment issue is unsupported by payment finding",
        )
    if issue in SHIPMENT_ISSUES:
        _require(
            shipment.facts.get("shipment_analysis", {}).get("verdict") == SHIPMENT_ISSUES[issue]
            and bool(shipment.evidence_refs),
            "delivery issue is unsupported by shipment finding",
        )
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        statuses = {
            str(row.get("order_status") or row.get("status") or "").lower()
            for row in order.facts.get("orders_data", {}).values()
            if isinstance(row, dict)
        }
        accepted = (
            {"canceled", "cancelled"}
            if issue == "canceled_order_paid"
            else {"unavailable", "unavailable_order"}
        )
        captured = payment.facts.get("payment_analysis", {}).get("captured_total_brl")
        _require(
            bool(statuses & accepted)
            and captured is not None
            and _money(captured) > 0
            and bool(order.evidence_refs)
            and bool(payment.evidence_refs),
            "paid order issue lacks order/payment evidence",
        )
    if issue == "valid_split_payment":
        _require(
            payment.facts.get("payment_analysis", {}).get("verdict") == "reconciled"
            and payment.facts.get("payment_facts", {}).get("split_payment") is True
            and bool(payment.evidence_refs),
            "split payment finding does not support primary issue",
        )

    financial = output["financial_resolution"]
    refund = _money(financial["recommended_refund_brl"])
    line_total = sum((_money(line["amount_brl"]) for line in financial["refund_lines"]), Decimal(0))
    _require(line_total == refund, "refund lines do not sum to recommended amount")
    remaining = payment.facts.get("payment_analysis", {}).get("refundable_total_brl")
    if refund > 0:
        _require(
            remaining is not None and refund <= _money(remaining), "refund exceeds captured balance"
        )
        _require(len(resolved) == 1, "monetary remedy requires one resolved order")
        _require(issue != "refund_pending", "pending refund must not be issued twice")
        _require("issue_refund" in output["resolution_actions"], "refund action is missing")
    else:
        _require(
            "issue_refund" not in output["resolution_actions"],
            "zero refund cannot request issue_refund",
        )
    for line in financial["refund_lines"]:
        _require(line["entity_id"] in resolved, "refund line refers to another order")
    status = output["assessment"]["case_status"]
    if status in {"no_action", "needs_investigation"}:
        _require(refund == 0, "non-actionable case has a refund")
    if status == "no_action":
        _require(output["resolution_actions"] == ["no_action_needed"], "no_action has an action")
    if status == "needs_investigation":
        _require(
            "no_action_needed" not in output["resolution_actions"], "investigation marked no_action"
        )
