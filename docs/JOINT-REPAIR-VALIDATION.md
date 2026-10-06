# Joint /AN5 + /AN6 repair

> Historical result, superseded on 2026-09-08 by
> [Connection repair: native gates passed](CONNECTION-FINISH-VALIDATION.md).
> The subsequently authorized dangling-branch removal is complete, and final
> revision `r-1bbf979cb3cc4206` has zero missing connections. The text below
> records the earlier candidate, not the current state.

## Actual outcome

**A real, natively verified candidate now connects both selected nets. It is not
accepted for release or manufacturing.** The original board is unchanged.

| Check | Original | Candidate |
| --- | --- | --- |
| Revision | r-b8fb758edb12488f | r-9c49830060a44003 |
| Native unconnected items | 8 | 7 |
| /AN5 missing connections | 1 | 0 |
| /AN6 missing connections | 0 | 0 |
| Non-connectivity DRC errors | 0 | 0 |
| DRC warnings | 53 | 54 |
| ERC errors / warnings | 0 / 16 | 0 / 16 |
| Schematic connectivity parity | Passed | Passed |
| Placement/width constraints and persisted minima | Passed | Passed |

The remaining seven missing connections are GND (4) and +3.3V (3).
The added warning is `track_dangling` on retained /AN6 B.Cu segment
`afffd855-085c-48df-9a9a-b5979b965856`, from (130.9358, 110.9646) to
(130.9358, 128.8483), length 17.8837 mm. Reconnection through another part of the
same copper island has made this old branch redundant. This is not an error to
hide by disabling DRC or adding an unnecessary electrical loop.

Additional approval to remove this specific branch has been requested. It has
**not** been removed. The new-warning comparison gate correctly rejects this
candidate until that issue is resolved. Even resolving it would not close the
remaining seven power/ground connections or constitute manufacturing signoff.

## Preserved scope

Only these four originally reviewed /AN6 copper items were removed in the
candidate; the original revision retains them:

- `05a8dab1-cf6e-4769-8870-18076025e233`
- `bd3c4cc8-3b8f-4e99-8b63-fbad7a234394`
- `565adeac-cac8-4d6c-b016-1bd6d82b1a21`
- `5d1c7860-af7c-4271-a99e-9e0b2ddf2ef6`

The merge proves every other copper AST and every noncopper AST item unchanged.
No components, pads, net assignments, electrical rules or other nets were edited.
New selected-net copper is contained by [127.382, 106.148, 164.544, 142.446] mm.
The final search extends /AN6 beyond the immediate pin escape to its existing
copper island; it is not a replacement confined to a tiny pad rectangle.

Added geometry: /AN5 30 segments and one via, /AN6 224 segments and one via.
New trace widths are 0.2 mm, vias 0.6 mm diameter / 0.4 mm drill, and the native
clearance requirement remains 0.15 mm. Added trace length is approximately
59.337 mm for /AN5 and 30.540 mm for /AN6. No signal-integrity or analog-performance
equivalence is claimed; length, return paths and detailed routing quality still
require engineering review. The experimental grid path is not a tuned industrial
router output.

## Implemented improvements

- Copy-only joint routing with explicit removal UUIDs, a hash-bound scope and
  retained-copper merge before native comparison.
- Correct stationary-via clearance envelopes. Cell-sweep inflation remains only
  for the conservative sampled-edge mode, not stationary vias.
- Optional continuous GEOS edge checks, with chunked queries and SciPy graph
  search, including diagonal edges.
- Local 0.01 mm axis refinement around U102, coarse 0.1 mm sampling elsewhere,
  still below the existing 2.5-million-node ceiling.
- Reserved escape corridors and bounded per-layer routing costs. Preferences do
  not modify physical design rules. Weighted solver cost is reported separately
  from actual geometric trace length.
- Optional whole-island terminals using geometric intersections on shared copper
  layers. A projection crossing on different layers does not join islands.
  Conservative envelopes remain a proposal model, never native connectivity proof.
- A resumable native acceptance runner that rechecks source, candidate and scope
  hashes and reproduces the retained-copper merge before checking a candidate.
- Fixed nested project-lock acquisition in both experimental acceptance scripts:
  native comparison is persisted before report generation, and report generation
  runs after the caller releases its project lock.

These are deployed Python modules and experimental acceptance tooling. The
production Freerouting repair command has not been silently replaced, and the
previously unverified `-inc` isolation mode remains disabled.

## Evidence and reproduction

Original board SHA-256:
`1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064`

Candidate board SHA-256:
`bfc912269f26e276780a93b5323ccf1a8a96cee0ad66b87341f806d8b8b8ed38`

Under the desktop project:

- `data/projects/system-clearance-acceptance/revisions/r-b8fb758edb12488f/repairs/joint-e2360eb648d6/`: exact scope, search, merged board and recovered `result.json`.
- `data/projects/system-clearance-acceptance/revisions/r-9c49830060a44003/verification/v-4de9b7ade791/`: authenticated rerun of native ERC/DRC and rule checks.
- `docs/validation/joint-repair-regression.xml`: software regression evidence.
- `docs/validation/joint-repair-ui/`: desktop/mobile intercepted-write UI checks.

Final software regression: **1,390 passed, 26 environment-dependent skips**, two
dependency deprecation warnings. Desktop and mobile UI checks both passed with
seven actual repair proposals and zero real job submissions. Screenshots were
visually inspected. No claim is made that software test success clears PCB DRC.

The first candidate verification completed, but report generation then encountered
the nested-lock bug. After the fix, the same board and exact merge were audited,
native verification rerun, comparison persisted and report generation completed.
That runtime issue is distinct from the remaining electrical acceptance reasons.

To audit the existing candidate without rerouting:

```powershell
.venv\Scripts\python.exe scripts/accept_joint_repair.py --root . --resume-candidate r-9c49830060a44003
```

To reproduce the search as another isolated candidate:

```powershell
.venv\Scripts\python.exe scripts/accept_joint_repair.py --root . --order an5-first --exact-edges --reserve-an6 --refine --step 0.1 --prefer-inner --whole-island
```

Neither command authorizes manufacturing or suppresses a failing comparison.
