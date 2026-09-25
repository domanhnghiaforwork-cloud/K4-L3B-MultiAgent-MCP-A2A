from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.interfaces import CaseContext, Finding
from student_agent.policy import decide_policy


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    def __init__(self, policy_data: dict[str, Any] | None) -> None:
        self.policy_data = policy_data
        self.calls: list[tuple[str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, case_id))
        if tool_name == "get_policy" and self.policy_data is None:
            raise RuntimeError("policy provider unavailable")
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_shipment_summary": "shipment",
            "get_order_payments": "payment",
            "get_policy": "policy",
        }[tool_name]
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{len(self.calls):024d}",
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain,
            "data": self.policy_data if tool_name == "get_policy" else {},
        }


def case(topic: str) -> dict[str, Any]:
    return {
        "case_id": "CASE_POLICY_001",
        "policy_version": "EC_POLICY_V2",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-a", "topic": topic},
                {"claim_id": "claim-b", "topic": "requested_full_refund"},
            ]
        },
    }


async def setup(
    policy_data: dict[str, Any] | None,
    *,
    order_status: str = "delivered",
    shipment_verdict: str = "on_time",
    payment_verdict: str = "reconciled",
    split: bool = False,
    resolved: bool = True,
    conflicts: list[dict[str, Any]] | None = None,
) -> tuple[CaseContext, dict[str, Finding], FakeGateway]:
    gateway = FakeGateway(policy_data)
    context = CaseContext("CASE_POLICY_001", gateway, FakeTrace())
    refs = {}
    if resolved:
        for actor, tool in (
            ("entity-agent", "get_order"),
            ("order-agent", "get_order_items"),
            ("shipment-agent", "get_shipment_summary"),
            ("payment-agent", "get_order_payments"),
        ):
            evidence = await context.fetch(actor, tool, order_id="ORDER_1")
            refs[actor] = evidence.evidence_ref
    order_ids = ["ORDER_1"] if resolved else []
    findings = {
        "entity": Finding(
            {
                "resolved_order_ids": order_ids,
                "entity_resolution": {
                    "status": "resolved" if resolved else "not_found",
                    "resolved_order_ids": order_ids,
                    "rejected_candidates": [],
                    "confidence": 0.9 if resolved else 0.2,
                },
                "customer_context": {
                    "customer_unique_id": "CUSTOMER_1",
                    "related_order_ids": order_ids,
                },
            },
            (refs["entity-agent"],) if resolved else (),
        ),
        "order": Finding(
            {
                "affected_entities": {
                    "order_ids": order_ids,
                    "item_ids": ["ITEM_1"] if resolved else [],
                    "seller_ids": ["SELLER_1"] if resolved else [],
                },
                "orders_data": {"ORDER_1": {"order_status": order_status}} if resolved else {},
            },
            (refs["order-agent"],) if resolved else (),
        ),
        "shipment": Finding(
            {
                "shipment_analysis": {
                    "verdict": shipment_verdict,
                    "late_seller_ids": ["SELLER_1"] if shipment_verdict == "seller_delay" else [],
                    "timeline_complete": resolved,
                },
                "shipment_ids": ["SHIPMENT_1"] if resolved else [],
            },
            (refs["shipment-agent"],) if resolved else (),
        ),
        "payment": Finding(
            {
                "payment_analysis": {
                    "verdict": payment_verdict,
                    "captured_total_brl": 100.0 if resolved else None,
                    "refunded_total_brl": 20.0 if resolved else None,
                    "refundable_total_brl": 80.0 if resolved else None,
                },
                "payment_references": ["PAYMENT_1"] if resolved else [],
                "payment_facts": {
                    "split_payment": split,
                    "expected_payment_total_brl": 100.0,
                    "failed_refund_total_brl": 0.0,
                },
            },
            (refs["payment-agent"],) if resolved else (),
        ),
        "conflicts": Finding({"data_conflicts": conflicts or []}),
    }
    return context, findings, gateway


def validate(output: dict[str, Any]) -> None:
    root = Path(__file__).resolve().parents[1]
    Contracts(root / "contracts/schemas").validate_output(output, "policy test")


def test_paid_canceled_order_uses_authoritative_full_refund_rule() -> None:
    async def run() -> None:
        context, findings, gateway = await setup(
            {
                "policy_version": "EC_POLICY_V2",
                "refund_rules": [
                    {
                        "issue": "canceled_order_paid",
                        "refund": {"base": "remaining_captured", "fraction": 1},
                    }
                ],
            },
            order_status="canceled",
        )
        output = await decide_policy(case("canceled_order_paid"), context, findings)
        validate(output)
        assert output["assessment"]["primary_issue"] == "canceled_order_paid"
        assert output["assessment"]["case_status"] == "action_required"
        assert output["financial_resolution"]["recommended_refund_brl"] == 80.0
        assert output["financial_resolution"]["refund_lines"][0]["amount_brl"] == 80.0
        assert output["root_cause_analysis"]["responsible_parties"][0]["party_type"] == "platform"
        assert output["claim_assessments"][1]["verdict"] == "supported"
        assert gateway.calls[-1] == ("get_policy", "CASE_POLICY_001")
        context.require_refs(output["evidence_refs"])

    asyncio.run(run())


