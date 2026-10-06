"""AgentCore Platform v1.0"""

# Backbone pre_process node — the external-facing trust and validation gate.
#
# Responsibilities:
#   - Enforce VERIFIED_EXTERNAL trust (required_trust_level — the trust gate)
#   - Reject empty / over-long product specifications early (fail fast)
#   - Refuse instruction-override payloads in the specification text
#   - Validate every caller-supplied field against explicit bounds and fail
#     CLOSED on any violation, naming the FIELD and never echoing the VALUE
#   - Normalise the accepted bill of materials for the compliance pipeline
#
# Caller data arrives on two channels and both are validated by the same rules:
#
#   input_context  the structured channel (recommended). Fields:
#                    channel            inert slug, [a-z0-9_]{1,32}
#                    target_market      one of jp | eu | jp_eu
#                    product_name       component label alphabet, <= 80 chars
#                    bom                list of component records, entry-capped
#   input          the product specification. Either a JSON object carrying the
#                  same product_name / bom shape, or a plain-text description.
#
# When both channels carry a bill of materials the structured channel wins.
# When neither does, the run degrades to the baseline: a specification-only
# report that states no bill of materials was submitted. There is no path on
# which unvalidated caller data reaches the compliance pipeline.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import json
import logging
import math
import re
from typing import Any, ClassVar, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

logger = logging.getLogger(__name__)

# Hard cap on the product specification text.
_MAX_SPEC_CHARS = 4000

# Bill-of-materials entry cap when config/config.yaml declares none.
_DEFAULT_MAX_BOM_ENTRIES = 200

# Market scope applied when the caller selects none and config declares none.
_DEFAULT_TARGET_MARKET = "jp_eu"

# The market slugs this template evaluates. Each is an inert identifier and is
# rendered in the report, so the set is closed: anything else is refused.
VALID_TARGET_MARKETS = ("jp", "eu", "jp_eu")

# Caller strings that select behaviour are inert identifiers — lowercase
# alphanumerics and underscore, bounded length. Free text in such a field is
# caller-controlled output and log injection.
_SLUG_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Component labels (product name, item name, material, type) are the only
# caller free text that reaches the rendered report, so they are locked to a
# component-label alphabet: letters, digits, space and the punctuation real
# part designations use (SKF-6205-2RS, PCB Substrate (FR4), M3x8 Screw,
# Cable Assy 1.5m, 10uF/25V, 100-240V). Everything else — quotes, angle
# brackets, colons, at-signs, backslashes, braces, control characters and every
# newline — is refused, so a label can neither carry markup nor break the line
# structure of the report it is rendered into.
_LABEL_ALPHABET = "[A-Za-z0-9 .,_()/+%#-]"
_LABEL_CHAR_RE = re.compile(_LABEL_ALPHABET)
_LABEL_RE = re.compile(f"{_LABEL_ALPHABET}+")

_MAX_PRODUCT_NAME_CHARS = 80
_MAX_ITEM_NAME_CHARS = 80
_MAX_MATERIAL_CHARS = 120
_MAX_TYPE_CHARS = 60

# Substance concentration bounds, in parts per million of the article's mass.
# 1,000,000 ppm is 100% — nothing above it is a physical concentration.
_CONCENTRATION_MIN_PPM = 0.0
_CONCENTRATION_MAX_PPM = 1_000_000.0

# Control characters (except tab and newline) are stripped from the free-text
# specification before it is measured and normalised.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
# Space runs only — a label's tabs and newlines are refused, never collapsed.
_SPACES_RE = re.compile(r" +")

