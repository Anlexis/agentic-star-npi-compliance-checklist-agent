"""AgentCore Platform v1.0"""

# Backbone post_process node — the external-output boundary for the compliance
# report. Three independent layers, applied in this order:
#
#   (1) credential scan — an API key, JWT, bearer token or credential
#       assignment anywhere in the report withholds the response entirely
#       (sanitised stub, status=ERROR);
#   (2) caller-label containment — the only caller free text this report
#       renders is component labels (the product name and each finding's item
#       name). Each one is re-validated at the boundary against the same
#       alphabet the input contract enforces, and a label that fails is
#       withheld. The check is independent of the input gate on purpose: if any
#       future path ever reached the renderer without validation, the boundary
#       still holds;
#   (3) concentration-disclosure grid — the documented external schema reports
#       measured substance concentrations as regulatory bands only, never as a
#       supplier-declared figure. Every concentration token in the report is
#       checked against that schema and an off-schema figure is replaced by its
#       band, with an audit event per replacement.
#
# Layer order matters. The credential scan is a PATTERN scan, so it runs BEFORE
# any token is rewritten — a rewrite that lands inside a credential-shaped
# string would destroy the very pattern the scan looks for — and it is repeated
# after the rewrites so nothing a rewrite produced can escape it.
#
# The output gate is the module-level function `_security_gate_output` called
# from inside execute(), not an instance method: the framework's own gate
# methods are final, and a node-level override would raise at class definition.

import json
import logging
import re
from typing import Any, ClassVar, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, OUTPUT_BLOCKED, TOO_LONG
from src.nodes.compliance_checklist_node import concentration_band

logger = logging.getLogger(__name__)

# Credential patterns that must never appear in the rendered report.
_CREDENTIAL_PATTERNS: List[Tuple[str, str]] = [
    (r"(?:sk|pk|ak)-[A-Za-z0-9_\-]{10,}", "api_key_pattern"),
    (r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", "jwt_pattern"),
    (r"Bearer\s+[A-Za-z0-9_.\-]{8,}", "bearer_token"),
    (
        r"(?:password|passwd|secret|api_key|token|credential|access_key|private_key)" r"\s*[:=]\s*\S{4,}",
        "credential_assignment",
    ),
]

# The component-label alphabet the input contract enforces. Kept here as the
# boundary's own copy so this layer stays independent of the input node.
_LABEL_ALPHABET = "[A-Za-z0-9 .,_()/+%#-]"
_LABEL_RE = re.compile(f"{_LABEL_ALPHABET}+")
_MAX_LABEL_CHARS = 80
_WITHHELD_LABEL = "[label withheld]"

# Approved external disclosure schema. A concentration may be rendered only as
# one of the regulatory band phrases, or as one of the regime thresholds those
# bands are drawn from; any other figure is a supplier-declared measurement
# reaching the external surface.
_APPROVED_PPM = frozenset({100.0, 1000.0})
_APPROVED_PERCENT_WW = frozenset({0.1, 0.01})

# Delimiter between a figure and its unit: horizontal whitespace only. A `\s*`
# delimiter would span a paragraph break, letting a number that ends one line
# bind to a unit word opening the next block and rewriting document structure.
_UNIT_DELIM = r"[ \t]*"

# Identifier guards keep the grammar off part designations: a concentration
# token never starts or ends inside an alphanumeric identifier, so a match may
# not be immediately preceded or followed by identifier characters. Part
# numbers (SKF-6205-2RS, EAB64785603, MFG-2026-001) therefore stay
# byte-identical while every rendered concentration figure is still checked.
_PPM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.,-])"
    r"(?P<value>[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    rf"(?P<delim>{_UNIT_DELIM})"
    r"(?P<unit>ppm)"
    r"(?![A-Za-z0-9-])"
)
_PERCENT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9.,-])"
    r"(?P<value>[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
    rf"(?P<delim>{_UNIT_DELIM})"
    rf"(?P<unit>%{_UNIT_DELIM}w/w)"
    r"(?![A-Za-z0-9-])"
)


def _security_gate_output(content: str) -> Optional[str]:
    """Scan the report for credential and secret patterns.

    Returns the first violation label, or None when the report is clean.
    Module-level function, not a node instance method — the framework's gate
    methods are final and a node-level override raises at class definition.
    """
    if not content:
        return None
    for pattern, label in _CREDENTIAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return label
    return None


def _caller_labels(state: dict[str, Any]) -> List[str]:
    """Collect every caller-derived label this report renders.

    That is the product name and the item name of each finding. Regulation
    names, thresholds, actions and reference text are template constants and
    are not caller-derived.
    """
    labels: List[str] = []
    product_name = state.get("product_name")
    if isinstance(product_name, str) and product_name:
        labels.append(product_name)
    try:
        findings = json.loads(state.get("compliance_results", "[]"))
    except (json.JSONDecodeError, TypeError, ValueError):
        findings = []
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                item = finding.get("item")
                if isinstance(item, str) and item:
                    labels.append(item)
    return labels


