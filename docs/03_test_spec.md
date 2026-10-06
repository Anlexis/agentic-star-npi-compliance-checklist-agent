# Test Specification — MFG-C2-007

## Test strategy

- 168 tests ship with the template; all run offline against the installed framework
  wheel, with no external service, model or network access.
- Types: unit (node contracts, caller-data contract, output boundary), proof-of-boundary
  (backbone order, import isolation, state safety) and end-to-end through the real HTTP
  entry point.
- Run them with `python -m pytest tests/ -v` from the repository root.

| File | Tests | Scope |
|------|-------|-------|
| `tests/unit/test_agent.py` | 32 | Node-level contracts for all five domain nodes |
| `tests/unit/test_caller_data_and_output_gate.py` | 100 | The caller-data contract and the output boundary |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | 2 | The framework gates cannot be overridden |
| `tests/proof_of_boundary/test_invoke_e2e.py` | 25 | End-to-end through the real ASGI entry point |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | 6 | Backbone traversal order |
| `tests/proof_of_boundary/test_import_isolation.py` | 1 | No platform-SDK imports |
| `tests/proof_of_boundary/test_state_safety.py` | 1 | State carries no credentials or unserialisable types |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | 1 | Skips — this template enables no cross-boundary interrupt |

## Framework compliance

