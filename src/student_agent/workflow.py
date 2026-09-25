"""Người 1: điều phối các module, handoff và evidence theo từng case."""

from __future__ import annotations

from typing import Any

from . import conflicts, entity, order, payment, policy, shipment, verifier
from .interfaces import CaseContext, Finding
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinate specialists for one case; CLI emits receive/finalize events."""
    case_id = case["case_id"]
    context = CaseContext(case_id=case_id, gateway=gateway, trace=trace)
    findings: dict[str, Finding] = {}
    previous_actor: str | None = None

    def assign(actor: str) -> None:
        nonlocal previous_actor
        if previous_actor is not None:
            trace.emit(case_id=case_id, event_type="handoff", actor=previous_actor, target=actor)
        trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target=actor)
        previous_actor = actor

    def record(name: str, finding: Finding) -> None:
        if not isinstance(finding, Finding):
            raise TypeError(f"{name} must return Finding")
        context.require_refs(finding.evidence_refs)
        findings[name] = finding

    assign("entity-agent")
    record("entity", await entity.investigate_entity(case, context))

    assign("order-agent")
    record("order", await order.investigate_order(case, context, findings["entity"]))

    assign("shipment-agent")
    record(
        "shipment",
        await shipment.investigate_shipment(
            case, context, findings["entity"], findings["order"]
        ),
    )

    assign("payment-agent")
    record(
        "payment",
        await payment.investigate_payment(case, context, findings["entity"], findings["order"]),
    )

    assign("conflict-agent")
    record("conflicts", await conflicts.resolve_conflicts(case, context, findings))

    assign("policy-agent")
    output = await policy.decide_policy(case, context, findings)
    if not isinstance(output, dict) or output.get("case_id") != case_id:
        raise ValueError("policy must return an L3B output for the current case")
    context.require_refs(output.get("evidence_refs", []))
    for claim in output.get("claim_assessments", []):
        context.require_refs(claim.get("evidence_refs", []))
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent")

    assign("verifier-agent")
    await verifier.verify_output(case, context, findings, output)
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier-agent")
    return output
