# Template Design Specification — MFG-C2-007

## Position in the Architecture

- **Agent class**: `NPIComplianceChecklistAgent`
- **L1 Base (framework base class)**: `AgentBaseGraph` — direct framework inheritance
- **Inner graph base**: `BaseGraph` (custom topology for the domain workflow)
- **Pattern**: Category 2 — a nested `GraphNode` wrapping a multi-step pipeline
- **Three-layer separation**
  - State: a flat `TypedDict` (`State(AgentState)`) — no Pydantic; list/dict payloads
    are `Optional[str]` and JSON-encoded by the producing node
  - Nodes: `FunctionNode` subclasses implementing `execute(self, state) -> dict`
  - Graphs: outer `AgentBaseGraph` (backbone) + inner `BaseGraph` (domain workflow)

## Architecture Overview

### The nested Category 2 shape

```
Caller (VERIFIED_EXTERNAL)
  |
  v
[Outer backbone — AgentBaseGraph]
  START -> initialize
        -> pre_process    (InputValidateNode, VERIFIED_EXTERNAL)
        -> main           (NPIComplianceGraphNode, GraphNode)
              |
              v  [Inner workflow — BaseGraph]
              compliance_checklist -> regulatory_ref -> report_generate
              (all ANONYMOUS)
        -> post_process   (OutputFormatNode, ANONYMOUS + output gate)
        -> finalize -> END
```

### Node configuration

| Slot | Class | File | Trust level | Responsibility |
|------|-------|------|-------------|----------------|
| initialize | InitializeNode (default) | framework | — | Schema version, session id, trust level |
| pre_process | InputValidateNode | `src/nodes/input_validate_node.py` | **VERIFIED_EXTERNAL** | Validate the specification and every caller field; normalise the bill of materials |
| main | NPIComplianceGraphNode | `src/graph/graph.py` | — (GraphNode) | Wrap the inner workflow; bridge the caller-data channel; merge results back |
| *(inner)* compliance_checklist | ComplianceCheckListNode | `src/nodes/compliance_checklist_node.py` | ANONYMOUS | Evaluate every component against the regimes in scope; grade severity |
| *(inner)* regulatory_ref | RegulatoryRefNode | `src/nodes/regulatory_ref_node.py` | ANONYMOUS | Attach the regulatory references for those regimes |
| *(inner)* report_generate | ReportGenerateNode | `src/nodes/report_generate_node.py` | ANONYMOUS | Render the checklist report |
| post_process | OutputFormatNode | `src/nodes/output_format_node.py` | ANONYMOUS | Apply the output gate and expose the caller-facing report |
| finalize | FinalizeNode (default) | framework | — | Response metadata, total time |

### Data flow

```
input (specification text)  +  caller data (channel, target_market, product_name, bom)
  |
  v InputValidateNode
validated_input, product_name, bom_content (JSON), bom_entry_count, target_market, channel
  |
  v NPIComplianceGraphNode.extract_input()
  user_input        <- validated_input (free text only)
  caller-data channel <- {channel, target_market, product_name, bom_content}
  |
  v ComplianceCheckListNode
compliance_results (JSON list of findings), compliance_status
  |
  v RegulatoryRefNode
regulatory_references (JSON dict, scoped to the target market)
  |
  v ReportGenerateNode
compliance_report
  |
  v NPIComplianceGraphNode.merge_output()
compliance_report, compliance_results, compliance_status, result, status
  |
  v OutputFormatNode
formatted_output — the three output-gate layers applied
```

### Crossing the outer/inner boundary

The framework's `GraphNode` invokes a subgraph without forwarding the caller-data
channel, so `src/graph/context_bridge.py` bridges it: `extract_input()` stashes the
record in a `ContextVar` immediately before the inner invoke, and the inner graph's
`_extra_initial_state()` reads it back. A `ContextVar` keeps the hand-off correct per
task, so concurrent invocations in one process cannot see each other's data.

The structured record must NOT travel on `user_input`. That field is rewritten by the
framework's input gate, whose personal-name heuristic matches any two consecutive
capitalised words — the shape of an ordinary component label ("Lead Capacitor C12",
"Wireless Charging Module"). A record carried there arrives with its component names
replaced by a mask token, and the report then names the wrong parts.

## State definition

