from __future__ import annotations

import asyncio
from typing import Any

from student_agent.interfaces import CaseContext


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return {
            "evidence_ref": "ev_12345678901234567890",
            "domain": "order",
            "data": {"order_id": arguments["order_id"]},
            "result_hash": "sha256:" + "a" * 64,
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_case_context_caches_calls_but_traces_each_consumer() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    context = CaseContext("CASE_001", gateway, trace)

    async def run() -> None:
        first = await context.fetch("entity-agent", "get_order", order_id="ORDER_1")
        second = await context.fetch("order-agent", "get_order", order_id="ORDER_1")
        assert first is second

    asyncio.run(run())
    assert gateway.calls == [("get_order", "CASE_001", {"order_id": "ORDER_1"})]
    assert context.call_count == 1
    assert [event["actor"] for event in trace.events] == ["entity-agent", "order-agent"]
    assert all(event["event_type"] == "tool_result_consumed" for event in trace.events)
    assert context.evidence_refs == {"ev_12345678901234567890"}


def test_case_context_rejects_scope_override_and_unknown_ref() -> None:
    gateway = FakeGateway()
    trace = FakeTrace()
    context = CaseContext("CASE_001", gateway, trace)

    async def run() -> None:
        try:
            await context.fetch("order-agent", "get_order", case_id="CASE_002")
        except ValueError as exc:
            assert "case_id is fixed" in str(exc)
        else:
            raise AssertionError("cross-case request was accepted")

    asyncio.run(run())
    try:
        context.require_refs(("ev_not_from_gateway",))
    except ValueError as exc:
        assert "not fetched" in str(exc)
    else:
        raise AssertionError("invented ref was accepted")
    assert gateway.calls == []
    assert trace.events == []
