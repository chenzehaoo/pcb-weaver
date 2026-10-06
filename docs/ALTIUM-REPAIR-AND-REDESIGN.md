# Altium Repair and Redesign Decision

## Implemented and Verified

The complete source project and six referenced documents are now copied and
hash-checked together. Native project opening, schematic loading, compilation,
flattened connectivity availability and PCB project membership were verified.
The original project and user PCB were not modified.

One GND connection was repaired on a new copy. The first deletion candidate
`cbdb34a0d550467e9eac33738c4c55c7` caused an unrouted connection and was rejected.
The second `cc96a572ef4e4d83a591117d554f6310` removed the antenna but introduced
a clearance violation and was rejected. Both histories are retained.

Accepted local candidate `962efdb9522e425a8b2b9012b2fd973f` retains 579 tracks;
578 have identical enumerated fields. Only the target GND track's starting Y
and width changed, using the already-defined minimum width. The isolated-board
DRC comparison was 158 -> 157, with only Net Antennae decreasing by one and no
increase in any reported rule category. Other object classes are not fully
proven unchanged; this is not full-board equivalence.

The same repair was subsequently tested in a complete project context:

| Context | Original | Repaired |
| --- | ---: | ---: |
| Isolated PCB | 158 | 157 |
| Compiled complete project | 160 | 159 |

Do not compare counts across these contexts. Complete-project baseline
`altium-project/1d42552e55ef41f896086b3f2a16fa76` and repaired project
`altium-project/246e8c0e8a6c4e5e8ecd3f1404d97077` used the same observed 55 Batch
switches and report options. Only Net Antennae decreased by one. The remaining
two project-context antenna findings are unresolved.

Signal Integrity no longer stopped for unavailable source schematics in the
complete project. The repaired project emitted an SI-warning confirmation;
its screenshot was retained and continuation acknowledged. This is NOT SI
qualification. Zero warnings in the DRC HTML do not negate that separate dialog.

Evidence: `validation/altium-project-repair-comparison.json`,
`validation/altium-local-repair-mcp.json`, and
`validation/altium-project-repair-tests.xml` (**394 passed**).
The new MCP tool `altium_wifi_local_repair_status` reports local evidence only.
Full DRC, automatic routing, SI and manufacturing release remain unapproved.

## Route B Selected for Implementation

### Implemented Baseline (2026-09-26)

Created `validation/altium-redesign/eb8ce77bbff14657b2c8ee7a040fd0b4/`
with `WiFi_Controller_B.PrjPcb`. The active project references only the schematic
and repaired PCB; historical panel, drawing and BOM files remain copied but are
excluded from the active project. Original files and rule values are unchanged.

Native Altium compilation and project membership checks completed. A fresh
native DRC with 55 Batch types enabled reported **159 violations**, not a pass.
The separate Signal Integrity warning was captured in `native-message.png`.
Compilation does not imply ERC, SI, routing or manufacturing acceptance.

`altium_wifi_redesign_status` exposes the pinned revision and optional complete
finding details. Real stdio verification is recorded in
`validation/altium-redesign-mcp.json`; regression results are in
`validation/altium-redesign-tests.xml` (**401 passed**).
This source MCP extension is not a desktop/web deployment update.

This revision establishes a reproducible working baseline only. No new outline,
component placement or routing has been applied in this phase. Manufacturing
process qualification, test access and physical redesign remain outstanding.

The user delegated the route decision and authorized implementation of route B.
Preserve electrical function and existing parts where possible. A new working
revision must not inherit production-release claims from the old board.
No hardware substitution or relayout below has yet been validated.

### A. Preserve miniPCIe Mechanical Requirements

- Keep the current outline, connector, mounting holes and height limits.
- First assess moving the existing U2 and C10 to the top layer. Their recorded
  heights fit the current top limit numerically, but placement and routing must
  be checked. This is a feasibility proposal, not a validated placement.
- If U2 cannot fit on top, investigate STM32F103RBH7 in TFBGA64. The official
  package height is 1.2 mm, but it is NOT footprint-compatible and requires
  pin mapping, fanout and a reviewed fabrication process. It must not be swapped
  automatically. [ST official listing](https://estore.st.com/en/products/microcontrollers-microprocessors/stm32-32-bit-arm-cortex-mcus/stm32-mainstream-mcus/stm32f1-series/stm32f103/stm32f103rb.html),
  [ST datasheet](https://www.st.com/resource/en/datasheet/stm32f103rb.pdf).
- C10's published thickness is 1.60 +/- 0.20 mm. Do not change its height label
  to a typical value to pass the bottom-layer limit.
  [TDK original component](https://product.tdk.com/en/search/capacitor/ceramic/mlcc/info?part_no=C3216X5R1A476M160AB).
- U1 remains a mechanical blocker: its recorded 2.45 mm exceeds the top limit
  of 2.40 mm. No verified thin drop-in replacement has been selected.

### B. Redesign as a General Wi-Fi Controller Board

Keep the existing functional design where possible, but define a new enclosure,
height envelope and placement plan. Explicitly abandon miniPCIe mechanical
compatibility if those requirements change. Electrical connector, power and
host-interface changes need separate approval; they are not implicit in this route.

### Decisions Required in Either Route

1. Approve a manufacturer-backed solder-mask process. Existing fine-pitch U2
   geometry cannot meet the current 0.254 mm sliver requirement simply by moving
   the component. Do not silently merge mask openings or shrink copper/mask.
2. Define fabrication/assembly test access. The 54 findings correspond to 27 nets
   checked for two test roles, not necessarily 54 new physical pads. Existing
   copper must not be marked as testable without access/style verification.
3. Confirm mechanical holes and tolerances before altering MH1/MH2. Do not reduce
   their 2.6 mm diameter solely to satisfy a generic 2.54 mm rule.
4. Rework differential-pair coupling and silkscreen in a new revision, then run
   same-context native checks and preserve the full rule/option history.

Route B is authorized; further A/B confirmation is not needed. Electrical
interface changes and manufacturing process qualifications remain separate
decisions. The revision builder excludes old panel, drafting and BOM documents
from the active project while retaining their baseline copies for traceability.