def test_seller_delay_without_policy_does_not_invent_refund() -> None:
    async def run() -> None:
        context, findings, _ = await setup(None, shipment_verdict="seller_delay")
        output = await decide_policy(case("late_delivery_seller"), context, findings)
        validate(output)
        assert output["assessment"]["primary_issue"] == "late_delivery_seller"
        assert output["assessment"]["case_status"] == "needs_investigation"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0
        assert output["root_cause_analysis"]["responsible_parties"] == [
            {"party_type": "seller", "party_id": "SELLER_1"}
        ]

    asyncio.run(run())


def test_valid_split_payment_requires_no_refund() -> None:
    async def run() -> None:
        context, findings, _ = await setup(None, split=True)
        output = await decide_policy(case("valid_split_payment"), context, findings)
        validate(output)
        assert output["assessment"]["primary_issue"] == "valid_split_payment"
        assert output["assessment"]["case_status"] == "no_action"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0
        assert output["claim_assessments"][1]["verdict"] == "unsupported"

    asyncio.run(run())


def test_unresolved_conflict_blocks_monetary_decision() -> None:
    async def run() -> None:
        context, findings, _ = await setup(
            {
                "policy_version": "EC_POLICY_V2",
                "refund_rules": [
                    {
                        "issue": "late_delivery_seller",
                        "refund": {"base": "remaining_captured", "fraction": 1},
                    }
                ],
            },
            shipment_verdict="seller_delay",
            conflicts=[
                {
                    "field": "delivery_date",
                    "sources": ["order", "shipment"],
                    "selected_source": None,
                    "resolution_code": "unresolved",
                }
            ],
        )
        output = await decide_policy(case("late_delivery_seller"), context, findings)
        validate(output)
        assert output["assessment"]["case_status"] == "needs_investigation"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0
        assert output["assessment"]["confidence"] < 0.86

    asyncio.run(run())


def test_unresolved_entity_does_not_call_policy_or_claim_order() -> None:
    async def run() -> None:
        context, findings, gateway = await setup(None, resolved=False)
        output = await decide_policy(case("duplicate_charge"), context, findings)
        validate(output)
        assert output["assessment"]["primary_issue"] == "insufficient_evidence"
        assert output["assessment"]["case_status"] == "needs_investigation"
        assert output["affected_entities"]["order_ids"] == []
        assert output["evidence_refs"] == []
        assert gateway.calls == []

    asyncio.run(run())


def test_policy_integrates_with_pulled_specialists() -> None:
    from student_agent.conflicts import resolve_conflicts
    from student_agent.entity import investigate_entity
    from student_agent.order import investigate_order
    from student_agent.payment import investigate_payment
    from student_agent.shipment import investigate_shipment
    from student_agent.verifier import verify_output
    from student_agent.workflow import solve_case

    class SpecialistGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            assert case_id == "CASE_POLICY_001"
            self.calls.append(tool_name)
            data = {
                "get_customer_history": {"orders": [{"order_id": "ORDER_1"}]},
                "get_order": {"order_id": "ORDER_1", "order_status": "canceled"},
                "get_order_items": {
                    "items": [
                        {
                            "order_item_id": "ITEM_1",
                            "seller_id": "SELLER_1",
                            "product_id": "PRODUCT_1",
                        }
                    ]
                },
                "get_sellers": {"sellers": [{"seller_id": "SELLER_1"}]},
                "get_shipment_summary": {
                    "shipment_id": "SHIPMENT_1",
                    "order_status": "canceled",
                    "events": [],
                },
                "get_order_payments": {
                    "payments": [
                        {
                            "payment_reference": "PAYMENT_1",
                            "payment_value": 100,
                            "status": "captured",
                        }
                    ]
                },
                "get_payment_timeline": {"payment_events": []},
                "get_refund_timeline": {"refund_events": []},
                "get_policy": {
                    "policy_version": "EC_POLICY_V2",
                    "refund_rules": [
                        {
                            "issue": "canceled_order_paid",
                            "refund": {"base": "remaining_captured", "fraction": 1},
                        }
                    ],
                },
            }
            domain = {
                "get_customer_history": "customer",
                "get_order": "order",
                "get_order_items": "item",
                "get_sellers": "seller",
                "get_shipment_summary": "shipment",
                "get_order_payments": "payment",
                "get_payment_timeline": "payment",
                "get_refund_timeline": "refund",
                "get_policy": "policy",
            }[tool_name]
            return {
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": f"ev_{len(self.calls):024d}",
                "result_hash": "sha256:" + "b" * 64,
                "domain": domain,
                "data": data[tool_name],
            }

    async def run() -> None:
        gateway = SpecialistGateway()
        context = CaseContext("CASE_POLICY_001", gateway, FakeTrace())
        input_case = {
            **case("canceled_order_paid"),
            "candidate_order_ids": ["ORDER_1"],
            "customer_unique_id_hint": "CUSTOMER_1",
            "investigation_scope": {"include_product_context": True},
        }
        entity = await investigate_entity(input_case, context)
        order = await investigate_order(input_case, context, entity)
        shipment = await investigate_shipment(input_case, context, entity, order)
        payment = await investigate_payment(input_case, context, entity, order)
        findings = {
            "entity": entity,
            "order": order,
            "shipment": shipment,
            "payment": payment,
        }
        findings["conflicts"] = await resolve_conflicts(input_case, context, findings)
        output = await decide_policy(input_case, context, findings)
        await verify_output(input_case, context, findings, output)
        validate(output)
        assert output["assessment"]["primary_issue"] == "canceled_order_paid"
        assert output["financial_resolution"]["recommended_refund_brl"] == 100
        assert output["affected_entities"]["item_ids"] == ["ITEM_1"]
        assert output["affected_entities"]["payment_references"] == ["PAYMENT_1"]
        assert output["affected_entities"]["shipment_ids"] == ["SHIPMENT_1"]
        assert gateway.calls.count("get_policy") == 1
        context.require_refs(output["evidence_refs"])

        trace = FakeTrace()
        full_output = await solve_case(input_case, SpecialistGateway(), trace)
        validate(full_output)
        assert full_output["financial_resolution"]["recommended_refund_brl"] == 100
        assert [event["event_type"] for event in trace.events][-1] == "verification_completed"

    asyncio.run(run())