def _contain_caller_labels(report: str, state: dict[str, Any]) -> Tuple[str, List[str]]:
    """Withhold any rendered caller label that does not satisfy the contract.

    Returns (report, withheld_labels). A label that is not plain text of the
    component-label alphabet within the length bound is replaced wherever it
    appears, so caller-controlled markup or line structure can never reach the
    external surface even if it bypassed the input contract.
    """
    withheld: List[str] = []
    contained = report
    for label in _caller_labels(state):
        if len(label) <= _MAX_LABEL_CHARS and _LABEL_RE.fullmatch(label):
            continue
        if label in contained:
            contained = contained.replace(label, _WITHHELD_LABEL)
            withheld.append(label[:16])
    return contained, withheld


def _band_for_ppm(value: float) -> str:
    """Return the approved band phrase for a concentration in ppm."""
    return concentration_band(value)


def _enforce_disclosure_schema(report: str) -> Tuple[str, int]:
    """Replace every off-schema concentration figure with its regulatory band.

    Returns (report, replacement_count). A replacement means a supplier-declared
    measurement reached the external surface; the boundary reduces it to the
    band the documented schema allows. Figures that are already one of the
    approved threshold values are left byte-identical.
    """
    replacements = 0

    def _reduce(match: re.Match[str], to_ppm: float, approved: frozenset[float]) -> str:
        nonlocal replacements
        try:
            value = float(match.group("value").replace(",", ""))
        except ValueError:  # pragma: no cover - the grammar only matches numerals
            return match.group(0)
        if value in approved:
            return match.group(0)
        replacements += 1
        return _band_for_ppm(abs(value) * to_ppm)

    report = _PPM_TOKEN_RE.sub(lambda m: _reduce(m, 1.0, _APPROVED_PPM), report)
    report = _PERCENT_TOKEN_RE.sub(lambda m: _reduce(m, 10_000.0, _APPROVED_PERCENT_WW), report)
    return report, replacements


# Caller-facing wording for a run that completed without a report. The marker
# is an internal reason code; this maps it to the sentence the caller sees.
# Static sentences only - no request value is ever substituted.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class OutputFormatNode(FunctionNode):
    """Apply the output gate and expose the caller-facing compliance report.

    Outer backbone post_process slot. Declared ANONYMOUS — trust was already
    enforced at InputValidateNode (VERIFIED_EXTERNAL).

    Input state keys:
        compliance_report:  str — rendered report from the inner workflow
        product_name:       str — validated caller label
        compliance_results: str — JSON findings, source of the item labels

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str] (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _withhold(self, state: dict[str, Any], violation: str) -> dict[str, Any]:
        """Withhold the report entirely — the fail-closed credential outcome."""
        logger.error("OutputFormatNode: credential pattern detected in output — %s", violation)
        emit_trace_event("output_credential_violation", {"violation": violation}, state)
        sanitised = (
            f"[REPORT WITHHELD: the output contained a disallowed pattern ({violation}). "
            f"Contact the compliance-data operator.]"
        )
        emit_progress(OUTPUT_BLOCKED)
        return {
            "formatted_output": sanitised,
            "result": sanitised,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"OutputFormatNode: credential pattern detected — {violation}"],
        }

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        emit_progress("Formatting the response...")

        # The run completed without a report because the request could not be
        # accepted as written. Report the reason as the response: the caller
        # needs to know what to change, and an empty body would leave them with
        # nothing. Status stays SUCCESS - the run did what it could with the
        # request it was given, and the caller can correct it and send again on
        # the same conversation.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("output_format_degraded", {"reason": marker}, state)
            return {
                "formatted_output": message,
                "result": message,
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        report = state.get("compliance_report") or state.get("result") or ""
        if not str(report).strip():
            report = "No compliance report could be generated for this request."
        report = str(report)

        # Layer 1 — credential scan, before any token is rewritten.
        violation = _security_gate_output(report)
        if violation:
            return self._withhold(state, violation)

        # Layer 2 — caller-label containment.
        report, withheld = _contain_caller_labels(report, state)
        if withheld:
            logger.error("OutputFormatNode: caller label withheld at the output boundary")
            emit_trace_event("output_label_withheld", {"withheld_count": len(withheld)}, state)

        # Layer 3 — concentration-disclosure schema.
        report, replacements = _enforce_disclosure_schema(report)
        if replacements:
            logger.warning(
                "OutputFormatNode: %d off-schema concentration figure(s) reduced to a band",
                replacements,
            )
            emit_trace_event("output_disclosure_reduction", {"replacement_count": replacements}, state)

        # Layer 1 again — nothing a rewrite produced may escape the pattern scan.
        violation = _security_gate_output(report)
        if violation:
            return self._withhold(state, violation)

        emit_trace_event("output_finalized", {"output_length": len(report)}, state)

        return {
            "formatted_output": report,
            "result": report,
            "status": AgentStatus.SUCCESS.value,
        }