| Field | Type | JSON-encoded | Producer | Consumer |
|-------|------|--------------|----------|----------|
| `validated_input` | `Optional[str]` | — | InputValidateNode | NPIComplianceGraphNode |
| `product_name` | `Optional[str]` | — | InputValidateNode | ReportGenerateNode, OutputFormatNode |
| `target_market` | `Optional[str]` | — | InputValidateNode | ComplianceCheckListNode, RegulatoryRefNode |
| `channel` | `Optional[str]` | — | InputValidateNode | audit attribution |
| `bom_content` | `Optional[str]` | list | InputValidateNode | ComplianceCheckListNode |
| `bom_entry_count` | `Optional[int]` | — | InputValidateNode | ReportGenerateNode |
| `compliance_results` | `Optional[str]` | list | ComplianceCheckListNode | RegulatoryRefNode, ReportGenerateNode, OutputFormatNode |
| `compliance_status` | `Optional[str]` | — | ComplianceCheckListNode | ReportGenerateNode, caller |
| `regulatory_references` | `Optional[str]` | dict | RegulatoryRefNode | ReportGenerateNode |
| `compliance_report` | `Optional[str]` | — | ReportGenerateNode | OutputFormatNode |
| `formatted_output` | `Optional[str]` | — | OutputFormatNode | Caller |

**State constraints (mandatory)**

- Flat `TypedDict` only (primitives and JSON-serialisable values)
- No credentials, tokens or keys in State — checkpoints are persisted
- The invocation context arrives via `config["configurable"]`, never in State
- No Pydantic models, dataclasses or arbitrary Python objects
- List/dict fields are typed `Optional[str]`; the producer calls `json.dumps`, the
  consumer `json.loads`

## Caller contract

Two channels, both validated by the same rules in `InputValidateNode`.

| Field | Channel | Contract | Absent |
|-------|---------|----------|--------|
| `input` | request body | Specification text, <= 4,000 characters, control characters stripped. A JSON object may carry the same `product_name` / `bom` shape. | Rejected — the request needs a specification |
| `channel` | caller data | `^[a-z0-9_]{1,32}$` | `unknown` |
| `target_market` | caller data | One of `jp`, `eu`, `jp_eu` | `compliance.default_target_market` |
| `product_name` | caller data | Component-label alphabet, <= 80 characters | Derived from the specification's first line |
| `bom` | caller data | List of component records, at most `compliance.max_bom_entries` | Specification-only baseline report |
| `bom[].name` | caller data | Component-label alphabet, <= 80 characters | Required |
| `bom[].material` | caller data | Component-label alphabet, <= 120 characters | Empty |
| `bom[].type` | caller data | Component-label alphabet, <= 60 characters | Empty |
| `bom[].concentration_ppm` | caller data | Finite number, 0 <= x <= 1,000,000 | Graded `unverified`, never compliant |

**Component-label alphabet**: `[A-Za-z0-9 .,_()/+%#-]`. It admits every character real
part designations use (`SKF-6205-2RS`, `PCB Substrate (FR4)`, `M3x8 Screw`, `10uF/25V`,
`100-240V`) and excludes quotes, angle brackets, colons, at-signs, braces, backslashes,
control characters and every newline — so a label can carry neither markup nor line
structure into the report. The alphabet is checked on the value as supplied, before
spaces are collapsed: normalising first would silently repair a label carrying a newline
into an acceptable one.

**Numbers fail closed.** Every caller-supplied number goes through a finite and bounded
parser. Booleans, numeric strings, `NaN` and `±Infinity` are all refused. `NaN` matters
in particular: it parses through `float()`, it arrives intact in a raw JSON body, and
every comparison against it is False — so a `NaN` concentration would slide under every
regulatory threshold and be reported as compliant, which is the one decision this agent
exists to make.

**Completion is not the same as answering.** A run that ends with
`AgentStatus.SUCCESS` reports that the request was handled safely to a defined
end, not that the request was carried out. A value the caller can correct (an
out-of-contract parameter, an empty or over-long request) ends this way so the
caller receives the reason and can send a corrected request on the same
conversation; terminating instead would end the calling surface's turn and
surface only an exception type, leaving the reason reachable solely from the
audit trail. The reason travels as `error_code` in State, every later domain
node passes through without doing work once it is set, the structured output
fields are withheld, and the output boundary renders the reason as a static
caller-facing sentence.

Two classes keep terminating, and must not be folded into the above: content
the agent refuses outright (an instruction-override payload — re-sending a
reworded variant is not a correction), and a breach of a contract the caller
cannot influence.

**Rejections name the field, never the value.** Caller data must not round-trip into
error logs.

## Compliance evaluation

Regulation scope follows the target market:

| Market | Regimes evaluated |
|--------|-------------------|
| `jp` | RoHS (and the parallel domestic marking standard), PSE (Category A) |
| `eu` | REACH, RoHS |
| `jp_eu` | REACH, RoHS, PSE (Category A) |

Every accepted component is evaluated against every regime in scope — a submission is
never reduced to its first row. Each finding is graded against the limit that applies:

