# Altium Constraint Evidence Phase 2

## Status

Read-only capture and MCP evidence access work. Complete constraint mapping,
full analysis coverage and automatic routing acceptance are NOT complete.
No manufacturing release is authorized. The accepted KiCad desktop deployment
and original user PCB were not changed.

## Actual Captures

Numeric run `altium-native/ccaa1e480e1446918ad46ee9904dc6be` captured forty
rule identities and numeric fields for six instances: two clearances, width,
routing layers, via style and differential-pair gaps/uncoupled length.
Thirty-four instances still lack numeric fields. The 1..32 index domain is not
a physical 32-layer board. Scope evaluation and clearance matrices remain open.

Geometry run `altium-native/233b217e8c3a4170b748045f9f586dae` captured electrical
layers `[1,2,3,32]`, two hundred pads, and four polygon definitions with sixty
segments, including twelve arcs. Pad identities match the earlier inventory.
This does not capture complete poured copper, custom pad contours, cutouts,
selective keepouts, board outline or physical dielectric stackup.

DSN `altium-native/9dcb104cde14458ba7425ff6f306aa0e` was structurally parsed:
four layers, twenty-eight placements, twenty-seven nets, 198 component pad names,
and 111 assigned pin-net matches. Standalone pads MH1/MH2 remain unmapped.
Five DSN polygon forms are not assumed to equal the four native polygons.
Units, flattened footprint orientation, shapes and full rule semantics still
need equivalence checks. Existing wiring is original example routing.

## Expanded Native DRC

Run `altium-native/8fbedf606b454e1698f9e891b96135f6` used a fresh copy.
The native UI's `batch-before.json`, `batch-effective.json` and
`report-options-effective.json` record all 55 displayed Batch categories enabled,
with Online settings unchanged. The stop threshold increased from 30 to 100000
and PCB health reporting was enabled. Settings were observed in memory, not
saved into the PCB snapshot.

The report has 25 rule rows, 158 violations, zero warnings and zero reported
PCB health issues. Waivers remain unknown. Altium emitted "Source schematic
documents not available. Signal Integrity cannot continue." Evidence screenshot:
`validation/altium-drc-setup-error.png`. The error was recorded and acknowledged.
The script returned `native_return=False`; completed execution is not a board pass.

| Finding | Count |
| --- | ---: |
| Minimum solder mask sliver | 92 |
| Fabrication testpoint usage | 27 |
| Assembly testpoint usage | 27 |
| Silk to solder mask | 4 |
| Component height | 3 |
| Hole size | 2 |
| Differential-pair uncoupled length | 1 |
| Silk to silk | 1 |
| Net antennae | 1 |

The previous six-row/zero-violation report had narrower coverage. Native rule
instances include generation/routing directives, so subtracting report rows
from forty does not yield an exact missed-check count. Batch enablement,
rule-set applicability and successful analysis execution are separate facts.

## MCP Validation

The isolated bridge exposes `altium_wifi_constraints_snapshot`,
`altium_wifi_geometry_snapshot` (paginated), `altium_wifi_dsn_audit` and
`altium_wifi_drc_coverage_snapshot`. These read pinned hash-checked evidence,
not live CAD state. No default MCP configuration or desktop web deployment changed.
Real SDK stdio acceptance passed, including all 200 pads across four pages:
`validation/altium-constraint-phase-mcp.json`.

Combined focused regression: **300 passed**, recorded in
`validation/altium-constraint-phase-tests.xml`. The original user PCB retained
SHA-256 `2658b6ffa4eb826c577379578a7fc303bbe7f96170ce079bc88fe1ac74b5409a`.
No pending native job remained at the final check. These software results do
not override the native board violations or incomplete conversion coverage.

## Remaining Work

1. Capture remaining geometry and rule parameters, then verify per-object
   transformation into route format, including MH1/MH2 and polygon semantics.
2. Bind the project and schematics for analyses that require them; establish
   rule-set applicability and waiver coverage.
3. Assess the 158 findings against design intent and manufacturing requirements.
   Do not disable rules or silently relax limits to obtain a pass.
4. Qualify unrouted-copy -> router -> native writeback -> native recheck.
   This phase did not run a router or alter existing routing.

References: [PCB object API](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-design-objects-interfaces-reference),
[script examples](https://www.altium.com/documentation/altium-designer/scripting/examples-reference),
[DRC setup](https://www.altium.com/documentation/altium-designer/pcb/drc/setting-up-running),
[routing rule applicability](https://www.altium.com/documentation/altium-designer/pcb/design-rule-types/routing).