# Instruction-override phrasings: text addressed to the MODEL rather than a
# product specification addressed to this agent. The platform input gate
# refuses high-confidence payloads too, but this template must not depend on
# that — where the gate is absent or configured off, an unchecked payload would
# reach the compliance path and return a report. Refusal is stated in terms of
# BEHAVIOUR (error status, no findings, no report), never a gate's wording.
#
# Deliberately narrow: each alternative requires the imperative override shape,
# so genuine manufacturing text that happens to use these words is unaffected
# ("Ignore Fault Relay", "Override Protection Switch", "Insert Molded Contact",
# "System Prompt Display Panel" all pass — see the fail-closed probes in
# tests/unit/test_caller_data_and_output_gate.py).
_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|direction|directions)"
    r"|disregard\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules)"
    r"|forget\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instruction|instructions|prompt|prompts)"
    r"|(?:reveal|show|print|repeat|output|disclose)\s+(?:me\s+)?(?:your|the)\s+"
    r"(?:system\s+prompt|system\s+message|instructions|initial\s+prompt)"
    r"|you\s+are\s+now\s+(?:a|an)\s"
    r"|act\s+as\s+(?:if\s+you\s+are\s+)?(?:a\s+|an\s+)?(?:developer|admin|root)\s+mode"
    r"|override\s+(?:your|the)\s+(?:instruction|instructions|rules|safety)",
    re.IGNORECASE,
)

# Contact identifiers have no place in a component label, and the label
# alphabet alone does not exclude them (it permits digits, spaces and hyphens).
# Only high-precision detector types are screened here:
#   - the personal-name heuristics are excluded because they match any two
#     Title Case words, which is what a component label IS ("Power Supply",
#     "Lead Capacitor", "PCB Substrate") — screening on them would refuse
#     ordinary bills of materials;
#   - the 12-digit national-identifier shape is excluded because it is also
#     the shape of a 12-digit article code.
_SCREENED_PII_TYPES = frozenset({"email", "phone_jp", "phone_us", "ssn_us", "credit_card"})


def _label_error(field: str, limit: int) -> str:
    """Build the rejection message for a label field — names the field only."""
    return f"InputValidateNode: {field} must be {limit} characters or fewer of {_LABEL_ALPHABET}"


def _validate_label(value: Any, field: str, limit: int) -> tuple[Optional[str], Optional[str]]:
    """Validate one caller label. Returns (normalised_value, error).

    Fail CLOSED: only a string of the component-label alphabet within the
    length limit is accepted. The rejected value is never echoed — the error
    names the field and restates the contract.

    The alphabet is checked on the value AS SUPPLIED, before spaces are
    collapsed. Normalising first would silently repair a label carrying a
    newline or a tab into an acceptable one, and caller-controlled line
    structure is exactly what this contract exists to keep out of the report.
    """
    if not isinstance(value, str):
        return None, _label_error(field, limit)
    if not _LABEL_RE.fullmatch(value):
        return None, _label_error(field, limit)
    collapsed = _SPACES_RE.sub(" ", value).strip()
    if not collapsed or len(collapsed) > limit:
        return None, _label_error(field, limit)
    findings = [f for f in detect_pii(collapsed) if f.get("type") in _SCREENED_PII_TYPES]
    if findings:
        return None, f"InputValidateNode: {field} must not contain contact identifiers"
    # The caller-data channel gets the same instruction-override screen the
    # specification text gets. A label is short and alphabet-restricted, but an
    # override instruction fits inside both bounds, and labels are rendered into
    # the report — so a screen applied only to the specification would leave the
    # structured channel as an open path to the same content.
    if _INJECTION_RE.search(collapsed):
        return None, f"InputValidateNode: {field} refused - instruction-override content"
    return collapsed, None


