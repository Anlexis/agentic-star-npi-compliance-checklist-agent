"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization, and Pydantic objects cause silent
# corruption. Extend AgentState with agent-specific fields only; never add
# credentials, secrets, or Pydantic models.
#
# Serialisation rule: any field that carries a list or dict is typed
# Optional[str] and serialised with json.dumps() by the producer node; the
# consumer node deserialises it with json.loads().

from typing import Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """NPI Compliance Checklist Agent state.

    Extends AgentState with the fields the compliance workflow carries between
    nodes. List/dict payloads are Optional[str] (JSON-serialised).
    """

    # Normalised product specification text (control characters stripped)
    validated_input: Optional[str]

    # Product name — a validated caller label; renders in the report
    product_name: Optional[str]

    # Target market slug in scope for this run: "jp", "eu" or "jp_eu"
    target_market: Optional[str]

    # Caller channel slug, recorded for audit attribution
    channel: Optional[str]

    # Bill of materials (list of validated component records) — JSON string
    # Producer: InputValidateNode; Consumer: ComplianceCheckListNode
    bom_content: Optional[str]

    # Number of accepted bill-of-materials entries
    bom_entry_count: Optional[int]

    # Per-regulation compliance findings — JSON-serialised list
    # Producer: ComplianceCheckListNode; Consumer: ReportGenerateNode, OutputFormatNode
    compliance_results: Optional[str]

    # Overall verdict: "pass", "needs_review" or "blocked"
    compliance_status: Optional[str]

    # Regulatory references for the regimes in scope — JSON-serialised dict
    # Producer: RegulatoryRefNode; Consumer: ReportGenerateNode
    regulatory_references: Optional[str]

    # Human-readable NPI compliance checklist report
    compliance_report: Optional[str]

    # ------------------------------------------------------------------
    # Degraded completion marker
    # ------------------------------------------------------------------

    # Set when the run completes WITHOUT producing a report because the
    # caller's request could not be accepted as written - a rejection the
    # caller can correct and retry. The run still completes: no checklist is
    # run, no report is assembled, and the domain audit event for the
    # rejection is still emitted. Carrying this as a completion marker rather
    # than a terminal error is what lets the caller see the reason and send a
    # corrected request on the same conversation.
    #
    # Content the agent refuses outright, and a breach of a contract the
    # caller cannot influence, are NOT reported here - those stay terminal so
    # they are not mistaken for something a reworded request would get past.
    error_code: Optional[str]
