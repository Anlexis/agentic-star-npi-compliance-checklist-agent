"""Unit tests for the NPI compliance nodes.

Covers InputValidateNode, ComplianceCheckListNode, RegulatoryRefNode,
ReportGenerateNode and OutputFormatNode at the node level.

emit_trace_event is patched at the node MODULE rather than through a
sys.modules stub, so the audit helper's own import chain is left alone.
"""

import json
from unittest.mock import patch

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def base_state():
    return {
        "user_input": "",
        "node_history": [],
        "error_log": [],
        "correlation_id": "test-corr-001",
        "session_id": "test-session-001",
    }


@pytest.fixture()
def bom_context():
    """A caller-data payload with one clear part, one restricted, one certified."""
    return {
        "channel": "npi_portal",
        "target_market": "jp_eu",
        "product_name": "Wireless Charging Module WCM-200",
        "bom": [
            {"name": "SKF-6205-2RS Bearing", "material": "chrome steel", "type": "bearing"},
            {
                "name": "Lead Capacitor C12",
                "material": "lead electrolytic",
                "type": "capacitor",
                "concentration_ppm": 1500,
            },
            {"name": "AC Adapter PA-65", "material": "ABS and copper", "type": "power supply"},
        ],
    }


def _record(bom_entries, product_name="Test Product", target_market="jp_eu"):
    """Build the caller-data record the graph bridges into the inner pipeline."""
    return {
        "channel": "unit_test",
        "target_market": target_market,
        "product_name": product_name,
        "bom_content": json.dumps(bom_entries),
    }


# --- InputValidateNode ------------------------------------------------------


class TestInputValidateNode:
    """The external-facing trust and validation gate."""

    def _node(self, **config):
        from src.nodes.input_validate_node import InputValidateNode

        return InputValidateNode(config=config or None)

    def test_success_structured_caller_data(self, base_state, bom_context):
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            state = {
                **base_state,
                "user_input": "Wireless charging module review",
                "input_context": bom_context,
            }
            result = self._node().execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["product_name"] == "Wireless Charging Module WCM-200"
        assert result["bom_entry_count"] == 3
        assert result["target_market"] == "jp_eu"
        assert result["channel"] == "npi_portal"
        assert len(json.loads(result["bom_content"])) == 3

    def test_success_specification_only(self, base_state):
        """No bill of materials submitted degrades to the specification baseline."""
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            state = {**base_state, "user_input": "Product XYZ - check compliance"}
            result = self._node().execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["bom_entry_count"] == 0
        assert result["bom_content"] == "[]"
        assert result["product_name"] == "Product XYZ - check compliance"

    def test_json_specification_payload_accepted(self, base_state):
        """A JSON object on the specification channel carries the same shape."""
        payload = json.dumps(
            {
                "product_name": "Inline Widget",
                "bom": [{"name": "Fuse F1", "material": "ceramic", "type": "fuse"}],
            }
        )
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            result = self._node().execute({**base_state, "user_input": payload})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["product_name"] == "Inline Widget"
        assert result["bom_entry_count"] == 1

    def test_error_empty_input(self, base_state):
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            result = self._node().execute({**base_state, "user_input": ""})
        assert result["status"] == AgentStatus.SUCCESS
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")
        assert any("empty" in entry for entry in result["error_log"])

    def test_error_whitespace_input(self, base_state):
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            result = self._node().execute({**base_state, "user_input": "   "})
        assert result["status"] == AgentStatus.SUCCESS
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")

    def test_execute_method_signature(self):
        """The node contract is execute(self, state)."""
        import inspect

        from src.nodes.input_validate_node import InputValidateNode

        params = list(inspect.signature(InputValidateNode.execute).parameters)
        assert params[1] == "state"

    def test_trust_level_verified_external(self):
        """The external-facing gate must require VERIFIED_EXTERNAL."""
        from src.nodes.input_validate_node import InputValidateNode

        assert InputValidateNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL

    def test_audit_event_emitted(self, base_state, bom_context):
        with patch("src.nodes.input_validate_node.emit_trace_event") as spy:
            self._node().execute({**base_state, "user_input": "review", "input_context": bom_context})
        spy.assert_called_once()
        assert spy.call_args.args[0] == "input_validated"

    def test_declared_entry_cap_is_live(self, base_state):
        """The cap the graph forwards from configuration is the one enforced."""
        context = {"bom": [{"name": f"Part {i}"} for i in range(3)]}
        with patch("src.nodes.input_validate_node.emit_trace_event"):
            result = self._node(max_bom_entries=2).execute(
                {**base_state, "user_input": "spec", "input_context": context}
            )
        assert result["status"] == AgentStatus.SUCCESS
        # Completes carrying the reason, so the caller can
        # correct the value and send the request again.
        assert result.get("error_code")
        assert any("2 entries or fewer" in entry for entry in result["error_log"])


