"""AgentCore Platform v1.0"""

# Outer graph — AgentBaseGraph with a GraphNode subclass in the `main` slot.
#
# Outer backbone (fixed 5-node skeleton):
#   START -> initialize -> pre_process(InputValidateNode, VERIFIED_EXTERNAL)
#        -> main(NPIComplianceGraphNode, GraphNode)
#        -> post_process(OutputFormatNode, ANONYMOUS + output gate)
#        -> finalize -> END
#
# Inner workflow (NPIComplianceWorkflowGraph, BaseGraph):
#   START -> compliance_checklist -> regulatory_ref -> report_generate -> END
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (compliance pipeline)
#   src/graph/context_bridge.py        <- input_context hand-off (outer -> inner)
#
# The class name here matches the `class:` entry point in config/agent.yaml and
# the import in src/api/server.py.

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import set_caller_input_context
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.schemas.state import State

if TYPE_CHECKING:
    from src.graph.domain_workflow_graph import NPIComplianceWorkflowGraph

# Runtime-parameter file: src/graph/graph.py -> parents[2] is the repo root.
# config/agent.yaml (the static manifest) holds only registration identity;
# every runtime parameter lives in config/config.yaml.
_RUNTIME_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


def _runtime_config() -> dict[str, Any]:
    """Read the runtime parameters from config/config.yaml.

    This is the file the platform registry loads and passes as
    Graph(config=...); the standalone server (src/api/server.py) reads it here
    so a deployed agent and a registry-loaded agent see identical
    configuration. Returns an empty dict — never raises — when the file is
    absent, unreadable, not valid YAML, or not a mapping (the graph then runs
    on its built-in defaults).
    """
    try:
        import yaml

        loaded = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _config_number(value: Any, lo: float, hi: float) -> Optional[float]:
    """Validate a declared numeric setting: a real number, finite, within [lo, hi].

    Bools, strings, non-numerics, NaN/Infinity and out-of-range values all
    return None, and the consumer then keeps its built-in default. Rejecting
    non-finite values matters: NaN comparisons are always False, so a NaN scan
    budget or entry cap would silently disable the guard it configures.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


def declared_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and flatten the declared runtime settings for the domain nodes.

    `config` is what the graph was constructed with — the contents of
    config/config.yaml. Constructor injection is the config route because the
    node contract is ``execute(self, state) -> dict``, which takes no config
    argument, so the settings are flattened once here and handed to each node.

    Every value is checked for type, finiteness and range. An invalid or absent
    key is simply not forwarded and the consuming node keeps its module
    default, so a malformed configuration file can neither crash graph
    construction nor weaken a guard.
    """
    compliance_raw = config.get("compliance")
    compliance: dict[str, Any] = compliance_raw if isinstance(compliance_raw, dict) else {}

    declared: dict[str, Any] = {}

    # Wall-clock budget for one compliance scan (config/config.yaml timeout_s).
    scan_budget = _config_number(config.get("timeout_s"), 0.1, 600.0)
    if scan_budget is not None:
        declared["scan_budget_s"] = scan_budget

    max_entries = _config_number(compliance.get("max_bom_entries"), 1, 10_000)
    if max_entries is not None and max_entries == int(max_entries):
        declared["max_bom_entries"] = int(max_entries)

    default_market = compliance.get("default_target_market")
    if isinstance(default_market, str) and default_market:
        declared["default_target_market"] = default_market

    return declared


