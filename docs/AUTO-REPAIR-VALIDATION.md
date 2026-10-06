# Automatic connection repair acceptance

This increment productizes bounded connection repair through the desktop web
workbench, the persistent engineering queue and the MCP Skill. It is not a new
full-board placement engine, universal PCB autorouter, or industrial certification.

## Delivered workflow

1. Submit a project and immutable revision. No net names, coordinates, UUIDs or
   board-specific repair script are required from the caller.
2. Validate source hashes, geometry support, critical-net constraints and native
   baseline DRC/ERC/parity/minimum-width checks. Existing clearance errors require
   the separate clearance workflow, not a bypass here.
3. Derive targets from native unconnected findings and actual board connectivity.
   Read native per-net rules; try additive routing, then bounded region expansion.
4. Generate proposals in isolated, cancellable subprocesses. Bound region area,
   graph size, proposal execution time and total attempts.
5. Merge only scoped copper changes into a new immutable revision. Preserve
   placement, electrical identity, other copper and noncopper board records.
6. Run native checks again. Adopt only strict connectivity improvement without
   new DRC/ERC findings, out-of-scope regression or weakened constraints.
7. Persist every attempt and the last accepted revision. Finish successfully only
   when the native engineering gate passes. Otherwise stop with evidence.

MCP entry: `submit_pcb_auto_repair(project, revision, options=None)`.
Follow with `get_engineering_job(job_id)`. MCP disconnection does not cancel the
job: the platform worker owns execution. The web toolbar has an automatic-repair
wand icon; task details show attempts, before/after missing connections and a
link to the retained revision.

Defaults: 6 attempts, 600-second scheduling budget, 60-second proposal timeout,
900 mm2 maximum repair region. The scheduling budget prevents starting new stages;
an active native command retains its configured timeout. It is not a hard total
wall-clock guarantee. Cancellation is cooperative at stage boundaries, with hard
proposal-child cleanup. An interrupted job is not silently restarted; inspect its
retained revision and submit a new authorized job to resume.

Short neckdowns and local fanout adjustment are separate explicit opt-ins, off by
default. Neckdowns require toolchain authorization, respect persisted minima and
retain the existing per-proposal narrow-length cap. Local adjustment preserves
widths/drills and is limited by the existing joint-escape geometry solver. These
optional strategies remain bounded experimental capabilities: the native matrix
below validates the default additive/expanded workflow, not every opt-in topology.

## Acceptance results

Full regression: **1451 passed, 26 skipped**, two dependency deprecation warnings.
Skipped opt-in/environment-dependent tests are not counted as passed. Raw test
results and skip reasons are in `validation/auto-regression.xml`.
The deployed desktop Python environment independently passes 141 focused
auto-repair/grid/protocol tests. The updated Skill passes `quick_validate.py`.

Real native regression uses disposable, deliberately damaged copies of donor
boards. Fixture construction names the removed segments; the solver receives
only project/revision/options and discovers repair targets itself. Donor and
fixture-source hashes remain unchanged.

| Native case | Missing before / after | Attempts | Repair time | Outcome |
| --- | --- | --- | --- | --- |
| Two-layer single gap | 1 / 0 | 1 | 26.704 s | Completed |
| Two-layer two gaps | 2 / 0 | 2 | 49.032 s | Completed |
| Four-layer system-board gap | 1 / 0 | 1 | 110.109 s | Completed |
| Already-passed board | 0 / 0 | 0 | 13.000 s | Completed, no new revision |
| Critical-net review required | Not routed | 0 | 0.172 s | Correctly blocked, unchanged |

All five cases satisfy their acceptance expectation. The critical-net case is
not a repaired board and must not be displayed as engineering-passed.

Additional contract/fault-injection tests cover strict option validation, process
timeout and cancellation cleanup, rejected candidates, ERC regression, budget
exhaustion, path/hash integrity, partial progress retention, legacy request hashes,
and restricted enterprise permissions. These are not substitutes for native tests.

Real deployed MCP acceptance:

- Job `job-83d1da46ea804a47` was submitted with just project and revision.
- The submitting MCP process disconnected; a second client followed the real
  persistent worker to completion.
- Current board `system-clearance-acceptance/r-1bbf979cb3cc4206` was rechecked and
  correctly returned unchanged, with no repair candidate and no manufacturing release.
- Native result: DRC errors 0, missing connections 0, ERC errors 0. Existing
  DRC warnings 53 and ERC warnings 16 remain visible.

Browser acceptance covers 1440, 390 and 320 px widths, real canvas pixels,
unobscured toolbar actions, valid/invalid options, opt-ins, stale-target rejection,
and rendering the real completed MCP job. Browser mutations are intercepted;
their payload tests do not claim to execute native repairs. Separate read-only
desktop/mobile acceptance checks inventories, tabs, the passed status band and
switching to the historical blocked revision without a stale pass badge. A real
320 px toolbar overlap found during testing was fixed with content-aware grid rows.

## Deployment and evidence

Runtime: 本机验收目录（公开版本已脱敏）。
URL: http://127.0.0.1:8765/?project=system-clearance-acceptance&revision=r-1bbf979cb3cc4206&tab=checks

Only listed code, web, Skill, test and acceptance files are synchronized. Source
`data` is never copied over desktop `data`. `.mcp.json` and the native toolchain
configuration retain their original hashes. The platform was restarted only after
confirming that no jobs were queued or running.

Evidence under `docs/validation`:

- `auto-native/results.json`, `auto-native-matrix/results.json`: native cases.
- `auto-mcp.json`: real protocol, disconnect/reconnect and persistent-worker result.
- `auto-ui/results.json` and screenshots: UI contract and actual-job display.
- `auto-finished-ui/results.json`: native status, inventories and canvas checks.
- `auto-regression.xml`: full regression and explicit skips.
- `auto-deployment.json`: source/deployed hashes, immutable boards and native status.

Large disposable regression workspaces remain in the source evidence directory;
only compact reports and screenshots are copied to desktop documentation.

## Release boundary

This is acceptance of the automatic connection-repair increment. It does not prove
full-board autonomous placement, every component/package type, arbitrary enterprise
platform compatibility, SI/PI, timing, current capacity, thermal/EMC performance,
manufacturing yield or physical board operation. The earlier system-board repair
and its power-escape/fabrication caveats remain documented in
`CONNECTION-FINISH-VALIDATION.md`.

The enterprise machine bridge does not acquire auto-repair permissions implicitly;
the new local/MCP action is denied there until explicitly designed and authorized.
No manufacturing release is authorized. Future illegal or unsupported designs must
still stop for review; there is no guarantee that arbitrary boards can never block.
