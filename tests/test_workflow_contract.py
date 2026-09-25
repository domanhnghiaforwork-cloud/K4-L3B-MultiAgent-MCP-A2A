from __future__ import annotations

import asyncio
from typing import Any

from student_agent import workflow
from student_agent.interfaces import Finding


def test_coordinator_handoff_contract(monkeypatch: Any) -> None:
    calls: list[str] = []
    events: list[dict[str, Any]] = []
    gateway_calls: list[tuple[str, str]] = []

    class Gateway:
        async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
            gateway_calls.append((tool_name, case_id))
            return {
                "evidence_ref": "ev_12345678901234567890",
                "domain": "order",
                "data": {"order_id": arguments["order_id"]},
                "result_hash": "sha256:" + "a" * 64,
            }

    class Trace:
        def emit(self, **event: Any) -> dict[str, Any]:
            events.append(event)
            return event

    async def entity(case: dict[str, Any], context: Any) -> Finding:
        calls.append("entity")
        evidence = await context.fetch("entity-agent", "get_order", order_id="ORDER_1")
        return Finding({"resolved_order_ids": ["ORDER_1"]}, (evidence.evidence_ref,))

    async def order(case: dict[str, Any], context: Any, entity: Finding) -> Finding:
        calls.append("order")
        assert entity.facts["resolved_order_ids"] == ["ORDER_1"]
        evidence = await context.fetch("order-agent", "get_order", order_id="ORDER_1")
        return Finding({"affected_entities": {}}, (evidence.evidence_ref,))

    async def shipment(case: dict[str, Any], context: Any, *prior: Finding) -> Finding:
        calls.append("shipment")
        return Finding({"shipment_analysis": {}})

    async def payment(case: dict[str, Any], context: Any, *prior: Finding) -> Finding:
        calls.append("payment")
        return Finding({"payment_analysis": {}})

    async def conflicts(case: dict[str, Any], context: Any, findings: Any) -> Finding:
        calls.append("conflicts")
        assert set(findings) == {"entity", "order", "shipment", "payment"}
        return Finding({"data_conflicts": []})

    async def policy(case: dict[str, Any], context: Any, findings: Any) -> dict[str, Any]:
        calls.append("policy")
        assert "conflicts" in findings
        return {"case_id": case["case_id"], "evidence_refs": sorted(context.evidence_refs)}

    async def verifier(case: dict[str, Any], context: Any, findings: Any, output: Any) -> None:
        calls.append("verifier")
        assert output["case_id"] == case["case_id"]

    monkeypatch.setattr(workflow.entity, "investigate_entity", entity)
    monkeypatch.setattr(workflow.order, "investigate_order", order)
    monkeypatch.setattr(workflow.shipment, "investigate_shipment", shipment)
    monkeypatch.setattr(workflow.payment, "investigate_payment", payment)
    monkeypatch.setattr(workflow.conflicts, "resolve_conflicts", conflicts)
    monkeypatch.setattr(workflow.policy, "decide_policy", policy)
    monkeypatch.setattr(workflow.verifier, "verify_output", verifier)

    output = asyncio.run(workflow.solve_case({"case_id": "CASE_001"}, Gateway(), Trace()))
    assert output["evidence_refs"] == ["ev_12345678901234567890"]
    assert calls == ["entity", "order", "shipment", "payment", "conflicts", "policy", "verifier"]
    assert gateway_calls == [("get_order", "CASE_001")]
    kinds = [event["event_type"] for event in events]
    assert kinds.count("task_assigned") == 7
    assert kinds.count("handoff") == 6
    assert kinds.count("tool_result_consumed") == 2
    assert "policy_decided" in kinds
    assert kinds[-1] == "verification_completed"
