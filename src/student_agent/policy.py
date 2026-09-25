"""Combine case findings with authoritative policy evidence into L3B output."""

from __future__ import annotations

from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .interfaces import CaseContext, Finding

CENT = Decimal("0.01")
ISSUES = (
    "duplicate_charge",
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "payment_mismatch",
    "late_delivery_seller",
    "late_delivery_logistics",
    "refund_pending",
    "valid_split_payment",
    "unsupported_claim",
)
PARTIES = {
    "duplicate_charge": "payment_provider",
    "canceled_order_paid": "platform",
    "unavailable_order_paid": "platform",
    "refund_failed": "payment_provider",
    "payment_mismatch": "payment_provider",
    "late_delivery_seller": "seller",
    "late_delivery_logistics": "logistics_provider",
    "refund_pending": "payment_provider",
    "unsupported_claim": "customer",
    "valid_split_payment": "customer",
}
ACTIONS = {
    "duplicate_charge": "investigate_duplicate_charge",
    "canceled_order_paid": "resolve_paid_canceled_order",
    "unavailable_order_paid": "resolve_paid_unavailable_order",
    "refund_failed": "investigate_failed_refund",
    "payment_mismatch": "reconcile_payment_capture",
    "late_delivery_seller": "contact_seller_about_delay",
    "late_delivery_logistics": "contact_logistics_provider_about_delay",
    "refund_pending": "monitor_pending_refund",
}


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        return None
    return amount


def _ids(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(x for x in value if isinstance(x, str) and x))[:20]


def _refs(*findings: Finding) -> list[str]:
    return list(dict.fromkeys(ref for f in findings for ref in f.evidence_refs))


def _observed(findings: dict[str, Finding], order_ids: list[str]) -> dict[str, list[str]]:
    if not order_ids:
        return {}
    entity, order, shipment, payment = (
        findings[k] for k in ("entity", "order", "shipment", "payment")
    )
    pa = payment.facts.get("payment_analysis", {})
    pf = payment.facts.get("payment_facts", {})
    sa = shipment.facts.get("shipment_analysis", {})
    pa = pa if isinstance(pa, dict) else {}
    pf = pf if isinstance(pf, dict) else {}
    sa = sa if isinstance(sa, dict) else {}
    result: dict[str, list[str]] = {}
    payment_codes = {
        "duplicate_capture": "duplicate_charge",
        "capture_mismatch": "payment_mismatch",
        "refund_failed": "refund_failed",
        "refund_pending": "refund_pending",
    }
    if payment.evidence_refs:
        code = payment_codes.get(pa.get("verdict"))
        if code:
            result[code] = _refs(entity, payment)
        if pa.get("verdict") == "reconciled" and pf.get("split_payment") is True:
            result["valid_split_payment"] = _refs(entity, payment)
    if shipment.evidence_refs:
        code = {
            "seller_delay": "late_delivery_seller",
            "logistics_delay": "late_delivery_logistics",
        }.get(sa.get("verdict"))
        if code:
            result[code] = _refs(entity, shipment)
    if (
        order.evidence_refs
        and payment.evidence_refs
        and (_money(pa.get("captured_total_brl")) or 0) > 0
    ):
        rows = order.facts.get("orders_data", {})
        if isinstance(rows, dict):
            statuses = {
                str(
                    (rows.get(oid) or {}).get("order_status")
                    or (rows.get(oid) or {}).get("status")
                    or ""
                ).lower()
                for oid in order_ids
                if isinstance(rows.get(oid), dict)
            }
            if statuses & {"canceled", "cancelled"}:
                result["canceled_order_paid"] = _refs(entity, order, payment)
            if statuses & {"unavailable", "unavailable_order"}:
                result["unavailable_order_paid"] = _refs(entity, order, payment)
    return result


def _issue(case: dict[str, Any], observed: dict[str, list[str]]) -> str:
    claims = case.get("customer_request", {}).get("claims", [])
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, dict) and claim.get("topic") in observed:
                return claim["topic"]
        for claim in claims:
            if isinstance(claim, dict) and claim.get("topic") == "unsupported_claim":
                return "unsupported_claim"
    return next((code for code in ISSUES if code in observed), "insufficient_evidence")


def _refund_rule(policy_data: Any, issue: str) -> dict[str, Any] | None:
    """Accept explicit MCP refund_rules/rules entries keyed by issue code."""
    if not isinstance(policy_data, dict):
        return None
    rules = policy_data.get("refund_rules") or policy_data.get("rules")
    if isinstance(rules, dict):
        entry = rules.get(issue)
        if isinstance(entry, dict):
            return entry
    if isinstance(rules, list):
        return next(
            (
                row
                for row in rules
                if isinstance(row, dict) and (row.get("primary_issue") or row.get("issue")) == issue
            ),
            None,
        )
    return None