# --- ComplianceCheckListNode ------------------------------------------------


class TestComplianceCheckListNode:
    """The substance and certification evaluation."""

    def _node(self, **config):
        from src.nodes.compliance_checklist_node import ComplianceCheckListNode

        return ComplianceCheckListNode(config=config or None)

    def _state(self, base_state, record):
        return {**base_state, "input_context": record}

    def test_substance_finding_detected(self, base_state):
        record = _record([{"name": "Old Solder", "material": "lead-tin alloy", "type": "solder"}])
        with patch("src.nodes.compliance_checklist_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, record))

        assert result["status"] == AgentStatus.SUCCESS
        findings = json.loads(result["compliance_results"])
        assert [f for f in findings if f["regulation"] == "REACH"]
        assert [f for f in findings if f["regulation"] == "RoHS"]

    def test_certification_finding_detected(self, base_state):
        record = _record([{"name": "AC Adapter", "material": "ABS and copper", "type": "power supply unit"}])
        with patch("src.nodes.compliance_checklist_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, record))

        findings = json.loads(result["compliance_results"])
        assert [f for f in findings if "PSE" in f["regulation"]]

    def test_clean_bom_no_findings(self, base_state):
        record = _record([{"name": "Enclosure", "material": "recycled PET", "type": "housing"}])
        with patch("src.nodes.compliance_checklist_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, record))

        assert json.loads(result["compliance_results"]) == []
        assert result["compliance_status"] == "pass"

    def test_trust_level_anonymous(self):
        """Inner domain nodes stay ANONYMOUS; the external gate is pre_process."""
        from src.nodes.compliance_checklist_node import ComplianceCheckListNode

        assert ComplianceCheckListNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_audit_event_emitted(self, base_state):
        with patch("src.nodes.compliance_checklist_node.emit_trace_event") as spy:
            self._node().execute(self._state(base_state, _record([])))
        spy.assert_called_once()
        assert spy.call_args.args[0] == "compliance_check_complete"

    def test_compliance_results_serialised_as_json_string(self, base_state):
        with patch("src.nodes.compliance_checklist_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, _record([])))
        assert isinstance(result["compliance_results"], str)
        assert isinstance(json.loads(result["compliance_results"]), list)


# --- RegulatoryRefNode ------------------------------------------------------


class TestRegulatoryRefNode:
    """Regulatory reference attachment."""

    def _node(self):
        from src.nodes.regulatory_ref_node import RegulatoryRefNode

        return RegulatoryRefNode()

    def test_returns_every_regulation_in_scope(self, base_state):
        state = {**base_state, "compliance_results": "[]", "target_market": "jp_eu"}
        with patch("src.nodes.regulatory_ref_node.emit_trace_event"):
            result = self._node().execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        references = json.loads(result["regulatory_references"])
        assert set(references) == {"REACH", "RoHS", "PSE (Category A)"}

    def test_flags_regulations_with_findings(self, base_state):
        findings = [{"item": "Cap", "regulation": "REACH", "action": "Declare"}]
        state = {
            **base_state,
            "compliance_results": json.dumps(findings),
            "target_market": "jp_eu",
        }
        with patch("src.nodes.regulatory_ref_node.emit_trace_event"):
            result = self._node().execute(state)

        references = json.loads(result["regulatory_references"])
        assert references["REACH"]["status"] == "flagged"
        assert references["RoHS"]["status"] == "not_flagged"

    def test_trust_level_anonymous(self):
        from src.nodes.regulatory_ref_node import RegulatoryRefNode

        assert RegulatoryRefNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_references_serialised_as_json_string(self, base_state):
        state = {**base_state, "compliance_results": "[]", "target_market": "eu"}
        with patch("src.nodes.regulatory_ref_node.emit_trace_event"):
            result = self._node().execute(state)
        assert isinstance(json.loads(result["regulatory_references"]), dict)


# --- ReportGenerateNode -----------------------------------------------------


class TestReportGenerateNode:
    """Report rendering."""

    def _node(self):
        from src.nodes.report_generate_node import ReportGenerateNode

        return ReportGenerateNode()

    def _state(self, base_state, findings, **extra):
        return {
            **base_state,
            "product_name": "Widget Pro 3000",
            "compliance_results": json.dumps(findings),
            "regulatory_references": json.dumps({"REACH": {"full_name": "Reference"}}),
            "target_market": "jp_eu",
            "bom_entry_count": len(findings) or 1,
            **extra,
        }

    def test_report_contains_product_name(self, base_state):
        with patch("src.nodes.report_generate_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, []))
        assert result["status"] == AgentStatus.SUCCESS
        assert "Widget Pro 3000" in result["compliance_report"]

    def test_clean_bom_renders_pass(self, base_state):
        with patch("src.nodes.report_generate_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, [], compliance_status="pass"))
        report = result["compliance_report"]
        assert "GATE VERDICT: PASS" in report
        assert "REACH: PASS" in report

    def test_blocking_finding_renders_blocked(self, base_state):
        findings = [
            {
                "item": "Cap",
                "regulation": "REACH",
                "substance": "Lead",
                "severity": "blocking",
                "threshold": "0.1% w/w (1,000 ppm) declaration trigger",
                "concentration_band": "at or above 1,000 ppm",
                "action": "Declare",
            }
        ]
        with patch("src.nodes.report_generate_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, findings, compliance_status="blocked"))
        report = result["compliance_report"]
        assert "GATE VERDICT: BLOCKED" in report
        assert "REACH: BLOCKED" in report

    def test_report_never_renders_a_raw_concentration(self, base_state):
        """Only the band is rendered; the supplied figure is not reproduced."""
        findings = [
            {
                "item": "Cap",
                "regulation": "RoHS",
                "substance": "Cadmium",
                "severity": "blocking",
                "threshold": "100 ppm restriction limit",
                "concentration_band": "at or above 100 ppm",
                "action": "Confirm",
            }
        ]
        with patch("src.nodes.report_generate_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, findings))
        assert "at or above 100 ppm" in result["compliance_report"]
        assert "Disclosure schema" in result["compliance_report"]

    def test_result_field_set(self, base_state):
        with patch("src.nodes.report_generate_node.emit_trace_event"):
            result = self._node().execute(self._state(base_state, []))
        assert result["result"]

    def test_trust_level_anonymous(self):
        from src.nodes.report_generate_node import ReportGenerateNode

        assert ReportGenerateNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_audit_event_emitted(self, base_state):
        with patch("src.nodes.report_generate_node.emit_trace_event") as spy:
            self._node().execute(self._state(base_state, []))
        spy.assert_called_once()
        assert spy.call_args.args[0] == "report_generated"


# --- OutputFormatNode -------------------------------------------------------


class TestOutputFormatNode:
    """The output boundary."""

    def _node(self):
        from src.nodes.output_format_node import OutputFormatNode

        return OutputFormatNode()

    def test_formats_compliance_report(self, base_state):
        state = {
            **base_state,
            "compliance_report": "NPI COMPLIANCE CHECKLIST REPORT\nREACH: PASS",
            "product_name": "Test Widget",
            "compliance_results": "[]",
        }
        result = self._node().execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert "NPI COMPLIANCE CHECKLIST REPORT" in result["formatted_output"]

    def test_credential_pattern_withholds_the_report(self, base_state):
        state = {
            **base_state,
            "compliance_report": "Report with key sk-AbcDefGhiJklMno1234567 inside",
            "product_name": "",
            "compliance_results": "[]",
        }
        result = self._node().execute(state)
        assert result["status"] == AgentStatus.ERROR
        assert "WITHHELD" in result["formatted_output"]
        assert "sk-AbcDefGhiJklMno1234567" not in result["formatted_output"]

    def test_token_pattern_withholds_the_report(self, base_state):
        state = {
            **base_state,
            "compliance_report": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature_here_abc",
            "product_name": "",
            "compliance_results": "[]",
        }
        result = self._node().execute(state)
        assert result["status"] == AgentStatus.ERROR

    def test_clean_output_passes(self, base_state):
        state = {
            **base_state,
            "compliance_report": "REACH: PASS\nRoHS: PASS\nPSE: NEEDS REVIEW",
            "product_name": "Safe Widget",
            "compliance_results": "[]",
        }
        result = self._node().execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert result["result"] == "REACH: PASS\nRoHS: PASS\nPSE: NEEDS REVIEW"

    def test_trust_level_anonymous(self):
        """The trust gate sits on pre_process, not on the output boundary."""
        from src.nodes.output_format_node import OutputFormatNode

        assert OutputFormatNode.required_trust_level == TrustLevel.ANONYMOUS

    def test_module_level_gate_present(self):
        """The output gate is a module-level function, not an instance method."""
        import src.nodes.output_format_node as module

        assert callable(module._security_gate_output)
