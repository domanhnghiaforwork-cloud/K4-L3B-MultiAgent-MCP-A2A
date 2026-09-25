"""Shared, internal interfaces for one L3B investigation.

These types are handoff data between modules, not fields in the submitted JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


class Gateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]: ...


class TraceSink(Protocol):
    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Evidence:
    """A validated MCP response; the ref is copied unchanged from the gateway."""

    evidence_ref: str
    domain: str
    data: Any
    result_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """One specialist's handoff to the coordinator and policy module."""

    facts: dict[str, Any]
    evidence_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class CaseContext:
    """Case-scoped MCP access, trace linkage, and query cache shared by specialists."""

    case_id: str
    gateway: Gateway
    trace: TraceSink
    tool_permissions: dict[str, frozenset[str]] | None = None
    max_calls: int | None = None
    _cache: dict[tuple[str, str], Evidence] = field(
        default_factory=dict, init=False, repr=False
    )
    _evidence_refs: set[str] = field(default_factory=set, init=False, repr=False)
    _call_count: int = field(default=0, init=False, repr=False)

    @property
    def call_count(self) -> int:
        """Number of actual MCP calls, excluding cache hits."""
        return self._call_count

    @property
    def evidence_refs(self) -> frozenset[str]:
        """Refs returned by the gateway during this case only."""
        return frozenset(self._evidence_refs)

    def require_refs(self, refs: tuple[str, ...] | list[str]) -> None:
        unknown = set(refs) - self._evidence_refs
        if unknown:
            raise ValueError(
                f"evidence refs were not fetched for {self.case_id}: {sorted(unknown)}"
            )

    async def fetch(self, actor: str, tool_name: str, **arguments: Any) -> Evidence:
        """Fetch and consume evidence. Do not pass or construct an evidence_ref."""
        if not actor or not tool_name:
            raise ValueError("actor and tool_name are required")
        if "case_id" in arguments:
            raise ValueError("case_id is fixed by CaseContext")
        if self.tool_permissions is not None and tool_name not in self.tool_permissions.get(
            actor, frozenset()
        ):
            raise ValueError(f"{actor} cannot call {tool_name}")
        key = (
            tool_name,
            json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        )
        evidence = self._cache.get(key)
        if evidence is None:
            if self.max_calls is not None and self._call_count >= self.max_calls:
                raise RuntimeError(f"MCP call budget exhausted for {self.case_id}")
            # Count attempted calls too: the server audits unsuccessful requests.
            self._call_count += 1
            response = await self.gateway.call(tool_name, case_id=self.case_id, **arguments)
            evidence = Evidence(
                evidence_ref=response["evidence_ref"],
                domain=response["domain"],
                data=response["data"],
                result_hash=response["result_hash"],
                warnings=tuple(response.get("warnings", ())),
            )
            self._cache[key] = evidence

        self._evidence_refs.add(evidence.evidence_ref)
        self.trace.emit(
            case_id=self.case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence.evidence_ref],
        )
        return evidence
