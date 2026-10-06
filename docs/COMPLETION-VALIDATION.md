# Unrouted Completion Validation

Follow-up: [reference-guided multi-net repair](MULTINET-REPAIR-VALIDATION.md) now
repairs the retained four-layer checkpoint from three missing connections to zero.
It requires a compatible passed historical board and is a separate local ECO
acceptance, not evidence that autonomous four-layer completion without a reference
has passed. Historical results below are preserved.

## Scope and Evidence

This increment adds a persisted `complete` operation and the MCP tool
`submit_pcb_completion(project, revision, options=None)`. The local workbench uses
the same queue. It is a KiCad/Freerouting engineering workflow, not a new globally
optimal router or industrial certification. No manufacturing order or release is
authorized by completion.

The input must contain an existing schematic, footprints, electrical nets and
supported geometry, with no tracks or vias. Existing placement is the starting
point. This is not synthesis from a text circuit description or placement from a
random pile of components.

Execution: immutable import -> fresh native input checks -> feasible distinct
layouts -> child revision -> DSN -> Freerouting -> SES -> native DRC/ERC,
schematic parity, geometry and minima -> bounded additive connection repair ->
verified checkpoint and report. Failed layouts are not adopted. An incomplete
retained checkpoint remains blocked. Duplicate placements are not retried.

Candidate diversification has an explicit 0-2 mm spread parameter, default 0.5 mm.
Variants move eligible small components near larger devices, re-legalize all
constraints, preserve fixed parts and angles, and remain within twice the spread
of the primary candidate. This is a bounded heuristic, not congestion proof.
Whole-board completion uses an isolated profile with automatic neckdown disabled;
ordinary routing and the saved desktop toolchain configuration are unchanged.
This is the default `routing_policy: strict`. The explicit `normalize_widths`
policy uses the configured router and the existing width-only SES normalizer,
raising narrow paths to declared minima before native import. Up to six bounded
F.Cu clearance cleanup attempts may follow, with automatically derived scopes
up to 100 mm2 and at most 0.08 mm joint movement per axis. It does not enable
optional connection-repair neckdown/fanout permissions or lower any rule.

## Frozen Inputs

