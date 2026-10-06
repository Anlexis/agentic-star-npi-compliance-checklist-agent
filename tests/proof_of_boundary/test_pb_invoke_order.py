# PB-6: Backbone Invocation Order Verification
#
# Verifies that a compiled agent invoked over a SUCCESS-yielding payload
# traverses the fixed 5-node backbone in the correct order:
#   InitializeNode -> InputValidateNode -> <main slot> -> OutputFormatNode -> FinalizeNode
#
# The caller MUST be VERIFIED_EXTERNAL, never an internal context. A GraphNode
# passes the outer invocation context into the inner subgraph unchanged, so an
# INTERNAL-context invoke would mask a mis-set inner trust gate: an INTERNAL
# caller clears every ANONYMOUS gate, and the backbone test would go green while
# a real external caller was refused.
#
# Template-specific constants:
#   _MAIN_SLOT_NODE : class name of the GraphNode subclass in the `main` slot
#   _VALID_PAYLOAD  : specification text that produces SUCCESS end to end
#   _VALID_CONTEXT  : caller data that produces SUCCESS end to end
#     (both mirror deploy/invoke_payload.json — the smoke payload and the
#      boundary test exercise the same request)
from typing import ClassVar
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel

import json

# -- Template-specific constants ---------------------------------------------

_MAIN_SLOT_NODE = "NPIComplianceGraphNode"

_VALID_PAYLOAD = "Wireless charging module - pre-gate compliance review"

_VALID_CONTEXT = {
    "channel": "npi_portal",
    "target_market": "jp_eu",
    "product_name": "Wireless Charging Module WCM-200",
    "bom": [
        {"name": "Housing H1", "material": "recycled PET", "type": "housing"},
        {"name": "SKF-6205-2RS Bearing", "material": "chrome steel", "type": "bearing"},
    ],
}

# -- Backbone node class names (fixed for every nested Cat-2 template) --------

_BACKBONE = [
    "InitializeNode",
    "InputValidateNode",
    _MAIN_SLOT_NODE,
    "OutputFormatNode",
    "FinalizeNode",
]


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _run(payload: str, context: dict | None = None):
    """Compile and invoke the agent exactly as the server does; return the result."""
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel

    from src.graph.graph import NPIComplianceChecklistAgent, _runtime_config

    agent = NPIComplianceChecklistAgent(config=_runtime_config())
    agent.compile()

    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(payload, ctx=ctx, input_context=context or {})


class TestBackboneInvokeOrder:
    """PB-6: invoke() must traverse the backbone in the correct order."""

    def test_invoke_returns_success(self):
        from framework.schemas.agent_status import AgentStatus

        result = _run(_VALID_PAYLOAD, _VALID_CONTEXT)
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected SUCCESS, got {result.get('status')}. error_log: {result.get('error_log')}"

    def test_backbone_node_history_order(self):
        result = _run(_VALID_PAYLOAD, _VALID_CONTEXT)
        history = result.get("node_history", [])
        names = [(cls if isinstance(cls, str) else cls.__name__) for cls in history]
        assert names == _BACKBONE, f"Backbone order mismatch.\nExpected: {_BACKBONE}\nActual:   {names}"

    def test_output_populated(self):
        result = _run(_VALID_PAYLOAD, _VALID_CONTEXT)
        output = result.get("output")
        assert output, f"'output' key is empty or missing. result keys: {list(result.keys())}"
        assert "NPI COMPLIANCE CHECKLIST REPORT" in str(output)

    def test_verified_external_context_admitted(self):
        """A real external caller is admitted end to end — no inner trust trap."""
        from framework.schemas.agent_status import AgentStatus

        result = _run(_VALID_PAYLOAD, _VALID_CONTEXT)
        assert result.get("status") == AgentStatus.SUCCESS.value

    def test_no_error_log_on_success(self):
        result = _run(_VALID_PAYLOAD, _VALID_CONTEXT)
        assert result.get("error_log", []) == []

    def test_smoke_payload_matches_this_boundary_payload(self):
        """deploy/invoke_payload.json must exercise the same request as PB-6."""
        import pathlib

        path = pathlib.Path(__file__).parents[2] / "deploy" / "invoke_payload.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["input"] == _VALID_PAYLOAD
        assert payload["input_context"] == _VALID_CONTEXT

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(_PrivilegedTrustGateFixture.required_trust_level).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls
