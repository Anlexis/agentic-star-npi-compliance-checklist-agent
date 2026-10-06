"""AgentCore Platform v1.0"""

# Inner domain node — evaluate the validated bill of materials against the
# REACH, RoHS and PSE requirement sets in scope for the target market.
#
# The validated product record arrives on the caller-data channel, restored on
# the inner side by the context bridge (src/graph/context_bridge.py). It does
# NOT travel on user_input: the framework's input gate rewrites that field, and
# its personal-name heuristic matches any two consecutive capitalised words —
# the shape of an ordinary component label — so a record carried there would
# reach this node with its component names replaced by a mask token.
#
# Every accepted bill-of-materials entry is evaluated. A finding carries the
# regulation, the matched restricted substance, the disclosure band for the
# declared concentration, and the severity that decides whether the
# new-product-introduction gate is blocked.

import json
import math
import time
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import PROCESSING_FAILED

# Substances on the REACH candidate list of substances of very high concern.
_REACH_SVHC = (
    "lead",
    "mercury",
    "cadmium",
    "chromium vi",
    "chromium-vi",
    "dehp",
    "dbp",
    "bbp",
    "dibp",
    "cobalt",
    "arsenic",
    "barium",
)

# Substances restricted by the RoHS directive (and mirrored by the Japanese
# marking requirement for the domestic market).
_ROHS_SUBSTANCES = (
    "lead",
    "mercury",
    "cadmium",
    "chromium",
    "pbbs",
    "pbdes",
    "hexavalent chromium",
)

# Product types in the Japanese electrical-appliance category that requires
# third-party certification before market entry.
_PSE_CATEGORY_A_TERMS = (
    "power supply",
    "electric cable",
    "li-ion battery",
    "lithium battery",
    "circuit breaker",
    "fuse",
    "switch",
    "transformer",
    "capacitor",
)

# Declaration and restriction thresholds, in parts per million of the article's
# mass. REACH duties start at 0.1% w/w; the RoHS restriction is 0.1% w/w for
# every listed substance except cadmium, which is restricted at 0.01% w/w.
_REACH_DECLARATION_PPM = 1000.0
_ROHS_DEFAULT_LIMIT_PPM = 1000.0
_ROHS_CADMIUM_LIMIT_PPM = 100.0

# Regulation scope per target market. The Japanese market is governed by the
# domestic marking and appliance-safety regimes; the European market by REACH
# and RoHS; a product shipping to both is evaluated against all three.
_MARKET_SCOPE = {
    "jp": ("RoHS", "PSE"),
    "eu": ("REACH", "RoHS"),
    "jp_eu": ("REACH", "RoHS", "PSE"),
}
_DEFAULT_TARGET_MARKET = "jp_eu"

# Default wall-clock budget for one scan, in seconds, when config declares none.
_DEFAULT_SCAN_BUDGET_S = 30.0

# Defensive entry cap when config declares none. The pre_process gate already
# capped the list; this bound means the inner pipeline still holds if it is ever
# driven directly.
_DEFAULT_MAX_BOM_ENTRIES = 200

# Severity vocabulary. "blocking" holds the new-product-introduction gate,
# "monitor" records a duty that does not hold it, and "unverified" marks a
# restricted substance whose concentration the caller did not declare — which
# is never treated as compliant.
SEVERITY_BLOCKING = "blocking"
SEVERITY_MONITOR = "monitor"
SEVERITY_UNVERIFIED = "unverified"


def concentration_band(concentration_ppm: Optional[float]) -> str:
    """Return the disclosure band for a declared concentration.

    The external report discloses measured concentrations only as one of these
    bands — a raw supplier figure is confidential product data and never
    reaches the rendered report. See src/nodes/output_format_node.py, which
    enforces the same invariant independently at the output boundary.
    """
    if concentration_ppm is None or not math.isfinite(concentration_ppm):
        return "not declared"
    if concentration_ppm >= _ROHS_DEFAULT_LIMIT_PPM:
        return "at or above 1,000 ppm"
    if concentration_ppm >= _ROHS_CADMIUM_LIMIT_PPM:
        return "at or above 100 ppm"
    return "below 100 ppm"


def _severity(concentration_ppm: Optional[float], limit_ppm: float) -> str:
    """Grade one finding against its regulatory limit — fail closed.

    An undeclared concentration is "unverified", never "below the limit": the
    caller has not shown the article is compliant. A non-finite value cannot
    reach here (the pre_process gate refuses it) but is graded the same way,
    because every comparison against NaN is False and the compliant branch
    would otherwise be the one it silently falls into.
    """
    if concentration_ppm is None or not math.isfinite(concentration_ppm):
        return SEVERITY_UNVERIFIED
    return SEVERITY_BLOCKING if concentration_ppm >= limit_ppm else SEVERITY_MONITOR


def _rohs_limit(material: str) -> float:
    """Return the RoHS limit that applies to the matched material."""
    return _ROHS_CADMIUM_LIMIT_PPM if "cadmium" in material else _ROHS_DEFAULT_LIMIT_PPM


def _check_reach(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a REACH finding for one component, or None when it is clear."""
    material = str(entry.get("material", "")).lower()
    for svhc in _REACH_SVHC:
        if svhc in material:
            concentration = entry.get("concentration_ppm")
            return {
                "item": entry.get("name", "unknown"),
                "regulation": "REACH",
                "substance": svhc.title(),
                "threshold": "0.1% w/w (1,000 ppm) declaration trigger",
                "concentration_band": concentration_band(concentration),
                "severity": _severity(concentration, _REACH_DECLARATION_PPM),
                "action": (
                    "Declare the substance under the candidate-list communication duty "
                    "and confirm whether an authorisation is required"
                ),
            }
    return None


def _check_rohs(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return a RoHS finding for one component, or None when it is clear."""
    material = str(entry.get("material", "")).lower()
    for substance in _ROHS_SUBSTANCES:
        if substance in material:
            concentration = entry.get("concentration_ppm")
            limit = _rohs_limit(material)
            return {
                "item": entry.get("name", "unknown"),
                "regulation": "RoHS",
                "substance": substance.title(),
                "threshold": f"{limit:,.0f} ppm restriction limit",
                "concentration_band": concentration_band(concentration),
                "severity": _severity(concentration, limit),
                "action": (
                    "Confirm the homogeneous-material concentration against the restriction "
                    "limit and obtain the supplier declaration"
                ),
            }
    return None


def _check_pse(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return an appliance-certification finding for one component, or None.

    Certification is a mandatory conformity assessment with no concentration
    threshold, so a match always blocks the gate until the mark is obtained.
    """
    combined = f"{str(entry.get('type', '')).lower()} {str(entry.get('name', '')).lower()}"
    for term in _PSE_CATEGORY_A_TERMS:
        if term in combined:
            return {
                "item": entry.get("name", "unknown"),
                "regulation": "PSE (Category A)",
                "substance": "",
                "threshold": "Mandatory certification (no quantity threshold)",
                "concentration_band": "not applicable",
                "severity": SEVERITY_BLOCKING,
                "action": (
                    "Obtain the Category A certification mark through a registered "
                    "certification body before domestic market entry"
                ),
            }
    return None


_CHECKS = {"REACH": _check_reach, "RoHS": _check_rohs, "PSE": _check_pse}


def _overall_status(findings: list[dict[str, Any]]) -> str:
    """Reduce the findings to the verdict a gate reviewer acts on."""
    if any(f.get("severity") in (SEVERITY_BLOCKING, SEVERITY_UNVERIFIED) for f in findings):
        return "blocked" if any(f.get("severity") == SEVERITY_BLOCKING for f in findings) else "needs_review"
    return "needs_review" if findings else "pass"


class ComplianceCheckListNode(FunctionNode):
    """Evaluate the bill of materials against the regulations in scope.

    Reads the JSON record NPIComplianceGraphNode.extract_input() placed in
    user_input, and the caller's target market from the bridged input_context.
    Produces compliance_results as a JSON-serialised list and the summary
    verdict as compliance_status.

    ANONYMOUS — this is an inner domain node; the external trust gate is
    InputValidateNode in the outer pre_process slot.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """Bind the declared scan budget and entry cap forwarded by the graph."""
        super().__init__()
        settings = dict(config or {})
        budget = settings.get("scan_budget_s")
        self._scan_budget_s: float = (
            float(budget)
            if isinstance(budget, (int, float))
            and not isinstance(budget, bool)
            and math.isfinite(float(budget))
            and float(budget) > 0
            else _DEFAULT_SCAN_BUDGET_S
        )
        max_entries = settings.get("max_bom_entries")
        self._max_bom_entries: int = (
            max_entries
            if isinstance(max_entries, int) and not isinstance(max_entries, bool) and max_entries > 0
            else _DEFAULT_MAX_BOM_ENTRIES
        )
        default_market = settings.get("default_target_market")
        self._default_target_market: str = default_market if default_market in _MARKET_SCOPE else _DEFAULT_TARGET_MARKET

    @staticmethod
    def _record(state: dict[str, Any]) -> dict[str, Any]:
        """Return the validated product record for this run.

        The caller-data channel is the contract. A JSON object on user_input is
        accepted as a fallback so the node can be driven directly, but that
        path is not how the graph feeds it.
        """
        bridged = state.get("input_context")
        if isinstance(bridged, dict) and bridged:
            return bridged
        try:
            parsed = json.loads(state.get("user_input", ""))
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _target_market(self, record: dict[str, Any]) -> str:
        """Resolve the market scope for this run.

        The outer pre_process gate already refused anything outside the closed
        slug set, so a value here is either one of those slugs or absent. An
        unrecognised value falls back to the declared default rather than
        silently narrowing the scope of the evaluation.
        """
        candidate = record.get("target_market")
        if isinstance(candidate, str) and candidate in _MARKET_SCOPE:
            return candidate
        return self._default_target_market

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # The request was already found unacceptable upstream: this run
        # completes without a result, so there is nothing for this step to
        # do. Returning the marker keeps it on the node's own result dict,
        # which is what the output gate inspects.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        emit_progress("Checking compliance...")
        record = self._record(state)

        product_name = str(record.get("product_name", ""))
        target_market = self._target_market(record)
        regulations = _MARKET_SCOPE[target_market]

        bom_raw = record.get("bom_content", "[]")
        bom_entries: list[Any] = []
        if isinstance(bom_raw, list):
            bom_entries = bom_raw
        elif isinstance(bom_raw, str) and bom_raw:
            try:
                loaded = json.loads(bom_raw)
                bom_entries = loaded if isinstance(loaded, list) else []
            except (json.JSONDecodeError, ValueError):
                bom_entries = []
        bom_entries = [e for e in bom_entries if isinstance(e, dict)][: self._max_bom_entries]

        # Every entry is evaluated against every regulation in scope. The scan
        # fails CLOSED on the wall-clock budget: a partially evaluated bill of
        # materials must never be reported as a clean pass.
        findings: list[dict[str, Any]] = []
        started = time.monotonic()
        for entry in bom_entries:
            if time.monotonic() - started > self._scan_budget_s:
                emit_trace_event(
                    "compliance_scan_incomplete",
                    {"evaluated": len(findings), "bom_entry_count": len(bom_entries)},
                    state,
                )
                emit_progress(PROCESSING_FAILED)
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [
                        "ComplianceCheckListNode: compliance scan exceeded the configured "
                        "budget and did not evaluate every component"
                    ],
                }
            for regulation in regulations:
                finding = _CHECKS[regulation](entry)
                if finding is not None:
                    findings.append(finding)

        compliance_status = _overall_status(findings)

        emit_trace_event(
            "compliance_check_complete",
            {
                "target_market": target_market,
                "regulations_in_scope": list(regulations),
                "bom_entry_count": len(bom_entries),
                "finding_count": len(findings),
                "compliance_status": compliance_status,
            },
            state,
        )

        return {
            "product_name": product_name,
            "target_market": target_market,
            "bom_entry_count": len(bom_entries),
            "compliance_results": json.dumps(findings),
            "compliance_status": compliance_status,
            "status": AgentStatus.SUCCESS.value,
        }
