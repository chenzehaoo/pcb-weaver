# Bounded clearance repair delivery

## Result

This delivery clears the eight native clearance errors on the recorded
system-controller board. It does **not** complete the board or authorize manufacture.

- Original: `system-controller / r-62cd6db138694725`, preserved unchanged.
- Isolated baseline: `system-clearance-acceptance / r-9eda5224f31044d9`.
- First service candidate: `r-e44898434a0a487b`, clearance errors 8 -> 2.
- Final MCP/worker candidate: `r-b8fb758edb12488f`, clearance errors 2 -> 0.
- Persistent MCP job: `job-8f8b753b38a945ba`, completed after submitter disconnect/reconnect.
- Workbench: <http://127.0.0.1:8765/?project=system-clearance-acceptance&revision=r-b8fb758edb12488f>.

| Native check | Original | Final |
| --- | ---: | ---: |
| Clearance errors | 8 | 0 |
| Unconnected findings | 8 | 8 |
| Total DRC errors, including unconnected | 16 | 8 |
| DRC warnings | 53 | 53 |
| ERC errors | 0 | 0 |
| ERC warnings | 16 | 16 |

Final declared constraints, persisted track minima and schematic connectivity
consistency pass. Physical copper is still incomplete: GND has four unconnected
findings, +3.3V three and /AN5 one. Overall verification correctly remains blocked.
DRC checks here did not refill zones; SI/PI, timing, thermal, EMC, fabrication review
and physical prototype testing are not covered.

## Implementation

Shapely/GEOS computes copper distances and SciPy SLSQP minimizes bounded joint
movement. The solver proposes geometry; KiCad 9.0.9 independently accepts or rejects
it. Eleven existing segments change endpoint coordinates, without changes to widths,
UUIDs, net assignments, pads, vias, placements, schematic files or design rules.
The final board retains 160 components, 278 nets, 2,712 segments, 251 vias and four layers.

The supported operation is deliberately limited: 1-8 named nets, 1-80 unlocked
straight segments, F.Cu, an explicit rectangular ROI and at most 0.08 mm movement
per axis. Fixed attachment points retain their original coordinate precision.
Unsupported geometry and critical-net restrictions remain blocking. This is not
a general push-and-shove router or an automatic closure engine for arbitrary boards.

Fresh revision-bound native reports enforce strict clearance-count reduction,
no new clearance item pairs, no new other DRC/ERC findings, no per-net connection
regression, and passing constraints, schematic consistency and width minima.
Failed proposals retain evidence and do not replace their parent. An `improved`
task outcome is distinct from an electrically passed board.

## Use

The desktop workbench/worker has been updated. The existing MCP configuration and
project data are preserved. Restart an already connected MCP client to discover
`submit_pcb_clearance_repair`. The shipped `skills/pcb-engineering/SKILL.md` documents
the operation; this does not install or reconfigure a third-party host automatically.

For a revision with clearance errors, submit this MCP tool with `project`,
`revision`, `nets` and `region`, then query `get_engineering_job` using its returned
job ID. Use the result revision only when the repair outcome is `improved`.
The normal workbench job list displays the clearance stage; there is no new
automatic ROI-selection UI. The opt-in enterprise machine bridge deliberately
does not gain this additional write permission.

Equivalent explicit local CLI entry:

```powershell
.venv\Scripts\python.exe scripts/run_clearance.py --root C:\path\to\pcb-weaver-system --project system-clearance-acceptance --revision r-e44898434a0a487b --net /inout_user/RTS1 --region 155.8 79.9 157.9 81.5
```

The example repeats the last validated repair from its parent and creates another
candidate. It is not needed to view the delivered final revision.

`toolchain.hard-width.json` adds an explicit configuration with automatic neckdown
disabled. Its isolated four-layer smoke test passed native DRC with zero errors and
zero unconnected findings, four warnings, observed POWER width 0.6 mm and LINK width
0.25 mm. This fixture has only two routed segments: it proves the configuration and
width audit work, not complex-board routability. The existing default configuration
is unchanged; selecting the hard-width profile requires starting the worker with
`--config toolchain.hard-width.json` and using that same configuration for the client.
Do not restart a worker while jobs are active.

## Evidence

- `validation/clearance-mcp-bbb53387.json`: real stdio MCP tools, disconnect/reconnect,
  durable worker stages and final native checks.
- `../data/projects/system-clearance-acceptance/revisions/r-9eda5224f31044d9/repairs/clearance-c0d3060fa053/result.json`:
  first production service step and before/after comparisons.
- `validation/clearance-r-315246dd32364bd7.json`: earlier independent complete
  native acceptance; its final PCB bytes equal the production MCP result.
- `validation/hard-width-7360d5eb/result.json`: no-neckdown four-layer smoke test.
- `validation/clearance-regression.xml`: final full software regression, 1,343 passed,
  26 environment-dependent tests skipped, two dependency deprecation warnings.
- `validation/clearance-final-targeted.xml`: 51 focused final tests, including
  machine-bridge denial, fixed-coordinate precision and regression rejection.
- `validation/clearance-ui/browser-results.json`: desktop/mobile deep links, nonblank
  canvas pixels, zoom/layer controls, finding location and zero browser errors.
- `validation/clearance-ui/desktop.png` and `mobile.png`: visually inspected captures.

Original PCB SHA-256:
`63ba619ba8ee337770f162a6bc477768890f7542ca0bfe6b88b6a8440717ad22`.
Final PCB SHA-256:
`1c84db8a3ecb559c33c0b01bd62f4ba06f7afa34116e99bca7c16b2e30b06064`.

The user's separate full-board job `job-c5c6570c234a4e43` was not interrupted.
It independently reached its 1,800-second routing timeout and adopted no candidate.
The workbench was updated only after that job had ended.

## Next Engineering Gate

Review and repair the eight remaining physical connection findings using explicit
per-net regions, starting with the single /AN5 finding. Preserve this clearance-clean
candidate as the parent. Every adopted change must retain zero clearance errors,
decrease the target connection count and avoid new findings on other nets.
Only a separately requested release with all fresh gates passing may produce a
manufacturing package; no package is released by this delivery.
