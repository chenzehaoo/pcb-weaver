# Authorized Four-Layer Repair

Follow-up: the three remaining connections recorded here have subsequently been
repaired by [reference-guided local multi-net ECO](MULTINET-REPAIR-VALIDATION.md).
This historical short-neckdown/fanout result remains unchanged; the newer passed
revision is a different operation with an explicit compatible reference dependency.

## Scope

The user's confirmation on 2026-09-12 authorizes bounded short neckdown and
local fanout adjustment of `completion-system-review`. Manufacturing is not
authorized. This continues an existing checkpoint; it is not a fresh successful
whole-board completion from an unrouted input.

Declared track and clearance minima, drill sizes, footprints, electrical identity,
fixed placements, original revisions, and saved toolchain settings remain protected.
Every adopted child must improve native connectivity without new DRC/ERC findings
or regressions on other nets. A candidate or a passing software test is not native
board acceptance.

## Implemented Changes

- Physical-pad refinement after failed coarse searches uses the existing GEOS
  continuous-edge checker and 2.5-million-node ceiling. Refinement stays inside
  the original region. Both time and attempt limits remain enforced.
- Fanout candidates are derived from actual pad envelopes. Fixed-pad collisions
  and moves beyond the existing 0.35 mm per-axis bound are screened out; bounded
  candidate ranking uses clearance deficits. At most eight joint candidates are
  attempted. A legal adjustment that cannot connect the target no longer stops
  the search before later candidates are evaluated.
- Long incident tracks retain a far fragment while up to 2 mm of local trace
  participates in joint adjustment. The immutable original track envelope is
  included in the audited merge scope, still subject to the authorized area cap.
- Only the explicitly authorized fanout strategy can consider compact new vias.
  Sizes come from hash-authenticated project diameter, annulus and drill limits;
  owned per-net minima are preserved. Missing limits, custom rule files or stale
  evidence disable this option. Existing via dimensions and project rules are
  not rewritten. Native DRC remains the adoption gate.

## Real MCP Runs

| Evidence | Job | Native missing before / after | Retained revision | Outcome |
|---|---|---|---|---|
| `validation/authorized-system-repair.json` | `job-f598131737e54606` | 5 / 3 | `r-872a1728c20f4f93` | Partial improvement; budget exhausted |
| `validation/authorized-system-repair-2.json` | `job-99aaa1750a26418a` | 3 / 3 | `r-872a1728c20f4f93` | No acceptable further proposal |
| `validation/authorized-system-repair-3.json` | `job-3b7f84d1ff5e4917` | 3 / 3 | `r-872a1728c20f4f93` | Improved solver exhausted 12 attempts in 823.250 s; no further adoption |

The first run accepted two short-neckdown connections, with narrow lengths
0.582843 mm and 3.072792 mm. Each was below the existing 4 mm per-proposal cap.
The second run allowed up to 2500 mm2 instead of 900 mm2, enabling the longer
AN5 search without altering electrical or fabrication rules.

The accepted continuation remains **incomplete**, with GND, +3.3V and AN5
unconnected. Eight ranked GND adjustments could satisfy local geometry but did
not connect the target. Power and AN5 candidates also failed bounded geometry or
routing checks. This is a limit of the attempted search, not proof that the board
is physically impossible to route. Diagnostic candidates are not native passes.

Independent final native verification is in `validation/authorized-system-final.json`:
3 missing connections, 3 DRC errors (the missing items), 53 DRC warnings,
0 ERC errors and 16 ERC warnings. It reports **blocked**, not passed. Electrical
identity, placement, constraint hash, project rule content and all original via
dimensions match the initial review checkpoint. The historical fully passed
`system-clearance-acceptance/r-1bbf979cb3cc4206` board hash is unchanged.

The next substantive routing work is multi-net local rip-up/re-route and improved
escape-candidate coverage, not a smaller drill, lower clearance, forced pass, or
another repetition of identical attempts. The scope and production claims must
not expand automatically just because bounded repair failed.

## Supporting Checks

Final software regression: **1515 passed, 26 skipped**, two dependency deprecation
warnings, in `validation/authorized-repair-regression-final.xml`. This covers the
final production modules, including candidate continuation, compact-via bounds,
local split geometry and audited scope limits. Skips are not successes.

`validation/authorized-repair-ui-preflight/results.json` checks the live desktop
checkpoint at 1440, 390 and 320 px. All mutations are intercepted. It verifies
the canvas, header geometry, bounded forms, default-off authorizations and stale
revision rejection. These UI tests do not claim a completed repair job.

`validation/authorized-repair-ui-final/results.json` additionally opens the real
finished MCP job on all three viewports and asserts `blocked`, the exact retained
revision and three remaining connections. It does not substitute fixture data
for the real job. Console errors and overflow checks are clean.

Diagnostic proposal directories retain failed refinements and local candidates.
They are not adopted boards and cannot be counted as native positive results.