The two-layer circuits come from the official KiCad 9.0.0 demos at commit
`286b0611feca00727bf70bfa184ec2c28a745dc3`, under CC-BY-SA-4.0:
[KiCad source mirror](https://github.com/KiCad/kicad-source-mirror/tree/9.0.0/demos).
The original license and `PROVENANCE.json` accompany each derived fixture.

| Fixture | Circuit | Components / Nets / Layers | Preparation |
|---|---|---|---|
| `examples/completion-cpld-v4` | `test_xil_95108/carte_test` CPLD test board | 42 / 100 / 2 | Removed copper and zones, rectangularized board outline, moved copper graphics to documentation, synchronized same-footprint library aliases, explicitly allowed connector body overhang |
| `examples/completion-programmer-v5` | `flat_hierarchy` PIC programmer | 64 / 111 / 2 | Same documented mechanical/copper derivative process; explicit mounting-head body overhang permission |
| `examples/completion-system-v2` | MCU/CPLD system controller | 160 / 278 / 4 | Removed all 3343 tracks and 253 vias from the previously reviewed placement checkpoint; preserved physical constraints and geometry |

The first two inputs are mechanical benchmark derivatives, not unmodified
production-ready boards. Footprint geometry, physical pads, nets, values, poses
and sides are preserved by preparation assertions. No manual repair coordinates
are supplied to completion. Setup and constraint review are human engineering
inputs; the runtime intervention count does not include that preparation.

Explicit body overhang uses the envelope of every physical pad, including NPTH,
for the board-edge placement rule. It does not waive copper, drill, body collision
or mechanical signoff requirements. Netlist parity omits an electrical connection
only for an explicit singleton no-connect, unnumbered non-plated mechanical pad;
numbered, plated, connected, shared-net or mismatched components still fail.

## Native Results

Intermediate positive runs, retained separately from later deployed MCP runs:

| Case | Native missing connections | DRC errors / warnings | ERC errors / warnings | Tracks / Vias | Length mm | Moved parts / Total mm | Seconds |
|---|---|---|---|---|---|---|---|
| CPLD `r-f46d17d8bba748b9` | 177 -> 0 | 0 / 8 | 0 / 0 | 561 / 5 | 2880.834 | 2 / 0.035 | 177.281 |
| Programmer `r-cc0f79631023432f` | 127 -> 0 | 0 / 2 | 0 / 0 | 392 / 0 | 1994.431 | 8 / 0.611 | 93.672 |

Evidence: `validation/completion-cpld-v4/result.json` and
`validation/completion-programmer-v5/result.json`, with full isolated native
workspaces beside each report. CPLD used bounded additive repair after routing;
the programmer passed immediately after routing. These early runs predate the
isolated no-neckdown completion policy; deployed final-version runs are recorded
separately and must be used for current-version acceptance.

Real desktop MCP evidence records tool schema, actual import, queued submission,
disconnect, reconnect and final native-verified revision. UI requests in browser
contract tests are intercepted to avoid creating extra jobs; the displayed
completed job is real, not mocked.

## Current Acceptance Matrix

**The whole requested phase is not yet fully accepted.** Two independent two-layer
circuits pass. The four-layer positive stress case has not passed; it is not
reclassified as an expected-negative success to make the matrix green.

| Deployed MCP run | Final revision | Native missing / DRC errors / ERC errors | Time | Result |
|---|---|---|---|---|
| `completion-cpld-final`, default strict | `r-7b042ddcd7874ebc` | 0 / 0 / 0 | 197.797 s | Passed |
| `completion-programmer-mcp`, default strict | `r-31a83233e0334a39` | 0 / 0 / 0 | 99.203 s | Passed |
| `completion-cpld-normalized`, explicit normalized-width policy | `r-6ec3a38ce7f24924` | 0 / 0 / 0 | 190.735 s | Passed |

These are three MCP runs of two independent circuits, not three different boards.
CPLD retains 8 DRC warnings, programmer 2; both have zero ERC warnings. CPLD final
length is 2880.872 mm with 561 tracks and 5 vias; programmer length is 1994.431 mm
with 392 tracks and zero vias. Movement remains 2 parts / 0.035 mm total for CPLD
and 8 parts / 0.611 mm for programmer. Normalized CPLD needed zero width changes;
it validates pipeline integration, not the difficult four-layer width-repair case.

Evidence: `validation/completion-cpld-final-mcp.json`,
`validation/completion-programmer-mcp.json`, `validation/completion-normalized-mcp.json`.
Each records real import, MCP submitter disconnection, reconnection and native
checks against an exact immutable output revision.

Four-layer evidence remains separate:

- `validation/completion-system-v4/result.json`: two distinct strict-width
  layouts each reached the 1200-second native routing timeout; neither was adopted.
- `validation/completion-system-width-probe.json`: diagnostic reuse of the
  earlier completed SES, raised to declared minima, produced 8 clearance errors
  and 7 missing connections. This is not a passed completion job.
- `validation/completion-system-clearance-probe.json` and `-v2.json`: native
  clearance errors reduced 8 -> 2 -> 0, without worsening connectivity or warnings.
  Scope coordinates came from authenticated findings and full segment geometry,
  not manually supplied board-specific repair coordinates.
- `validation/completion-system-connections-probe.json`: 22 bounded additive
  attempts reduced missing connections 7 -> 5 in 305.031 seconds, then stopped
  because the permitted strategies found no more acceptable proposal.
- `completion-system-review/r-25f94cd90ad74aab` is the separately published desktop
  review checkpoint, freshly checked after import. It has 5 native missing items,
  no other DRC errors, zero ERC errors, 53 DRC warnings and 16 ERC warnings.
  It remains blocked and is not a substitute for full four-layer completion acceptance.

The unresolved targets include power-pin escapes that fail at preferred width.
The user confirmed bounded short-neckdown/local-fanout repair on 2026-09-12.
The authorized continuation and its separate native results are recorded in
[Authorized Four-Layer Repair](AUTHORIZED-REPAIR-VALIDATION.md). Default options
remain off for new unapproved requests. Authorization does not guarantee final
native acceptance or authorize manufacturing.

Software regression: **1501 passed, 26 skipped**, two dependency deprecation
warnings, recorded in `validation/completion-regression-latest.xml`. Skips are
not successes. Tests include layout bounds, strict options, native-evidence gates,
budget/cancellation, width-only normalization, fixed-copper preservation, invalid
mechanical netlist cases, and old request compatibility. Fault-injection tests
are distinct from actual native board results.

`validation/completion-programmer-ui-latest/results.json` covers 1440, 390 and
320 px: form constraints, opt-ins off, stale-target rejection, actual completed
job display, unobscured header, canvas pixels and no console errors.
`validation/completion-prior-ui/results.json` rechecks the original passed complex
board and historical-version switching without mutating any API state.

## Failure History

- Early imported CPLD/library aliases and programmer mechanical no-connect parity
  exposed actual import semantics. Fixes are covered by positive and negative tests.
- Connector shells and screw-head envelopes exposed mechanical overhang assumptions;
  fixture permissions are explicit and do not move actual pads outside the board.
- Raw system-controller placement contained conflicting fixed-component constraints.
  It was not silently altered. The separate four-layer fixture uses the previously
  reviewed placement, clearly recorded in provenance.
- The first fully unrouted four-layer attempt timed out at 300 seconds.
- A longer four-layer attempt completed routing but SES adoption correctly rejected
  a 0.15 mm segment below the declared 0.20 mm floor. This motivated the isolated
  no-neckdown completion profile; the floor itself was not lowered.
- Narrow-screen UI initially clipped the second toolbar row; a content-sized,
  explicit two-row header replaces the wrapping flex layout.
- The clearance optimizer included constant, non-movable geometry in constraints
  with extra solver safety margin, making a variable problem infeasible. It now
  optimizes only variable-dependent clearance inequalities. Constant copper is
  preserved byte-for-byte and remains subject to the complete native DRC gate.

## Boundaries

Scheduling budgets stop new stages; active native operations retain their own
process limits. Cancellation is cooperative at stage boundaries. Candidate proposal
workers have their separate hard timeout. Only authenticated native evidence can
qualify an accepted revision; warnings remain visible. Passing geometry and
connectivity does not validate SI/PI, high-speed buses, RF, EMC, thermal behavior,
component procurement, mechanical assembly or fabricated-board functionality.

Desktop `system-clearance-acceptance/r-1bbf979cb3cc4206` and its original baseline
must remain byte-identical. The desktop MCP registration and unified toolchain
configuration must also remain unchanged. Deployment hashes are checked separately.
