"""AgentCore Platform v1.0"""

# Inner domain node — attach the regulatory references for the regimes in
# scope. Reads compliance_results to see which regimes produced findings, and
# target_market to see which regimes apply at all. The reference data itself
# lives in src/services/service.py.

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.service import RegulatoryReferenceService


class RegulatoryRefNode(FunctionNode):
    """Attach the regulatory references for the regimes in scope.

    Produces regulatory_references as a JSON-serialised dict.
    ANONYMOUS — inner domain node.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        emit_progress("Looking up references...")
        results_raw = state.get("compliance_results", "[]")
        try:
            findings = json.loads(results_raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            findings = []
        if not isinstance(findings, list):
            findings = []

        flagged = {
            str(finding.get("regulation", "")).strip()
            for finding in findings
            if isinstance(finding, dict) and str(finding.get("regulation", "")).strip()
        }

        target_market = str(state.get("target_market", "") or "")
        references = RegulatoryReferenceService.lookup(target_market, flagged)

        emit_trace_event(
            "regulatory_refs_retrieved",
            {
                "target_market": target_market,
                "regulations_flagged": sorted(flagged),
                "total_references": len(references),
            },
            state,
        )

        return {
            "regulatory_references": json.dumps(references),
            "status": AgentStatus.SUCCESS.value,
        }