def _refund_amount(rule: dict[str, Any] | None, payment: Finding) -> Decimal | None:
    """Require a rule with an explicit base and rate; cap by the unpaid capture."""
    if rule is None:
        return None
    pa, pf = payment.facts.get("payment_analysis", {}), payment.facts.get("payment_facts", {})
    if not isinstance(pa, dict) or not isinstance(pf, dict):
        return None
    remaining = _money(pa.get("refundable_total_brl"))
    captured = _money(pa.get("captured_total_brl"))
    if remaining is None:
        return None

    if "refund_brl" in rule:
        val = _money(rule["refund_brl"])
        if val is not None:
            return min(remaining, val)

    remedy = rule.get("refund") if isinstance(rule.get("refund"), dict) else rule
    if not isinstance(remedy, dict):
        return None
    rate_value = remedy.get("fraction", remedy.get("refund_fraction"))
    if rate_value is None and remedy.get("refund_percent") is not None:
        try:
            rate_value = Decimal(str(remedy["refund_percent"])) / 100
        except (InvalidOperation, ValueError):
            return None
    try:
        rate = Decimal(str(rate_value))
    except (InvalidOperation, ValueError):
        return None
    if not rate.is_finite() or not 0 <= rate <= 1:
        return None
    expected = _money(pf.get("expected_payment_total_brl"))
    base_name = remedy.get("base")
    if base_name == "remaining_captured":
        base = remaining
    elif base_name == "excess_capture" and captured is not None and expected is not None:
        base = max(Decimal(0), captured - expected)
    elif base_name == "failed_refund":
        base = _money(pf.get("failed_refund_total_brl"))
    else:
        return None
    return (
        min(remaining, (base * rate).quantize(CENT, rounding=ROUND_HALF_UP))
        if base is not None
        else None
    )


def _confidence(issue: str, findings: dict[str, Finding], policy_ok: bool, conflict: bool) -> float:
    if issue == "insufficient_evidence":
        return 0.2
    entity_score = findings["entity"].facts.get("entity_resolution", {}).get("confidence", 1.0)
    if isinstance(entity_score, (int, float)) and entity_score < 0.8:
        return 0.5
    if conflict:
        return 0.85
    if not policy_ok:
        return 0.70
    return 0.95


