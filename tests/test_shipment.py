from __future__ import annotations

import asyncio
from typing import Any
import pytest
from jsonschema import Draft202012Validator

from student_agent.contracts import Contracts
from student_agent.interfaces import CaseContext, Finding
from student_agent.shipment import investigate_shipment


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return kwargs


class FakeGateway:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        order_id = arguments.get("order_id", "default")
        return {
            "evidence_ref": f"ev_{tool_name}_{order_id[:10]}_{'a' * 20}",
            "domain": "shipment",
            "data": self.data.get(order_id, {}),
            "result_hash": "sha256:" + "0" * 64,
        }


def _validate_schema(analysis: dict[str, Any], root_contracts: Contracts) -> None:
    dummy_output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "TEST_CASE_001",
        "assessment": {
            "primary_issue": "late_delivery_seller",
            "secondary_issues": [],
            "case_status": "no_action",
            "confidence": 1.0,
        },
        "affected_entities": {
            "order_ids": ["order_1"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": ["shipment-order_1"],
        },
        "entity_resolution": {
            "status": "resolved",
            "resolved_order_ids": ["order_1"],
            "rejected_candidates": [],
            "confidence": 1.0,
        },
        "customer_context": {
            "customer_unique_id": "cust_1",
            "related_order_ids": ["order_1"],
        },
        "shipment_analysis": analysis,
        "payment_analysis": {
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": "SELLER_DELAY", "rank": 1}],
            "responsible_parties": [{"party_type": "seller", "party_id": "seller_1"}],
        },
        "evidence_refs": ["ev_12345678901234567890"],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": ["no_action_needed"],
    }
    root_contracts.validate_output(dummy_output, "test_output")


def test_shipment_no_resolved_orders() -> None:
    case = {"case_id": "CASE_NO_ORDERS"}
    ctx = CaseContext("CASE_NO_ORDERS", FakeGateway({}), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": []})
    order = Finding(facts={"affected_entities": {"order_ids": []}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    assert finding.facts["shipment_analysis"]["verdict"] == "insufficient_evidence"
    assert finding.facts["shipment_analysis"]["late_seller_ids"] == []
    assert finding.facts["shipment_analysis"]["timeline_complete"] is False
    assert len(finding.evidence_refs) == 0


def test_shipment_on_time(tmp_path: Any) -> None:
    order_id = "order_1234567890ab"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
            "delivered_customer_at": "2018-05-20T09:00:00-03:00",
            "estimated_delivery_at": "2018-05-22T09:00:00-03:00",
            "shipping_limits": [
                {
                    "seller_id": "seller_abc",
                    "shipping_limit_at": "2018-05-14T09:00:00-03:00",
                }
            ],
            "events": [],
        }
    }
    case = {"case_id": "CASE_ON_TIME"}
    ctx = CaseContext("CASE_ON_TIME", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    analysis = finding.facts["shipment_analysis"]
    assert analysis["verdict"] == "on_time"
    assert analysis["late_seller_ids"] == []
    assert analysis["timeline_complete"] is True
    assert len(finding.evidence_refs) == 1


def test_shipment_seller_delay() -> None:
    order_id = "order_seller_late"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-07-15T09:00:00-03:00",  # late handoff!
            "delivered_customer_at": "2018-07-25T09:00:00-03:00",
            "estimated_delivery_at": "2018-07-20T09:00:00-03:00",
            "shipping_limits": [
                {
                    "seller_id": "seller_slow",
                    "shipping_limit_at": "2018-07-10T09:00:00-03:00",
                }
            ],
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": "seller",
                    "status": "confirmed",
                }
            ],
        }
    }
    case = {"case_id": "CASE_SELLER_LATE"}
    ctx = CaseContext("CASE_SELLER_LATE", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    analysis = finding.facts["shipment_analysis"]
    assert analysis["verdict"] == "seller_delay"
    assert analysis["late_seller_ids"] == ["seller_slow"]
    assert analysis["timeline_complete"] is True


def test_shipment_logistics_delay() -> None:
    order_id = "order_carrier_late"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-08-05T09:00:00-03:00",  # on time handoff!
            "delivered_customer_at": "2018-08-20T09:00:00-03:00",  # late delivery to customer!
            "estimated_delivery_at": "2018-08-15T09:00:00-03:00",
            "shipping_limits": [
                {
                    "seller_id": "seller_good",
                    "shipping_limit_at": "2018-08-06T09:00:00-03:00",
                }
            ],
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "status": "confirmed",
                }
            ],
        }
    }
    case = {"case_id": "CASE_CARRIER_LATE"}
    ctx = CaseContext("CASE_CARRIER_LATE", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    analysis = finding.facts["shipment_analysis"]
    assert analysis["verdict"] == "logistics_delay"
    assert analysis["late_seller_ids"] == []
    assert analysis["timeline_complete"] is True


def test_shipment_conflicting_events() -> None:
    order_id = "order_conflict"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
            "delivered_customer_at": "2018-05-20T09:00:00-03:00",  # on time!
            "estimated_delivery_at": "2018-05-21T09:00:00-03:00",
            "shipping_limits": [
                {
                    "seller_id": "seller_1",
                    "shipping_limit_at": "2018-05-14T09:00:00-03:00",
                }
            ],
            "events": [
                {
                    "event_type": "delivered_late",
                    "actor": "logistics_provider",
                    "status": "confirmed",
                }
            ],
        }
    }
    case = {"case_id": "CASE_CONFLICT"}
    ctx = CaseContext("CASE_CONFLICT", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    analysis = finding.facts["shipment_analysis"]
    assert analysis["verdict"] == "conflicting"
    assert len(finding.facts["conflicts"]) > 0


def test_shipment_lost() -> None:
    order_id = "order_lost"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "lost",
            "delivered_carrier_at": "2018-06-01T09:00:00-03:00",
            "delivered_customer_at": None,
            "estimated_delivery_at": "2018-06-10T09:00:00-03:00",
            "shipping_limits": [
                {"seller_id": "seller_1", "shipping_limit_at": "2018-06-02T09:00:00-03:00"}
            ],
            "events": [{"event_type": "lost", "status": "confirmed"}],
        }
    }
    case = {"case_id": "CASE_LOST"}
    ctx = CaseContext("CASE_LOST", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    analysis = finding.facts["shipment_analysis"]
    assert analysis["verdict"] == "lost"
    assert analysis["timeline_complete"] is False


def test_shipment_schema_compliance() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")

    order_id = "order_schema_test"
    mcp_data = {
        order_id: {
            "order_id": order_id,
            "order_status": "delivered",
            "delivered_carrier_at": "2018-05-13T09:00:00-03:00",
            "delivered_customer_at": "2018-05-20T09:00:00-03:00",
            "estimated_delivery_at": "2018-05-22T09:00:00-03:00",
            "shipping_limits": [
                {"seller_id": "seller_test", "shipping_limit_at": "2018-05-14T09:00:00-03:00"}
            ],
            "events": [],
        }
    }
    case = {"case_id": "CASE_SCHEMA"}
    ctx = CaseContext("CASE_SCHEMA", FakeGateway(mcp_data), FakeTrace())
    entity = Finding(facts={"resolved_order_ids": [order_id]})
    order = Finding(facts={"affected_entities": {"order_ids": [order_id]}})

    finding = asyncio.run(investigate_shipment(case, ctx, entity, order))
    _validate_schema(finding.facts["shipment_analysis"], contracts)