| Severity | Meaning |
|----------|---------|
| `blocking` | Declared concentration is at or above the limit, or a mandatory certification is missing — the gate is held |
| `monitor` | A restricted substance is present below the limit — a duty exists but the gate is not held |
| `unverified` | A restricted substance is present and no concentration was declared — never treated as compliant |

Limits: 1,000 ppm for the candidate-list declaration duty and for the general
restriction limit; 100 ppm for cadmium; certification carries no quantity threshold.

The scan runs under the wall-clock budget declared as `timeout_s` and fails CLOSED when
it is exceeded: a partially evaluated bill of materials must never be reported as a
clean pass.

## Output boundary

`OutputFormatNode` applies three independent layers, in this order, and then repeats the
first:

1. **Credential scan** — an API key, token or credential assignment anywhere in the
   report withholds the response entirely (sanitised stub, error status).
2. **Caller-label containment** — the only caller free text this report renders is the
   product name and each finding's item name. Each is re-validated at the boundary
   against the same alphabet the input contract enforces, and a label that fails is
   withheld. The check is deliberately independent of the input gate: if any future path
   reached the renderer without validation, the boundary still holds.
3. **Concentration-disclosure schema** — measured concentrations are reported as
   regulatory bands only (`below 100 ppm`, `at or above 100 ppm`, `at or above
   1,000 ppm`, `not declared`). A supplier-declared figure is confidential product data;
   any concentration token in the report that is not one of the approved regime
   thresholds is replaced by its band, with an audit event per replacement.
4. **Credential scan, repeated** — nothing a replacement produced may escape the scan.

**Layer order is deliberate.** The credential scan is a pattern scan, so it runs before
any token is rewritten: a rewrite landing inside a credential-shaped string would
destroy the pattern the scan looks for. It is then repeated afterwards.

**No monetary precision grid applies to this template.** It renders no monetary
aggregates — the report carries regulation names, thresholds, bands and component
labels. The stated numeric invariant it does enforce is the concentration-disclosure
schema above. The disclosure grammar is anchored on the unit (`ppm`, `% w/w`) and
guarded on both sides against identifier characters, so manufacturing identifiers
(`SKF-6205-2RS`, `EAB64785603`, `MFG-2026-001`) are rendered byte-identical. The
figure-to-unit delimiter is horizontal whitespace only, never `\s*`, so a figure ending
one block cannot bind to a unit word opening the next and rewrite document structure.

## Security design

### Trust levels
- `InputValidateNode` (pre_process): `VERIFIED_EXTERNAL` — the external-facing gate
- All inner domain nodes: `ANONYMOUS` — the inner graph inherits the outer invocation
  context unchanged, and `INTERNAL` would deny a genuine external caller
- `OutputFormatNode` (post_process): `ANONYMOUS` — the external gate is on pre_process