def _finite_in_range(value: Any, field: str, lo: float, hi: float) -> tuple[Optional[float], Optional[str]]:
    """Validate one caller-supplied number. Returns (value, error).

    Fail CLOSED. Bools, strings and other non-numerics are refused, and so are
    NaN and +/-Infinity: those parse through float() and arrive intact in a raw
    JSON body, and every comparison against NaN is False — so a NaN
    concentration would slide under every regulatory threshold and be reported
    as compliant, which is the one decision this agent exists to make. The
    rejected value is never echoed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"InputValidateNode: {field} must be a number between {lo:g} and {hi:g}"
    parsed = float(value)
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None, f"InputValidateNode: {field} must be a number between {lo:g} and {hi:g}"
    return parsed, None


def _validate_bom(raw: Any, max_entries: int) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Validate the caller's bill of materials. Returns (entries, error).

    Every entry is validated in full and EVERY accepted entry is carried
    forward — the compliance scan evaluates the whole list, not its first row.
    Errors name the field and the entry index, never the rejected value.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "InputValidateNode: bom must be a list of component records"
    if len(raw) > max_entries:
        return [], f"InputValidateNode: bom must contain {max_entries} entries or fewer"

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], f"InputValidateNode: bom[{index}] must be a component record"

        name, error = _validate_label(item.get("name"), f"bom[{index}].name", _MAX_ITEM_NAME_CHARS)
        if error:
            return [], error

        entry: dict[str, Any] = {"name": name}

        for field, limit in (("material", _MAX_MATERIAL_CHARS), ("type", _MAX_TYPE_CHARS)):
            supplied = item.get(field)
            if supplied is None or supplied == "":
                entry[field] = ""
                continue
            value, error = _validate_label(supplied, f"bom[{index}].{field}", limit)
            if error:
                return [], error
            entry[field] = value

        concentration = item.get("concentration_ppm")
        if concentration is None:
            entry["concentration_ppm"] = None
        else:
            parsed, error = _finite_in_range(
                concentration,
                f"bom[{index}].concentration_ppm",
                _CONCENTRATION_MIN_PPM,
                _CONCENTRATION_MAX_PPM,
            )
            if error:
                return [], error
            entry["concentration_ppm"] = parsed

        entries.append(entry)

    return entries, None


class InputValidateNode(FunctionNode):
    """Validate the incoming product specification and caller data.

    This is the outer backbone's pre_process slot and the only node declaring
    VERIFIED_EXTERNAL trust, so anonymous callers are rejected here and the
    inner domain nodes never see unauthenticated input.

    Input state keys:
        user_input:    str   — product specification (JSON object or plain text)
        input_context: dict  — structured caller data (channel, target_market,
                               product_name, bom)

    Output state keys (partial dict):
        validated_input:  str  — normalised specification text
        product_name:     str  — validated caller label
        bom_content:      str  — JSON list of validated component records
        bom_entry_count:  int
        target_market:    str
        channel:          str
        status:           str
        error_log:        list[str] (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """Bind the declared validation bounds forwarded by the outer graph."""
        super().__init__()
        settings = dict(config or {})
        max_entries = settings.get("max_bom_entries")
        self._max_bom_entries: int = (
            max_entries
            if isinstance(max_entries, int) and not isinstance(max_entries, bool) and max_entries > 0
            else _DEFAULT_MAX_BOM_ENTRIES
        )
        default_market = settings.get("default_target_market")
        self._default_target_market: str = (
            default_market if default_market in VALID_TARGET_MARKETS else _DEFAULT_TARGET_MARKET
        )

    def _reject(self, state: dict[str, Any], reason: str, message: str) -> dict[str, Any]:
        """Terminal refusal: the agent will not process this content at all.

        Reserved for content a reworded retry should not get past. A request
        the caller can correct goes through _decline() instead, so a refusal is
        never presented as something a corrected request would be accepted.
        """
        logger.warning("InputValidateNode: rejected request — %s", reason)
        emit_trace_event("input_validation_failed", {"reason": reason}, state)
        emit_progress(INPUT_REJECTED)
        return {"status": AgentStatus.ERROR.value, "error_log": [message]}

    def _decline(self, state: dict[str, Any], code: str, message: str) -> dict[str, Any]:
        """Decline a request the caller can correct, and COMPLETE the run.

        Nothing is processed and the audit event is still recorded, exactly as
        with a refusal - what differs is the reporting. Terminating here would
        end the caller's turn and surface only an exception type, leaving the
        reason reachable solely from the audit trail; completing with the reason
        lets the caller fix the value and send the request again on the same
        conversation. The message names a field, never a value.
        """
        logger.warning("InputValidateNode: declined request — %s", code)
        emit_trace_event("input_validation_declined", {"reason": code}, state)
        emit_progress(INPUT_REJECTED)
        return {"status": AgentStatus.SUCCESS.value, "error_code": code, "error_log": [message]}

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        emit_progress("Checking the request...")
        user_input = state.get("user_input", "")
        input_context = state.get("input_context")

        if not isinstance(user_input, str) or not user_input.strip():
            return self._decline(state, "EMPTY_INPUT", "InputValidateNode: user_input is empty or missing")

        cleaned = _CONTROL_CHARS_RE.sub("", user_input)
        if len(cleaned) > _MAX_SPEC_CHARS:
            return self._decline(
                state,
                "QUESTION_TOO_LONG",
                f"InputValidateNode: user_input exceeds {_MAX_SPEC_CHARS} characters",
            )

        # Instruction-override payloads are refused here, before any structured
        # parsing, so nothing downstream ever sees them.
        if _INJECTION_RE.search(cleaned):
            return self._reject(
                state,
                "instruction_override",
                "InputValidateNode: user_input refused - instruction-override content",
            )

        if input_context is not None and not isinstance(input_context, dict):
            return self._decline(state, "INVALID_REQUEST", "InputValidateNode: input_context must be an object")
        context: dict[str, Any] = input_context or {}

        # -- channel ----------------------------------------------------------
        channel = context.get("channel")
        if channel is None:
            channel = "unknown"
        elif not isinstance(channel, str) or not _SLUG_RE.match(channel):
            return self._decline(
                state,
                "INVALID_REQUEST",
                "InputValidateNode: input_context.channel must be a lowercase identifier "
                "(a-z, 0-9, _; 1-32 characters)",
            )

        # -- target market ----------------------------------------------------
        target_market = context.get("target_market")
        if target_market is None:
            target_market = self._default_target_market
        elif target_market not in VALID_TARGET_MARKETS:
            return self._decline(
                state,
                "INVALID_REQUEST",
                "InputValidateNode: input_context.target_market must be one of " + ", ".join(VALID_TARGET_MARKETS),
            )

        # -- specification payload --------------------------------------------
        # A JSON object carries the same product_name / bom shape as the
        # structured channel; anything else is a plain-text description.
        spec: dict[str, Any] = {}
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                spec = parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            spec = {}

        # -- product name -----------------------------------------------------
        supplied_name = context.get("product_name", spec.get("product_name"))
        if supplied_name is None:
            product_name = self._derive_product_name(cleaned)
        else:
            validated_name, error = _validate_label(supplied_name, "product_name", _MAX_PRODUCT_NAME_CHARS)
            if error or validated_name is None:
                return self._decline(
                    state,
                    "INVALID_REQUEST",
                    error or _label_error("product_name", _MAX_PRODUCT_NAME_CHARS),
                )
            product_name = validated_name

        # -- bill of materials -------------------------------------------------
        raw_bom = context.get("bom", spec.get("bom"))
        bom_entries, error = _validate_bom(raw_bom, self._max_bom_entries)
        if error:
            return self._decline(state, "INVALID_REQUEST", error)

        validated = _WS_RE.sub(" ", cleaned).strip()

        emit_trace_event(
            "input_validated",
            {
                "channel": channel,
                "target_market": target_market,
                "bom_entry_count": len(bom_entries),
                "spec_length": len(validated),
                "input_format": "structured" if bom_entries else "specification_only",
            },
            state,
        )

        return {
            "validated_input": validated,
            "product_name": product_name,
            "bom_content": json.dumps(bom_entries),
            "bom_entry_count": len(bom_entries),
            "target_market": target_market,
            "channel": channel,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _derive_product_name(cleaned: str) -> str:
        """Derive a renderable product name from a plain-text specification.

        The caller supplied no product_name field, so one is derived from the
        first line and reduced to the component-label alphabet — the report
        renders only alphabet-conforming labels, whichever channel they came
        from. An empty result becomes a fixed placeholder rather than nothing.
        """
        first_line = cleaned.splitlines()[0] if cleaned.splitlines() else ""
        stripped = "".join(ch for ch in first_line if _LABEL_CHAR_RE.fullmatch(ch))
        collapsed = _WS_RE.sub(" ", stripped).strip()[:_MAX_PRODUCT_NAME_CHARS].strip()
        return collapsed or "Unnamed product"