async def decide_policy(
    case: dict[str, Any], context: CaseContext, findings: dict[str, Finding]
) -> dict[str, Any]:
    """Produce the complete schema output without treating a claim as evidence."""
    if case.get("case_id") != context.case_id:
        raise ValueError("policy case_id does not match context")
    required = ("entity", "order", "shipment", "payment", "conflicts")
    if any(not isinstance(findings.get(name), Finding) for name in required):
        raise ValueError("policy requires all five specialist findings")
    for name in required:
        context.require_refs(findings[name].evidence_refs)
    entity, order, shipment, payment, conflicts = (findings[name] for name in required)
    resolution = entity.facts.get("entity_resolution", {})
    order_ids = _ids(entity.facts.get("resolved_order_ids", []))

    policy_evidence = None
    if order_ids and isinstance(case.get("policy_version"), str):
        with suppress(RuntimeError):
            policy_evidence = await context.fetch(
                "policy-agent", "get_policy", policy_version=case["policy_version"]
            )
    policy_data = None
    if policy_evidence is not None and policy_evidence.domain == "policy":
        data = policy_evidence.data
        if isinstance(data, dict) and data.get(
            "policy_version", case.get("policy_version")
        ) == case.get("policy_version"):
            policy_data = data

    observed = _observed(findings, order_ids)
    issue = _issue(case, observed)
    conflict_rows = conflicts.facts.get("data_conflicts", [])
    if not isinstance(conflict_rows, list):
        raise ValueError("conflict finding must provide a data_conflicts list")
    has_conflict = bool(conflict_rows or shipment.facts.get("conflicts", []))
    unresolved_conflict = any(
        isinstance(row, dict) and row.get("selected_source") is None for row in conflict_rows
    ) or bool(shipment.facts.get("conflicts", []) and not conflict_rows)
    if not observed and order_ids and not has_conflict:
        sa, pa = (
            shipment.facts.get("shipment_analysis", {}),
            payment.facts.get("payment_analysis", {}),
        )
        if (
            shipment.evidence_refs
            and payment.evidence_refs
            and isinstance(sa, dict)
            and isinstance(pa, dict)
            and sa.get("verdict") == "on_time"
            and pa.get("verdict") in {"reconciled", "refunded"}
        ):
            issue = "unsupported_claim"

    rule = _refund_rule(policy_data, issue)
    refund = _refund_amount(rule, payment) if len(order_ids) == 1 else None
    if issue in {"valid_split_payment", "unsupported_claim"}:
        status = "no_action"
        amount = Decimal(0)
    elif issue == "refund_pending":
        status = "needs_investigation"
        amount = Decimal(0)
    elif issue == "insufficient_evidence" or unresolved_conflict:
        status = "needs_investigation"
        amount = Decimal(0)
    elif rule is not None and "case_status" in rule:
        status = rule["case_status"]
        amount = refund if status == "action_required" and refund is not None else Decimal(0)
    elif policy_data is None or refund is None:
        status = "needs_investigation"
        amount = Decimal(0)
    else:
        status = "action_required"
        amount = refund if refund is not None else Decimal(0)

    policy_ref = [policy_evidence.evidence_ref] if policy_data is not None else []
    all_refs = list(dict.fromkeys(_refs(entity, order, shipment, payment, conflicts) + policy_ref))
    if len(all_refs) > 30:
        raise ValueError("policy output exceeds schema limit of 30 evidence refs")
    confidence = _confidence(issue, findings, policy_data is not None, unresolved_conflict)
    claims = case.get("customer_request", {}).get("claims", [])
    claim_assessments = []
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            pa = payment.facts.get("payment_analysis", {})
            remaining = _money(pa.get("refundable_total_brl")) if isinstance(pa, dict) else None
            refs = list(dict.fromkeys(list(payment.evidence_refs) + policy_ref))
            if status == "needs_investigation" and issue != "refund_pending":
                verdict = "insufficient_evidence"
            elif amount > 0 and remaining is not None and amount >= remaining:
                verdict = "supported"
            elif amount > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif topic in observed or topic == issue:
            if issue == "unsupported_claim":
                verdict, refs = "unsupported", _refs(entity, order, shipment, payment)
            else:
                verdict = "supported"
                refs = observed.get(topic, _refs(entity, order, shipment, payment))
        elif issue == "insufficient_evidence" or unresolved_conflict:
            verdict, refs = "insufficient_evidence", _refs(entity, order, shipment, payment)
        else:
            verdict, refs = "unsupported", _refs(entity, order, shipment, payment)
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": verdict,
                "confidence": confidence if refs else min(confidence, 0.35),
                "evidence_refs": refs[:30],
            }
        )
        if len(claim_assessments) == 5:
            break

    affected = order.facts.get("affected_entities", {})
    affected = affected if isinstance(affected, dict) else {}
    seller_ids = _ids(affected.get("seller_ids", []))
    party = PARTIES.get(issue, "unknown")
    ranked_issues = [issue, *[code for code in ISSUES if code in observed and code != issue]]
    causes = [
        code
        for code in ranked_issues
        if code not in {"insufficient_evidence", "unsupported_claim", "valid_split_payment"}
    ][:5]
    ranked = [{"cause_code": code.upper(), "rank": index} for index, code in enumerate(causes, 1)]
    if status == "no_action":
        actions = ["no_action_needed"]
    elif status == "needs_investigation":
        actions = (
            ["monitor_pending_refund"]
            if issue == "refund_pending"
            else ["investigate_missing_or_conflicting_evidence"]
        )
    else:
        primary_action = ACTIONS.get(issue, "review_case")
        actions = [primary_action]
        if amount > 0 and "issue_refund" not in actions:
            actions.append("issue_refund")

    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": context.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [code for code in ISSUES if code in observed and code != issue][
                :10
            ],
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": _ids(affected.get("order_ids", order_ids)),
            "item_ids": _ids(affected.get("item_ids", [])),
            "seller_ids": seller_ids,
            "payment_references": _ids(payment.facts.get("payment_references", [])),
            "shipment_ids": _ids(shipment.facts.get("shipment_ids", [])),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": resolution,
        "customer_context": entity.facts.get("customer_context", {}),
        "shipment_analysis": shipment.facts.get("shipment_analysis", {}),
        "payment_analysis": payment.facts.get("payment_analysis", {}),
        "root_cause_analysis": {
            "ranked_causes": ranked,
            "responsible_parties": [
                {
                    "party_type": party,
                    "party_id": seller_ids[0] if party == "seller" and seller_ids else None,
                }
            ],
        },
        "evidence_refs": all_refs,
        "data_conflicts": conflict_rows[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": float(amount),
            "refund_lines": (
                [{"reason_code": issue, "amount_brl": float(amount), "entity_id": order_ids[0]}]
                if amount > 0 and len(order_ids) == 1
                else []
            ),
        },
        "resolution_actions": actions,
    }
    context.require_refs(output["evidence_refs"])
    for claim in claim_assessments:
        context.require_refs(claim["evidence_refs"])
    return output
