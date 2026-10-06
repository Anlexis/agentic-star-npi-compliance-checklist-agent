"""AgentCore Platform v1.0"""

# Inner graph for the NPI Compliance Checklist Agent.
# Instantiated by NPIComplianceGraphNode.get_subgraph() in graph.py.
# Inherits BaseGraph (fully custom node topology — no backbone slots).
#
# Pipeline (linear):
#   START -> compliance_checklist -> regulatory_ref -> report_generate -> END
#
# compliance_checklist : Evaluate the validated bill of materials against the
#                        REACH / RoHS / PSE requirement sets in scope for the
#                        caller's target market.
# regulatory_ref       : Look up the regulatory references for those regimes.
# report_generate      : Combine findings + references into the checklist report.

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_input_context
from src.nodes.compliance_checklist_node import ComplianceCheckListNode
from src.nodes.regulatory_ref_node import RegulatoryRefNode
from src.nodes.report_generate_node import ReportGenerateNode
from src.schemas.state import State


class NPIComplianceWorkflowGraph(BaseGraph):
    """Inner graph: the multi-step NPI compliance workflow.

    Called by NPIComplianceGraphNode.get_subgraph() in graph.py.
    Uses BaseGraph (custom topology, not the 5-node backbone).

    All 7 BaseGraph abstract methods are implemented:
        name, state_schema, _validate_config, register_nodes,
        add_edges, route, get_output.
    """

    # -- Identity -------------------------------------------------------------

    @property
    def name(self) -> str:
        return "npi_compliance_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # -- Config validation ----------------------------------------------------

    def _validate_config(self) -> None:
        """No mandatory config keys.

        Every setting this graph forwards was already validated for type,
        finiteness and range by NPIComplianceGraphNode._parent_config(); absent
        keys leave each node on its module default.
        """
        return None

    # -- Caller-context bridge ------------------------------------------------

    def _extra_initial_state(self) -> dict[str, Any]:
        """Seed the inner state with the caller's input_context.

        The framework's GraphNode invokes a subgraph without forwarding
        input_context, so without this hook every inner read of
        state["input_context"] would see {}. NPIComplianceGraphNode.extract_input()
        stashes the outer value immediately before the invoke; this restores it
        on the inner side. See src/graph/context_bridge.py.
        """
        return {"input_context": get_caller_input_context()}

    # -- Node registration ----------------------------------------------------

    def register_nodes(self) -> None:
        """Register the domain nodes.

        No super() call — BaseGraph.register_nodes is abstract. The declared
        runtime settings arrive through the constructors because the node
        contract is ``execute(self, state) -> dict``, which takes no config
        argument.
        """
        self._nodes["compliance_checklist"] = ComplianceCheckListNode(config=self.config)
        self._nodes["regulatory_ref"] = RegulatoryRefNode()
        self._nodes["report_generate"] = ReportGenerateNode()

    # -- Edge wiring ----------------------------------------------------------

    def add_edges(self) -> None:
        """Linear pipeline: compliance_checklist -> regulatory_ref -> report_generate."""
        self._sg.add_edge(START, "compliance_checklist")
        self._sg.add_edge("compliance_checklist", "regulatory_ref")
        self._sg.add_edge("regulatory_ref", "report_generate")
        self._sg.add_edge("report_generate", END)

    # -- Routing --------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph contract; not called in this linear topology."""
        return END if state.get("status") == AgentStatus.ERROR.value else "report_generate"

    # -- Output shape ---------------------------------------------------------

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the sub_result returned to NPIComplianceGraphNode.merge_output().

        Designed together with merge_output() in graph.py:
            sub_result["compliance_report"]  -> outer state["compliance_report"]
            sub_result["compliance_results"] -> outer state["compliance_results"]
            sub_result["compliance_status"]  -> outer state["compliance_status"]
            sub_result["output"]             -> outer state["result"]
            sub_result["status"]             -> outer state["status"]
        """
        return {
            "compliance_report": state.get("compliance_report"),
            "compliance_results": state.get("compliance_results"),
            "compliance_status": state.get("compliance_status"),
            "output": state.get("compliance_report") or state.get("result"),
            "status": state.get("status"),
            # Carried explicitly: the boundary only moves the keys named here,
            # so a run that completed without a report would otherwise arrive
            # at the outer graph indistinguishable from one that produced one.
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