def test_conflict_resolver_chooses_order_status_and_keeps_internal_timeline_unresolved() -> None:
    from student_agent.conflicts import resolve_conflicts

    async def run() -> None:
        context, findings, _ = await setup(None)
        findings["order"].facts["orders_data"] = {"ORDER_1": {"order_status": "canceled"}}
        findings["shipment"].facts["shipments_data"] = {"ORDER_1": {"order_status": "delivered"}}
        findings["shipment"].facts["conflicts"] = [
            {
                "order_id": "ORDER_1",
                "field": "delivered_customer_at_vs_events",
                "reason": "delivered_on_time_but_event_asserts_late",
            }
        ]
        conflict = await resolve_conflicts(case("late_delivery_seller"), context, findings)
        rows = conflict.facts["data_conflicts"]
        assert len(rows) == 2
        assert rows[0]["selected_source"] == "get_order"
        assert rows[1]["selected_source"] is None
        assert rows[1]["field"] in conflict.facts["unresolved_fields"]
        context.require_refs(conflict.evidence_refs)

    asyncio.run(run())


def test_verifier_rejects_refund_above_remaining_capture() -> None:
    import pytest

    from student_agent.verifier import verify_output

    async def run() -> None:
        context, findings, _ = await setup(
            {
                "policy_version": "EC_POLICY_V2",
                "refund_rules": [
                    {
                        "issue": "canceled_order_paid",
                        "refund": {"base": "remaining_captured", "fraction": 1},
                    }
                ],
            },
            order_status="canceled",
        )
        input_case = case("canceled_order_paid")
        output = await decide_policy(input_case, context, findings)
        output["financial_resolution"]["recommended_refund_brl"] = 90.0
        output["financial_resolution"]["refund_lines"][0]["amount_brl"] = 90.0
        with pytest.raises(ValueError, match="refund exceeds captured balance"):
            await verify_output(input_case, context, findings, output)

    asyncio.run(run())


def test_verifier_rejects_ref_from_another_case() -> None:
    import pytest

    from student_agent.verifier import verify_output

    async def run() -> None:
        context, findings, _ = await setup(None, split=True)
        input_case = case("valid_split_payment")
        output = await decide_policy(input_case, context, findings)
        output["evidence_refs"].append("ev_999999999999999999999999")
        with pytest.raises(ValueError, match="not fetched"):
            await verify_output(input_case, context, findings, output)

    asyncio.run(run())


def test_verifier_rejects_wrong_responsible_party() -> None:
    import pytest

    from student_agent.verifier import verify_output

    async def run() -> None:
        context, findings, _ = await setup(None, shipment_verdict="seller_delay")
        input_case = case("late_delivery_seller")
        output = await decide_policy(input_case, context, findings)
        output["root_cause_analysis"]["responsible_parties"] = [
            {
                "party_type": "logistics_provider",
                "party_id": None,
            }
        ]
        with pytest.raises(ValueError, match="responsibility conflicts"):
            await verify_output(input_case, context, findings, output)

    asyncio.run(run())
