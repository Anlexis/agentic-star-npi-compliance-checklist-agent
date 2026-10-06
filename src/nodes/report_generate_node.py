"""AgentCore Platform v1.0"""

# Inner domain node — render the NPI compliance checklist report.
# Combines the findings and the regulatory references into the structured,
# human-readable report a gate reviewer works from.
#
# Disclosure schema: measured substance concentrations are rendered only as
# regulatory bands ("below 100 ppm", "at or above 100 ppm", "at or above
# 1,000 ppm", "not declared"). A raw supplier figure is confidential product
# data and is never rendered. The output boundary
# (src/nodes/output_format_node.py) enforces the same rule independently.

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.nodes.compliance_checklist_node import (
    SEVERITY_BLOCKING,
    SEVERITY_MONITOR,
    SEVERITY_UNVERIFIED,
)

# Verdict wording per regulation, ordered by how it affects the gate.
_REGULATION_VERDICT = {
    SEVERITY_BLOCKING: "BLOCKED",
    SEVERITY_UNVERIFIED: "NEEDS REVIEW",
    SEVERITY_MONITOR: "NEEDS REVIEW",
}

_SEVERITY_RANK = {SEVERITY_BLOCKING: 0, SEVERITY_UNVERIFIED: 1, SEVERITY_MONITOR: 2}

_MARKET_LABEL = {
    "jp": "Japan",
    "eu": "European Union",
    "jp_eu": "Japan and European Union",
}

_DISCLOSURE_NOTE = (
    "Disclosure schema: measured substance concentrations are reported as regulatory "
    "bands only (below 100 ppm / at or above 100 ppm / at or above 1,000 ppm). "
    "Supplier-declared figures are not reproduced in this report."
)


def _regulation_verdict(findings: list[dict[str, Any]], regulation: str) -> str:
    """Return the gate verdict for one regulation across all findings."""
    severities = [
        str(f.get("severity", ""))
        for f in findings
        if isinstance(f, dict) and str(f.get("regulation", "")) == regulation
    ]
    if not severities:
        return "PASS"
    if SEVERITY_BLOCKING in severities:
        return _REGULATION_VERDICT[SEVERITY_BLOCKING]
    return "NEEDS REVIEW"


class ReportGenerateNode(FunctionNode):
    """Render the NPI compliance checklist report.

    Combines compliance_results and regulatory_references into the report the
    outer graph formats and returns. ANONYMOUS — inner domain node.
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

        emit_progress("Composing the answer...")
        product_name = state.get("product_name") or "Unnamed product"
        target_market = str(state.get("target_market", "") or "jp_eu")
        entry_count = state.get("bom_entry_count", 0)
        compliance_status = str(state.get("compliance_status", "") or "pass")

        try:
            findings = json.loads(state.get("compliance_results", "[]"))
        except (json.JSONDecodeError, TypeError, ValueError):
            findings = []
        if not isinstance(findings, list):
            findings = []
        findings = [f for f in findings if isinstance(f, dict)]

        try:
            references = json.loads(state.get("regulatory_references", "{}"))
        except (json.JSONDecodeError, TypeError, ValueError):
            references = {}
        if not isinstance(references, dict):
            references = {}

        separator = "=" * 62
        lines = [
            separator,
            "  NPI COMPLIANCE CHECKLIST REPORT",
            f"  Product: {product_name}",
            f"  Target market: {_MARKET_LABEL.get(target_market, target_market)}",
            f"  Components evaluated: {entry_count}",
            separator,
            "",
            f"GATE VERDICT: {compliance_status.replace('_', ' ').upper()}",
            "",
            "REGULATORY STATUS SUMMARY",
        ]
        for regulation in references:
            lines.append(f"  {regulation}: {_regulation_verdict(findings, regulation)}")
        lines.append("")

        if findings:
            ordered = sorted(findings, key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "")), 9))
            lines.append("REQUIRED ACTIONS")
            for index, finding in enumerate(ordered, 1):
                substance = finding.get("substance") or ""
                substance_note = f" [{substance}]" if substance else ""
                lines.append(
                    f"  [{index}] {finding.get('item', 'Unknown component')}{substance_note} "
                    f"- {finding.get('regulation', '')} ({finding.get('severity', '')})"
                )
                lines.append(f"       Limit         : {finding.get('threshold', 'not specified')}")
                lines.append(f"       Concentration : {finding.get('concentration_band', 'not declared')}")
                lines.append(f"       Action        : {finding.get('action', '')}")
            lines.append("")
        else:
            lines.append("No compliance gaps were identified for the submitted components.")
            lines.append("")

        if not entry_count:
            lines.append(
                "No bill of materials was submitted, so no component-level evaluation was "
                "performed. Submit the component list to obtain a substance-level verdict."
            )
            lines.append("")

        lines.append("REGULATORY REFERENCES")
        for regulation, reference in references.items():
            if not isinstance(reference, dict):
                continue
            lines.append(f"  {regulation}:")
            lines.append(f"    {reference.get('full_name', regulation)}")
            lines.append(f"    Authority   : {reference.get('authority', 'not specified')}")
            lines.append(f"    Threshold   : {reference.get('threshold', 'not specified')}")
            lines.append(f"    Obligation  : {reference.get('key_obligation', 'not specified')}")
            lines.append(f"    Market note : {reference.get('market_note', 'not specified')}")
            lines.append("")

        lines.append(_DISCLOSURE_NOTE)
        lines.append(separator)
        lines.append("END OF REPORT")
        lines.append(separator)

        report = "\n".join(lines)

        emit_trace_event(
            "report_generated",
            {
                "target_market": target_market,
                "finding_count": len(findings),
                "compliance_status": compliance_status,
                "report_length": len(report),
            },
            state,
        )

        return {
            "compliance_report": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