class NPIComplianceGraphNode(GraphNode):
    """Cat-2 main slot: wraps the NPI compliance inner workflow graph.

    Delegates domain processing to NPIComplianceWorkflowGraph (BaseGraph).

    Contracts:
      get_subgraph()  — build the inner graph, carrying the declared settings
      extract_input() — serialise the validated product record for the inner
                        graph, and bridge input_context across the boundary
      merge_output()  — map the inner get_output() dict into the outer state delta
      error_strategy  — "propagate": re-raise inner failures as SubgraphError
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, settings: Optional[dict[str, Any]] = None) -> None:
        """Bind the declared runtime settings forwarded by the outer graph."""
        super().__init__()
        self._settings: dict[str, Any] = dict(settings or {})

    def get_subgraph(self) -> "NPIComplianceWorkflowGraph":
        """Instantiate the inner domain workflow graph with the declared settings.

        Imported inside the method to avoid a circular import at module load
        time and to match the nested Cat-2 pattern.
        """
        from src.graph.domain_workflow_graph import NPIComplianceWorkflowGraph

        return NPIComplianceWorkflowGraph(config=self._settings)

    def execute(self, state: AgentState) -> dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request declined upstream has no validated input to work on, so
        running the inner graph would only produce a second, vaguer reason for
        the same rejection - and overwrite the specific one already settled.

        This override is deliberate: GraphNode.execute() is not final, and the
        marker is the only signal that distinguishes "nothing to do" from "not
        run yet".
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        """Hand the inner graph the specification text, and bridge the record.

        GraphNode.extract_input() returns a STRING the framework writes into
        the inner state under `user_input`. That field is one the framework's
        input gate rewrites, and its personal-name heuristic matches any two
        consecutive capitalised words — precisely the shape of a component
        label ("Lead Capacitor C12", "Wireless Charging Module"). Carrying the
        structured product record on `user_input` therefore replaced component
        names with a mask token and the report named the wrong parts. Only the
        free-text specification travels on `user_input`; the validated product
        record crosses on the caller-data channel, which carries structured
        values through untouched.

        This is also the last hook that sees the outer state before the inner
        invoke, and the framework does not forward the caller-data channel into
        a subgraph (see src/graph/context_bridge.py), so the record is stashed
        here for the inner side to pick up.
        """
        set_caller_input_context(
            {
                "channel": state.get("channel", ""),
                "target_market": state.get("target_market", ""),
                "product_name": state.get("product_name", ""),
                "bom_content": state.get("bom_content", "[]"),
            }
        )
        return cast(str, state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        """Map the inner graph's sub_result back into the outer state delta.

        Designed together with NPIComplianceWorkflowGraph.get_output().
        Returns only changed state keys — never the full state.

        compliance_results travels out with the report because the output gate
        (OutputFormatNode) re-validates every caller-derived label that reaches
        the rendered report against the label contract.
        """
        return {
            "compliance_report": sub_result.get("compliance_report"),
            "compliance_results": sub_result.get("compliance_results"),
            "compliance_status": sub_result.get("compliance_status"),
            "result": sub_result.get("output") or sub_result.get("compliance_report"),
            "status": sub_result.get("status"),
            # Outer reason wins. A reason settled before the inner run is the
            # real one; the inner graph only sees its downstream consequence,
            # and a plain sub_result.get() would erase the outer reason whenever
            # the inner run did not set its own.
            "error_code": state.get("error_code") or sub_result.get("error_code", ""),
        }


class NPIComplianceChecklistAgent(AgentBaseGraph):
    """NPI Compliance Checklist Agent — outer graph.

    Validates a new product's bill of materials against the REACH, RoHS and
    PSE requirement sets in scope for the target market, ranks the resulting
    gaps by whether they block the new-product-introduction gate, and generates
    a compliance checklist plus a gap report.

    Backbone: initialize -> InputValidateNode(VERIFIED_EXTERNAL)
              -> NPIComplianceGraphNode(GraphNode)
              -> OutputFormatNode(ANONYMOUS, output gate)
              -> finalize

    Runtime configuration: the platform registry loads config/config.yaml and
    passes it as Graph(config=...); the standalone server does the same via
    _runtime_config(). AgentBaseGraph consumes max_retry from that config
    (retry routing) and register_nodes() forwards the domain settings to the
    nodes, so every declared value is live in both deployments.

    add_edges() is NOT overridden — backbone wiring belongs to AgentBaseGraph.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "NPIComplianceChecklistAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill the 3 domain slots after super() injects initialize + finalize.

        The declared settings are validated once and handed to the nodes that
        consume them, so config/config.yaml is the single source for both the
        validation bounds and the inner pipeline.
        """
        super().register_nodes()
        settings = declared_settings(self.config)
        self._nodes["pre_process"] = InputValidateNode(config=settings)
        self._nodes["main"] = NPIComplianceGraphNode(settings=settings)
        self._nodes["post_process"] = OutputFormatNode()

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Surface the compliance verdict alongside the base invoke() envelope.

        AgentBaseGraph.get_output() returns only
        ``{output, status, trace_id, correlation_id, node_history}``. A caller
        wiring this agent into a new-product-introduction gate needs the verdict
        as DATA — to release or hold the gate — rather than by parsing the
        rendered report, so the summary verdict is surfaced too.

        Output-gate invariant preserved (fail-closed): ``output`` stays the
        post-gate report produced by OutputFormatNode — the pre-gate
        ``compliance_report`` is never surfaced — and the structured verdict is
        withheld (None) on any non-success outcome, including a gate block.
        """
        # cast: the framework wheel ships no py.typed marker, so the base
        # method's return type resolves to Any for the checker.
        output: dict[str, Any] = dict(super().get_output(state))
        # A run that completed WITHOUT carrying out the request holds the
        # sentence saying what to correct, not a domain product: none of the
        # structured fields below were produced, so none of them is released.
        # The base envelope already holds that sentence. SUCCESS here reports
        # that the run reached a defined end safely, not that the request was
        # carried out.
        if state.get("error_code"):
            return output

        succeeded = state.get("status") == AgentStatus.SUCCESS.value
        output["compliance_status"] = state.get("compliance_status") if succeeded else None
        return output