| TC-ID | Test | Expected result | Where |
|-------|------|-----------------|-------|
| TC-01 | State contract: flat TypedDict | No Pydantic or dataclass in the schema | `test_state_safety.py` |
| TC-02 | Invalid input is rejected | Error status, nothing carried forward | `test_caller_data_and_output_gate.py` |
| TC-03 | No credentials in State | Credential scan reports zero violations | `test_state_safety.py` |
| TC-04 | Invocation context via `configurable` only | State carries no context object | `test_state_safety.py` |
| TC-05 | No duplicate lifecycle events in `execute()` | Domain events only; the framework owns the lifecycle | `test_agent.py` |
| TC-06 | The default input gate cannot be overridden | `TypeError` at class definition | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | The default output gate cannot be overridden | `TypeError` at class definition | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` is declared and enforced | An unauthenticated caller is refused with no report | `test_invoke_e2e.py` |
| TC-09 | Domain input checks execute | Instruction-override refused; ordinary text unaffected | `test_caller_data_and_output_gate.py` |
| TC-10 | Domain output checks execute | Credential withholding, label containment, disclosure schema | `test_caller_data_and_output_gate.py` |
| TC-11 | At least one domain audit event per node | One event on every invocation path | `test_agent.py` |

## Proof of boundary

| PB-ID | Boundary | Test | Expected result | Where |
|-------|----------|------|-----------------|-------|
| PB-1 | Node to audit sink | An audit event fires on every invocation path | No silent failures | `test_agent.py` |
| PB-2 | State serialisation | Post-invoke State is primitives only | No Pydantic or dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan for platform-SDK imports | Zero violations | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | State carries no credential-shaped fields | Inspection passes | `test_state_safety.py` **Auto-waived — checkpointing disabled**: `config/config.yaml` enables neither `memory_enabled` nor `hitl.enabled`, so no checkpoint surface exists; the conditional gate and the non-lossy traversal helper ship with the stub. |
| PB-6 | Invoke execution order | Backbone traversal, authenticated caller | Fixed five-node order | `test_pb_invoke_order.py` |
| PB-7 | Interrupt propagation | Not applicable — `propagate_hitl` is False and no interrupt checkpoint exists | Skips with a stated reason | `test_pb7_hitl_interrupt_propagation.py` |

## Business logic

| BL-ID | Test | Input | Expected result |
|-------|------|-------|-----------------|
| BL-01 | Every submitted row is evaluated | 12 components, two regimes in scope | 24 findings across 12 distinct components |
| BL-02 | Component labels survive the outer/inner boundary | `Lead Capacitor C12` | Rendered verbatim; no mask token in the report |
| BL-03 | Cadmium uses the tighter limit | 150 ppm cadmium | `blocking`; verdict `blocked` |
| BL-04 | Other substances use the general limit | 150 ppm lead / 1,500 ppm lead | `monitor` / `blocking` |
| BL-05 | An undeclared concentration is never a pass | Restricted substance, no figure | `unverified`; verdict `needs_review` |
| BL-06 | Every verdict is reachable end to end | Clean / monitor / blocking submissions | `pass`, `needs_review`, `blocked` |
| BL-07 | Market scope changes the regimes evaluated | `eu` vs `jp` vs `jp_eu` | Certification regime excluded for `eu`; candidate list excluded for `jp` |
| BL-08 | No bill of materials degrades to the baseline | Specification text only | Success, with a stated no-components note |
| BL-09 | The declared entry cap is live | `max_bom_entries` and one more | Accepted, then refused |
| BL-10 | The scan budget fails closed | Exhausted budget over 200 components | Error; no verdict rendered |

## Caller-data contract

| ID | Test | Input | Expected result |
|----|------|-------|-----------------|
| CD-01 | Non-finite concentration | `NaN`, `Infinity`, `-Infinity`, raw floats, booleans, strings, lists, objects | Refused, field named, value not echoed |
| CD-02 | Raw JSON `NaN` on the wire | Hand-built body carrying a bare `NaN` | Refused |
| CD-03 | Out-of-range concentration | `-1`, `1,000,001`, `1e12` | Refused |
| CD-04 | In-range concentration | `0`, `40`, `999.5`, `1000`, `1,000,000` | Accepted |
| CD-05 | Channel is an identifier | `Ops Team <script>` | Refused |
| CD-06 | Target market is a closed set | `us` | Refused |
| CD-07 | Labels outside the alphabet | markup, newline, colon, at-sign, over-length, empty, non-string | Refused, entry index named |
| CD-08 | Real component labels accepted | 12 genuine part designations, including ones containing screen keywords | Accepted verbatim |
| CD-09 | Contact identifiers in a label | A phone number | Refused |
| CD-13 | Instruction override on the caller-data channel | Override text in `bom[].name` and in `product_name` | Refused, field named |
| CD-10 | A rejected value is never echoed | A credential-shaped channel value | Absent from the error |
| CD-11 | One invalid row rejects the submission | 3 rows, the middle one invalid | Refused, `bom[1].name` named |
| CD-12 | Oversized caller payload | Beyond the adapter cap | 413 at the adapter |

## Output boundary

| ID | Test | Input | Expected result |
|----|------|-------|-----------------|
| OB-01 | Off-schema figures reduced to a band | 9 representations: attached, tabbed, signed, comma-grouped, percentage | Replaced by the band phrase |
| OB-02 | Approved thresholds byte-identical | `1,000 ppm`, `100 ppm`, `0.1% w/w`, `0.01% w/w` | Unchanged |
| OB-03 | Manufacturing identifiers byte-identical | `SKF-6205-2RS`, `EAB64785603`, `MFG-2026-001`, `8000 rpm`, `v12`, `100-240V` | Unchanged |
| OB-04 | The delimiter never spans a blank line | A figure ending one block, `ppm` opening the next | Unchanged |
| OB-05 | Credential scan precedes the rewrite | A token whose text contains a figure and a unit | Report withheld; the token never rendered |
| OB-06 | Out-of-contract label withheld | A label carrying markup and a newline | Replaced; markup absent |
| OB-07 | Conforming label untouched | `SKF-6205-2RS Bearing` | Byte-identical |
| OB-08 | The layers are independent | A report needing both a reduction and a clean scan | Both applied |
| OB-09 | No supplier figure is rendered | 137 ppm and 4,321 ppm submitted | Neither figure appears; only bands and thresholds |

## Execution summary

- Total tests: 168
- Result: 167 passed, 1 skipped (PB-7, not applicable to this template)
- Warnings: none

## Marketplace Entry Point — `tests/unit/test_cli_entry_point.py`

| ID | Case | Expected |
|----|------|----------|
| CLI-01 | `cli.py` imports | module loads; `run_agent_marketplace`, `load_agent_config` and `NPIComplianceChecklistAgent` are present |
| CLI-02 | override seam ships empty | `extend_config == {}`; a stray value would silently outrank `config/config.yaml` on the Marketplace path only |
| CLI-03 | the runner receives what the image's CMD would send | executing `cli.py` as `__main__` with the runner replaced captures the call: the graph class, `agent_name`, `namespace`, and every value declared in `config/config.yaml`. Loading the module alone never runs that block, so a wrong class or a dropped config there would otherwise ship unnoticed |

`cli.py` is imported by no other module, so nothing else in the suite would
notice if its import path, graph class or config assembly broke; the image
would build and fail only when the Pod starts. Skipped where the platform
events package is absent.
