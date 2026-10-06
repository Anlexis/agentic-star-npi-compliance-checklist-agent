# NPI Compliance Checklist Agent

AI agent for producing new product introduction compliance checklists, built with Agentic Star.

> **Category**: Cat 2 (a domain pipeline: a multi-step compliance workflow behind the fixed agent backbone)
> **Industry**: Manufacturing
> **Template ID**: MFG-C2-007

## Overview

Checks a new product's bill of materials against the REACH, RoHS and electrical-appliance
certification requirement sets before it clears a new-product-introduction gate. It grades each
gap by whether it holds the gate, and returns a compliance checklist and gap report together
with a machine-readable verdict (`pass`, `needs_review` or `blocked`) that a gate workflow can
act on directly.

The evaluation is deterministic and offline: substance and certification rules are static
requirement sets, not a model call, so the same submission always yields the same verdict.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/02_design.md` for the architecture and the caller contract, and `docs/03_test_spec.md`
for what the shipped test suite covers.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the requirement sets in `src/services/service.py` and the substance lists in
   `src/nodes/compliance_checklist_node.py` with the ones your programme is governed by.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
