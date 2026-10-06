# Connection repair: native gates passed

Date: 2026-09-08. Project: `system-clearance-acceptance`.

**Use revision `r-1bbf979cb3cc4206`.** It passes the existing native engineering
gates without ignoring findings, weakening persisted rules, or overwriting an
older revision. This is engineering-check acceptance, not manufacturing signoff.

Workbench: http://127.0.0.1:8765/?project=system-clearance-acceptance&revision=r-1bbf979cb3cc4206

## Verified result

| Check | Original r-b8fb758edb12488f | Final r-1bbf979cb3cc4206 |
| --- | --- | --- |
| Unconnected items | 8 | 0 |
| Non-connectivity DRC errors | 0 | 0 |
| DRC warnings | 53 | 53 |
| ERC errors / warnings | 0 / 16 | 0 / 16 |
| Schematic-to-board connectivity | Passed | Passed |
| Placement and width constraints | Passed | Passed |
| Persisted track minima | Passed | Passed |
| Native engineering gate | Blocked | Passed |

The original missing connections were /AN5 (1), GND (4), and +3.3V (3).
All are now connected, /AN6 remains connected, and the non-regression comparison
also checks all other nets. No new ERC findings or DRC findings were accepted.

Original board SHA-256:
`1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064`

Final board SHA-256:
`3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5`

## Changes

- Removed the obsolete /AN6 branch `afffd855-085c-48df-9a9a-b5979b965856`
  following the user's continued repair authorization. This removed the extra
  dangling-track warning in the earlier joint-repair candidate.
- Corrected overinflated pad-flat and straight-copper clearance envelopes in
  opt-in continuous-edge routing. The default conservative mode remains intact.
- Added pad-aligned local refinement and positive copper-overlap terminal
  landings, plus whole-island contact regions for width restoration.
- Added bounded 0.2 mm escape/bottleneck sections with a maximum aggregate
  narrow length of 4 mm per proposal. Other new power copper is restored to
  the native 0.4 mm preferred width wherever it fits. Existing traces are not
  narrowed. The persisted 0.2 mm minimum and 0.15 mm clearance are unchanged.
- Adjusted /XTAL and local QSPI fanout geometry with fixed widths, drills and
  remote endpoints. Long affected segments are split so only the nearby part
  can move. The last joint adjustment includes /QSPI_CS1, /QSPI_CS2 and /QSPI_CS3.
- Fixed canonical-versus-stored segment endpoint direction during joint writeback;
  regression tests cover both directions and long-segment splitting. The earlier
  failing candidate was rejected by native gates and was not used as a parent.
- Added one 0.5 mm diameter / 0.4 mm drill via for the last U102 power escape.
  The existing project permits 0.5 mm vias and 0.05 mm annular width. No physical
  rule was lowered. Its inner-layer clearance is checked, not just its surface.

For the last connection, the new route is approximately 3.863 mm long. Only
0.173333 mm retains 0.2 mm width; the rest is restored to 0.4 mm. Earlier accepted
new power/ground proposals retain 0.774264, 2.366704, 1.711010 and 2.998528 mm
of bounded narrow sections respectively. These are separate proposal budgets,
not a claim that the entire board contains less than 4 mm of narrow copper.

The final merge adds 176 copper records and replaces 14 selected records relative
to its immediate parent. It proves preservation of all 3420 retained copper AST
records and all noncopper AST records. Changes are scoped, hash-bound, and stored
in an immutable child revision. All rejected attempts remain available for audit.

## Validation and evidence

- Full source suite: **1424 passed, 26 skipped**, two dependency deprecation
  warnings. Skipped tests are not represented as passed.
- Desktop and mobile browser acceptance: real verification data, populated
  component/net/routing tables, no page overflow, no JavaScript errors, and
  nonblank board-canvas pixel checks. Browser tests enqueue no work.
- The configured desktop MCP performs an independent native re-verification,
  checks zero remaining repair proposals, and reads every inventory page.
- Code/tool/test files are hash-compared after deployment. Desktop `data`,
  `toolchain.unified.json`, and `.mcp.json` are not overwritten by source copies.

Evidence files:

- `docs/validation/connection-finish.json`: native before/after and merge proof.
- `docs/validation/finished-mcp.json`: independent MCP verification and pagination.
- `docs/validation/finished-ui/results.json`: desktop/mobile checks and counts.
- `docs/validation/finished-ui/desktop-passed.png` and `mobile-passed.png`.

The final attempt is also retained under the desktop project's
`data/projects/system-clearance-acceptance/revisions/r-5cd3169480f74544/repairs/finish-2fc6a6f6629d/`.

## Acceptance boundary

The existing 53 DRC warnings and 16 ERC warnings remain visible. Native gate
acceptance does not establish current capacity, signal/power integrity, timing,
thermal performance, EMC compliance, fabrication yield, or hardware operation.
The short minimum-width power escapes and minimum-annulus via especially require
electrical and fabricator review before production. No manufacturing release was
created or authorized, and no universal promise of never blocking future boards
is made. Future edits require fresh native checks.

The new routing and fanout tools are experimental acceptance tooling. The
production Freerouting/MCP repair backend was not silently replaced with them.
