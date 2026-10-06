"""AgentCore Platform v1.0"""

# Service layer: domain reference data and lookups.
# Must NOT contain business logic, routing, or credentials. Nodes call this.
#
# The regulatory reference data below is a static, versioned snapshot: the
# regimes this template evaluates publish stable obligations, thresholds and
# competent authorities, so the lookup is deterministic and needs no network
# call. RegulatoryRefNode is the only caller.

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping

# Regulation scope per target market, mirroring the scope the compliance scan
# applies (src/nodes/compliance_checklist_node.py).
_MARKET_SCOPE: Mapping[str, tuple[str, ...]] = {
    "jp": ("RoHS", "PSE (Category A)"),
    "eu": ("REACH", "RoHS"),
    "jp_eu": ("REACH", "RoHS", "PSE (Category A)"),
}

_REGULATORY_REFERENCES: Mapping[str, Mapping[str, str]] = {
    "REACH": {
        "full_name": "EU Regulation (EC) No 1907/2006 on the registration, evaluation, "
        "authorisation and restriction of chemicals",
        "summary": (
            "Substances on the candidate list of substances of very high concern must be "
            "communicated down the supply chain once they are present in an article above "
            "0.1% w/w. Use above a safe threshold requires an authorisation."
        ),
        "threshold": "0.1% w/w (1,000 ppm) declaration trigger",
        "authority": "European Chemicals Agency",
        "key_obligation": "Candidate-list communication duty; candidate-list maintenance",
        "market_note": "Applies to articles placed on the European market; the Japanese "
        "chemical-substances control regime carries parallel notification duties.",
    },
    "RoHS": {
        "full_name": "EU Directive 2011/65/EU on the restriction of hazardous substances "
        "in electrical and electronic equipment (recast)",
        "summary": (
            "Restricts ten hazardous substances in electrical and electronic equipment "
            "placed on the European market. The Japanese marking standard for specified "
            "chemical substances carries parallel disclosure duties for the domestic market."
        ),
        "threshold": "1,000 ppm per homogeneous material; 100 ppm for cadmium",
        "authority": "European Commission; the Japanese industry ministry for the " "domestic marking standard",
        "key_obligation": "Restricted-substances annex; homogeneous-material conformity",
        "market_note": "The Japanese marking standard requires content disclosure for the " "domestic market.",
    },
    "PSE (Category A)": {
        "full_name": "Japanese Electrical Appliance and Material Safety Act",
        "summary": (
            "Category A products must obtain the diamond conformity mark through a "
            "registered certification body before domestic market entry. Conformity "
            "assessment is mandatory — self-declaration is not available for this category."
        ),
        "threshold": "Mandatory certification (no quantity threshold)",
        "authority": "The Japanese industry ministry",
        "key_obligation": "Third-party conformity assessment; registered-certification-body " "attestation",
        "market_note": "Directly applicable to the Japanese domestic market.",
    },
}


class RegulatoryReferenceService:
    """Look up the regulatory reference data for a target market."""

    @staticmethod
    def regulations_for_market(target_market: str) -> tuple[str, ...]:
        """Return the regulation keys in scope for a market slug.

        An unrecognised slug returns the widest scope rather than an empty one,
        so a scope resolution failure can never silently produce a report with
        no obligations in it.
        """
        return _MARKET_SCOPE.get(target_market, _MARKET_SCOPE["jp_eu"])

    @staticmethod
    def lookup(target_market: str, flagged: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Return the references in scope, each marked flagged or not_flagged.

        `flagged` is the set of regulation names that produced findings. Every
        regulation in scope is returned — a reviewer needs the obligations for
        the clear regimes as much as for the flagged ones.
        """
        flagged_set = {str(name) for name in flagged}
        references: Dict[str, Dict[str, Any]] = {}
        for key in RegulatoryReferenceService.regulations_for_market(target_market):
            reference = dict(_REGULATORY_REFERENCES[key])
            reference["status"] = "flagged" if key in flagged_set else "not_flagged"
            references[key] = reference
        return references