### Refusal is owned by the template
Instruction-override payloads are refused in `InputValidateNode` before any structured
parsing, and the same screen runs on every caller label — a label is short and
alphabet-restricted, but an override instruction fits inside both bounds and labels are
rendered into the report, so screening only the specification would leave the structured
channel as an open path to the same content. The platform input gate refuses high-confidence payloads too, but this template
does not depend on that: where the gate is absent or configured off, an unchecked
payload would otherwise reach the compliance path and return a report. The refusal
pattern is deliberately narrow — each alternative requires the imperative override
shape, so ordinary manufacturing text is unaffected ("Ignore Fault Relay", "Override
Protection Switch", "Insert Molded Contact" are all accepted). Both directions are
pinned in `tests/unit/test_caller_data_and_output_gate.py`.

Contact identifiers (email addresses, phone numbers, card numbers) are refused in
component labels. The personal-name heuristics are deliberately not screened on: they
match any two capitalised words, which is what a component label is.

### Audit logging
Every side-effect node emits `emit_trace_event(event_name, payload, state)`:

| Node | Events |
|------|--------|
| InputValidateNode | `input_validated`, `input_validation_failed` |
| ComplianceCheckListNode | `compliance_check_complete`, `compliance_scan_incomplete` |
| RegulatoryRefNode | `regulatory_refs_retrieved` |
| ReportGenerateNode | `report_generated` |
| OutputFormatNode | `output_finalized`, `output_credential_violation`, `output_label_withheld`, `output_disclosure_reduction` |

## Configuration

`config/agent.yaml` is the flat registration manifest: identity, entry-point class,
required trust level, and the compile-time `requires` block. This template declares no
secrets and no extras — it constructs no external client and makes no model call, so
declaring either would fail the compile-time provisioning check for something it never
uses.

`config/config.yaml` holds every runtime parameter. The platform registry loads it and
passes it as `Graph(config=...)`; the standalone server does the same, so a deployed
agent and a registry-loaded agent see identical configuration.

| Key | Consumer |
|-----|----------|
| `max_retry` | The framework's retry routing |
| `timeout_s` | The compliance scan's wall-clock budget |
| `compliance.max_bom_entries` | The entry cap enforced in `InputValidateNode` |
| `compliance.default_target_market` | The market scope when the caller selects none |

`register_nodes()` validates these once — type, finiteness, range — and hands them to
the nodes that consume them. An invalid value is not forwarded and the node keeps its
module default, so a malformed configuration file can neither crash graph construction
nor disable the guard it configures.

## Framework utilisation

- [x] `emit_trace_event()` — audit logging in every side-effect node
- [x] Module-level `_security_gate_output()` helper in `OutputFormatNode`
- [x] `AgentBaseGraph` + `BaseGraph` (the two-layer nested shape)
- [x] `GraphNode` (`NPIComplianceGraphNode` in the `main` slot)
- [x] `FunctionNode` for every domain node
- [x] `detect_pii` for the contact-identifier screen on component labels

## Import isolation

- [x] The template does not import the platform SDK
- [x] Import targets are `framework.*`, `shared.*` and this package only
- [x] No imports from other templates

## Class-name alignment

| Source | Value |
|--------|-------|
| `src/graph/graph.py` class | `NPIComplianceChecklistAgent` |
| `config/agent.yaml` `class:` | `src.graph.graph.NPIComplianceChecklistAgent` |
| `src/api/server.py` import | `NPIComplianceChecklistAgent` |

## Design decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Base classes | `AgentBaseGraph` (outer) + `BaseGraph` (inner) | The outer graph supplies the fixed backbone; the inner graph needs a custom topology |
| Inner topology | Linear | The evaluation must precede reference lookup, which must precede rendering |
| Inner node trust | `ANONYMOUS` | The subgraph inherits the outer invocation context; `INTERNAL` would deny a genuine external caller |
| Serialisation | `Optional[str]` for the list/dict fields | All three carry compound data that must survive checkpoint serialisation |
| Caller-data channel for the record | Structured values bridged, free text on `user_input` | The framework's input gate rewrites `user_input`, masking ordinary component labels |
| Config route | Constructor injection from `register_nodes()` | The node contract is `execute(self, state)` — a node never receives a per-invocation config |
| Undeclared concentration | `unverified`, not compliant | The caller has not shown the article is within the limit |
| Output gate placement | Module-level helper called from `execute()` | The framework's own gate methods are final; a node-level override raises at class definition |
| Numeric output invariant | Concentration-disclosure bands | The template renders no monetary aggregates; supplier-declared concentrations are the confidential figures it must not reproduce |

## Entry Points

The agent is reachable through three entry points, all of which build the graph
from the same `config/config.yaml`:

| Entry point | Construction | Notes |
|---|---|---|
| Platform registry | `Graph(config=...)` by the registry | Reads `config/config.yaml` itself |
| Standalone HTTP (`src/api/server.py`) | Loads `config/config.yaml`, passes `Graph(config=...)` | Caller-auth boundary; see Security Design |
| Marketplace (`cli.py`) | `run_agent_marketplace(...)` is handed the graph class and the resolved config | The runner constructs the graph itself, so `cli.py` resolves `config/config.yaml` with `load_agent_config()` and passes it in; `extend_config` is the seam for deployment-specific overrides |

`cli.py` sits at the repository root because the deployment image starts it as
`CMD ["python", "cli.py"]`. It adds no business logic: graph construction,
lifecycle, secret provisioning and the invocation loop belong to
`run_agent_marketplace()`.

## Caller-Facing Events

Nodes report progress and rejection reasons to the caller as non-terminal
events, so a caller watching a run sees the pipeline advance instead of a
silent wait, and learns what to change when a request is refused.

- **Progress** — each node reports its phase at the top of `execute()`.
- **Rejection reason** — a node that returns `status: error` sends the reason
  first. It has to happen there: once the run carries an error status the
  framework skips `execute()` on every later node, so no downstream node could
  send it. Wording separates what the caller can fix (missing question,
  oversized request, malformed value) from what they cannot (retrieval or
  output failures), so a caller is not invited into a pointless retry.

Both are best-effort: the emitter is resolved lazily and failures are
swallowed, because reporting must never change the outcome of a run. Messages
are static phase and reason labels — no request value, record value or
internal identifier is ever included, since these events leave the process and
are not covered by the S-3 output gate. Terminal delivery (success/failure)
belongs to the platform runner alone.
