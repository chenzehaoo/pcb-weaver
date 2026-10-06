# Fixed-Placement Whole-Board Acceptance

## Scope

This phase starts with zero tracks and zero vias, retaining reviewed component
placement, circuit identity, project rules, outline and libraries. It is not
placement synthesis, hardware validation, SI/PI/EMC qualification or manufacturing
authorization. Native passing checkpoints from earlier local-repair phases are
not whole-board acceptance evidence.

The `placement_mode: preserve` completion option bypasses layout optimization.
Whole-board routing and bounded source-only local repair still require fresh
native DRC/ERC, schematic parity, geometry and width checks. The runtime compares
candidate findings to the original unrouted input, not only to the previous
routing candidate. Rules cannot be lowered to make the matrix pass.

The v4 design retains the full nonrouting board AST comparison, excluding only
top-level `segment` and `via` objects plus the `generator`/`generator_version`
labels. Every other node, including unknown fields and nested footprint/pad rule
overrides, must remain identical, preserving placement, setup, outline and other
board metadata. Arcs are not excluded from this comparison.
Companion files and constraints are checked separately. This is stronger than
checking component coordinates alone and is enforced both when retaining a
candidate and by independent acceptance verification.

## Frozen Fixtures

| Fixture | Components / pads / nets / layers | Removed segments / vias |
| --- | --- | --- |
| `examples/fixed-system-20260913` | 160 / 825 / 278 / 4 | 3025 / 255 |
| `examples/fixed-cpld-20260913` | 42 / 282 / 100 / 2 | 561 / 5 |
| `examples/fixed-programmer-20260913` | 64 / 247 / 111 / 2 | 392 / 0 |

These are three independent circuits derived from the attributed KiCad demos,
not three runs of one circuit. Original CC-BY-SA-4.0 notices, upstream provenance
and earlier mechanical-derivative notices remain in every fixture. `BENCHMARK.json`
records exact input hashes, all source files and the accepted-placement origin.
The source revisions stay untouched. Only top-level routing objects are removed;
all remaining AST nodes are asserted identical. These fixtures contain no zones.
Preparation rejects unsupported zones/groups rather than removing their semantics.
This phase therefore does not establish support for copper pours or plane routing.

## Execution

Use `scripts/wholeboard_benchmark.py prepare --source REVISION_FOLDER --output NEW_FOLDER`
to create a new fixture. Existing fixture directories cannot be overwritten.

The v4 cohort requires six fresh runs: system source and desktop invocations each
use `--repeats 1`, and source CPLD and programmer invocations each use `--repeats 2`.
Freeze the same core and profile for both implementations, with separate new D:
workspaces. Use `benchmarks/fixed-completion-options-v4.json`; its routing and
repair budgets are unchanged from v3. No v3 run qualifies as a v4 repetition.

Example desktop harness-owned invocation from the desktop implementation root;
the D: workspace and output must be new:

```powershell
.venv\Scripts\python.exe scripts/wholeboard_benchmark.py run --root . --workspace D:/CodexTemp/pcb-weaver-fixed-v4/desktop-system-001 --fixture examples/fixed-system-20260913 --options benchmarks/fixed-completion-options-v4.json --config toolchain.fixed-benchmark.json --project fixed-system-v4-desktop --repeats 1 --output docs/validation/fixed-system-v4-desktop.json
```

Example source harness-owned variant, from the source implementation root:

```powershell
..\..\.venv-pcb\Scripts\python.exe scripts/wholeboard_benchmark.py run --root . --workspace D:/CodexTemp/pcb-weaver-fixed-v4/source-system-001 --fixture examples/fixed-system-20260913 --options benchmarks/fixed-completion-options-v4.json --config toolchain.fixed-benchmark.json --project fixed-system-v4-source --repeats 1 --output docs/validation/fixed-system-v4-source.json
```

For source-owned small-board runs, select `fixed-cpld-20260913` or
`fixed-programmer-20260913`, use `--repeats 2`, and assign each invocation its own
new D: workspace, v4 project prefix and output JSON. These are command templates,
not evidence that the prepared runs have completed.

Both implementations support `--workspace` and `--external-worker`. External mode
attaches without starting or stopping the worker; MCP, worker and harness must
share the same workspace and configuration. Owned mode requires a new exclusive
workspace, defaulting to a fresh `benchmark-data/<uuid>` directory. Keep these
queues separate from normal desktop `data` and all historical workspaces. For v4,
explicitly select a new D: path instead of relying on the default directory.

Each repetition imports the frozen fixture, submits through the official MCP SDK,
disconnects, reconnects to fetch the persistent job, then independently reruns
native validation on the exact input/output revisions. A worker executes outside
the MCP connection. Every run records its options, toolchain, implementation,
source hashes, native results and job history. The script rejects optimized Python
execution and changes to package Python files, the harness or configuration during
the run. Output evidence is never overwritten. Failed and cancelled runs remain
in the historical record; later diagnostic repairs do not replace them.

## Repair Cycles

`CompletionOptions.repair_cycles` is a strict integer from 1 to 3, defaulting to
1. Values greater than 1 require `placement_mode: preserve`; optimize rejects
them. Default request serialization omits the field, preserving legacy hashes.
The UI omits it for optimize and for the default single cycle.

The isolated profile retains the same design minima and router edge clearance;
it uses eight routing passes, one optimization pass and a 3600-second native
route timeout. The v4 profile, like v3, explicitly selects three repair cycles and a
7200-second outer completion scheduling budget. Each cycle retains the unchanged
AutoRepair caps: 24 attempts, 1800 seconds scheduling, 180 seconds per proposal,
eight nets and 2500 mm2 per proposal. Three cycles can therefore use up to 72
attempts; 24 is not a whole-flow limit. Routing, verification and every cycle share
the outer completion deadline. Active native operations retain their own
bounds; scheduling budgets are not a wall-clock SLA. Neckdown/local adjustment
permissions are explicit and cannot lower declared minimum dimensions.

For `placement_mode: preserve` with `repair.allow_multinet: true`, each cycle first tries
repair with multinet disabled, capped at four attempts and 300 seconds scheduling.
It reserves at least one attempt and 30 seconds for a possible subsequent phase;
if the shared budget cannot support that split, the preliminary phase is skipped.
Both phases share that cycle's configured attempt limit and deadline, bounded by
the remaining outer completion budget. Four attempts/300 seconds are included in
the cycle's 24 attempts/1800 seconds. Standalone AutoRepair defaults and bounds are
unchanged. Optional neckdown/local adjustment still require explicit permission.

Continue to another cycle only if the completed cycle retained a native-accepted
revision with strictly fewer unconnected items than at its start. No such decrease,
an invalid candidate or an exhausted outer budget stops the flow. A successful
preliminary phase can justify continuation even if the subsequent phase makes no
further progress; the comparison is across the whole cycle. Passing native gates
ends completion immediately.

Cycle 1 retains `attempt.additive_repair` and optional `attempt.repair`. Extra
cycles alone appear in `attempt.additional_repair_cycles`, each with `cycle` (2
or 3), its actual phase records, source/retained revisions, status, timing and
counts. Workbench totals count the legacy fields once plus each extra cycle's
phases, with explicit cycle labels. An absent subsequent repair phase does not
mean no additive repair occurred.

## Strict Via Cleanup

V4 adds one strict redundant-via cleanup stage within completion, only for an
original input with zero tracks/vias, `placement_mode: preserve` and
`routing_policy: normalize_widths`. It targets newly introduced dangling-via
findings after routing. Unsupported vias are rejected; warnings are not ignored
or allowlisted. The proof must preserve the entire retained/nonrouting AST and
retained connectivity partitions, with fresh native checks and no per-net missing
connection increase or new findings. This is not a general copper-removal pass.

Read `flow.stage: via_cleanup` and `attempt.via_cleanup`. Only an accepted
`improved` cleanup with `proof.removed_ids` establishes the cleanup-stage deletion
count; top-level IDs alone can be a plan. `comparison.before_by_net` and
`after_by_net` show the connection check. Rejected cleanup retains its old revision.
Even `improved` means adoption at the cleanup stage, not guaranteed final-flow
adoption: the subsequent completion assessment still applies.

## Earlier Outcomes

The v2 desktop system's first run failed with two missing connections; its second
run was intentionally cancelled. The source pilot also failed with two missing
connections. Separate diagnostics later repaired an existing checkpoint from
2 to 1 to 0; they establish local-repair evidence, not a successful whole-board
completion from zero routing. V2 CPLD and programmer each passed 2/2 native runs,
but those outcomes do not qualify a later cohort.

## V3 Stage Record

The frozen-v3 software regression rerun using D: temporary storage completed.
The 1909-test total includes 26 skips: 22 opt-in/native-command cases without the
required configuration and four symlink-permission cases. Only 1883 passed; this
does not mean all 1909 tests executed.

| Evidence | Tests | Passed | Skipped | Failures | Errors | Seconds |
| --- | --- | --- | --- | --- | --- | --- |
| [D: full rerun](validation/fixed-wholeboard-v3-regression-d-final.xml) | 1909 | 1883 | 26 | 0 | 0 | 357.808 |
| [Earlier full run, retained](validation/fixed-wholeboard-v3-regression-final.xml) | 1909 | 1879 | 26 | 4 | 0 | 523.635 |

All four earlier failures reported `OSError: [Errno 28] No space left on device`
(ENOSPC). The [11 placement tests](validation/fixed-v3-placement-d-temp-3.xml)
passed using D: temporary storage without code changes; earlier failure/error
reports remain intact. Software regression success
does not establish whole-board native acceptance.

The initial desktop v3 job `job-811e53608fc24f98` (session `76042`) suffered an
ENOSPC report-write exit, and its worker stopped with six missing connections.
The saved benchmark JSON can still say `running` because reporting exited; that
stale state is not successful completion. This attempt remains non-passing evidence.

The independent retry uses workspace
`D:/CodexTemp/pcb-weaver-fixed-v3/desktop-system-retry-001`, project
`fixed-system-v3-desktop-retry-1`, job `job-195ad2296cbd4216` and session `62385`.
Its separate output is desktop-root
`docs/validation/fixed-system-v3-desktop-retry.json`; the record reports
`worker_mode: harness_owned`. This second desktop v3 attempt was blocked by six
new `via_dangling` findings, not a storage failure, and did not pass. Its failure
record remains intact; v4 cleanup changes require a new frozen cohort.

The [source system formal run](validation/fixed-system-v3-source.json) finished
with `passed` (session `86860`, exit 0). Its retained revision is
`r-9691a12c7cc34cef`, board SHA-256
`be4920955a0d643f421ec4391a7994c3356aff58e5c9db8dde43204a12b7312f`.
It contains 2902 tracks and 250 vias, with routed length 9429.8502595 mm.
Completion elapsed time was 5305.765 seconds; cycle 2 reduced missing connections
from 2 to 0 in 844.203 seconds. Independent verification found zero DRC errors,
zero missing connections and zero ERC errors, retaining 47 DRC and 16 ERC warnings.
There were no new native findings, no moved components, and the full nonrouting
AST preservation check passed. This is formal completion evidence from the frozen
unrouted input, distinct from the earlier checkpoint-only diagnostic repair.
Together with CPLD 2/2 and programmer 2/2, v3 had five native passes. All five are
historical evidence only and are excluded from v4 qualification.

## V4 Stage Record

The [real cleanup diagnostic](validation/fixed-v4-via-cleanup-diagnostic.json)
used an existing failed v3 checkpoint on D:, not a fresh unrouted input. It returned
`improved`, retaining `r-9eec623326fa4055` from `r-a912880e3ae841a3`. Six vias were
deleted at that stage; DRC warnings fell from 53 to 47. Eight missing connections
remained unchanged by net, with zero other DRC errors and zero ERC errors.
The retained/nonrouting AST and connectivity partitions were preserved. Native
status is still `blocked`: this diagnostic is not a whole-board pass.

The [full v4 software regression](validation/fixed-wholeboard-v4-regression.xml)
completed: 2007 tests comprise 1981 passed and 26 skipped, with zero failures or
errors. The reported test-summary duration is 352.45 seconds; the XML suite records
352.414 seconds. The skipped cases were not executed. The v3 regression figures
above remain historical.

The [deployment preflight](validation/fixed-v4-deployment-preflight.json) passed
checks for 225 files. The v4 core is now frozen. Regression and deployment checks
do not establish native whole-board acceptance. Final desktop synchronization
remains separate from the specific real-UI evidence recorded below.

All six v4 native runs have completed and passed, using independent D: workspaces
and the same frozen core/profile. System sessions `49479` and `12534` both exited
with code 0, as did programmer harness session `34223`. The
[evidence summary](validation/fixed-v4-matrix/summary.md) and
[matrix](validation/fixed-v4-matrix/matrix.json) qualify six of six runs across
three independent circuits, each with two passes. The summarizer checks recorded
evidence and local run hashes; it does not execute native tools again or itself
constitute independent native execution verification. Native, real UI and MCP
checks and the [final deployment](validation/fixed-v4-deployment-final.json) have
passed for this v4 fixed-placement phase only.

| Invocation | D: workspace directory | Repeats | Session | Execution state |
| --- | --- | --- | --- | --- |
| Desktop / system | `v4-desktop-system-001` | 1 | `49479` | Native passed 1/1, exit 0 |
| Source / system | `v4-source-system-001` | 1 | `12534` | Native passed 1/1, exit 0 |
| Source / CPLD | `v4-cpld-001` | 2 | `21271` | Native passed 2/2 |
| Source / programmer | `v4-programmer-001` | 2 | `34223` | Native passed 2/2, exit 0 |

All three v4 publications and their [real MCP QA](validation/fixed-v4-three-published-mcp.json)
passed; QA session `29487` exited with code 0. System publication session `43003`
also exited with code 0. Normal-workbench (8765) published revisions are:

| Publication | Normal project | Published revision |
| --- | --- | --- |
| [CPLD](validation/fixed-cpld-v4-publication.json) | `fixed-cpld-v4-review` | `r-8c9904f6ca1b40a2` |
| [Programmer](validation/fixed-programmer-v4-publication.json) | `fixed-programmer-v4-review` | `r-409bce309a1d4934` |
| [System](validation/fixed-system-v4-publication.json) | `fixed-system-v4-review` | `r-33f712f4dade4c53` |

These are published copies, distinct from native output revision IDs. The MCP
helper's `wholeboard_phase: not_evaluated` describes its limited scope, not a
failure or an override of the separately passed native matrix. Publication/MCP
success does not stand in for the final deployment report.

The first real CPLD UI invocation, session `96359`, completed desktop and mobile,
but narrow `page.goto` exceeded the unchanged 30000 ms timeout. Retain the
[load incident](validation/fixed-v4-ui-load-incident.md) and the two result entries
in `docs/validation/fixed-cpld-v4-ui`; this is not a complete three-viewport UI pass.
The cause is unconfirmed. A later low-memory launch was followed too early by
session `14221`, which failed with `ERR_CONNECTION_REFUSED`; retain its separate
folder `D:/CodexTemp/pcb-weaver-fixed-v3/ui-v4-cpld-final` as failed evidence.

After health confirmed readiness and the expected workspace, session `74679`
passed all three viewports with the original 30000 ms timeout and assertions.
Evidence: `D:/CodexTemp/pcb-weaver-fixed-v3/ui-v4-cpld-final-ready/results.json`.
Desktop/mobile/narrow canvas color counts are 1823/1036/656, with no page errors;
the narrow passed-board screenshot was visually inspected by the main validator.
This complete rerun does not replace or reclassify the earlier failed attempts.
Programmer UI session `15725` also passed all three viewports after low-memory
startup and readiness checks. Evidence:
`D:/CodexTemp/pcb-weaver-fixed-v3/ui-v4-programmer-final/results.json`.
Desktop/mobile/narrow canvas color counts are 1565/933/611, with no page errors;
the main validator inspected the actual mobile screenshot.

Four-layer system UI also completed all three viewports, originally in
`D:/CodexTemp/pcb-weaver-fixed-v3/ui-v4-system-final/results.json`, against native
job `job-8febac712c694d3f`, revision `r-6b982b62c2924bbb`. Canvas color counts are
4273/1593/769. The main validator confirmed no remaining `check_completion_ui`
process and visually checked the desktop actual-job and 320px narrow-passed
screenshots. All nine viewports have true page/unobscured layout checks, empty
page-error arrays and seven intercepted submissions each. Final source copies:

| Real UI evidence | Desktop/mobile/narrow colors | Result |
| --- | --- | --- |
| [System](validation/fixed-system-v4-ui-final/results.json) | 4273/1593/769 | Passed 3/3 |
| [CPLD](validation/fixed-cpld-v4-ui-final/results.json) | 1823/1036/656 | Passed 3/3 |
| [Programmer](validation/fixed-programmer-v4-ui-final/results.json) | 1565/933/611 | Passed 3/3 |

Native qualification, real UI, publication/MCP and final deployment remain
separate evidence scopes; the earlier CPLD failures are retained unchanged.

For subsequent auxiliary Python/UI launches, set process-local
`OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1` and `OMP_NUM_THREADS=1`, and check
health readiness before browser navigation. Only the idle normal workbench on
8765 was restarted with these settings (parent PID `14356`); observed Python
private memory fell from about 1 GB to 63 MB. C: free space had reached about
269 MB while the observed pagefile was about 22 GB. These observations do not
establish the cause of earlier failures. No system settings, frozen core or running
benchmark processes were changed or stopped.

The generic review publisher now supports ERC structural equivalence with
schema 3 evidence, rather than project-specific warning allowlists. Native outcome
counts, board/companion/constraint identity and finding-equivalence proofs remain
checked. Three real published reviews have passed MCP checks
([publication evidence](validation/fixed-v3-three-published-mcp.json)). These are
review-publication checks, not fresh v4 routing runs, manufacturing authorization
or a whole-board phase pass.

## Native Results

These final native values are transcribed from the matrix's six qualifying runs.
Revision IDs identify native output boards, not subsequently published copies.
Every row has zero native DRC errors, ERC errors and missing connections, zero
new DRC/ERC findings and zero moved components. Existing warnings are retained,
not waived. Times are completion-flow elapsed seconds; lengths are rounded to
three decimals here, with full precision and board hashes in `matrix.json`.

| Run | Native revision | Tracks | Vias | Length mm | Seconds | DRC/ERC warnings |
| --- | --- | --- | --- | --- | --- | --- |
| System / desktop | `r-6b982b62c2924bbb` | 2879 | 250 | 9433.372 | 5057.562 | 47/16 |
| System / source | `r-b414de11b6ab4676` | 2888 | 249 | 9430.465 | 5035.156 | 47/16 |
| CPLD / 1 | `r-18d9abc4edb1484a` | 561 | 5 | 2880.872 | 309.469 | 8/0 |
| CPLD / 2 | `r-5a78988b38854cce` | 561 | 5 | 2880.872 | 250.359 | 8/0 |
| Programmer / 1 | `r-8bf7145c2b934598` | 392 | 0 | 1994.431 | 171.016 | 2/0 |
| Programmer / 2 | `r-66869216775f40d3` | 392 | 0 | 1994.431 | 123.546 | 2/0 |

## Acceptance Matrix

V4 native qualification is **passed 6/6; final acceptance for this phase is passed**.
All six fresh runs use the same frozen core/profile and have their own native
evidence. Neither the cleanup diagnostic, v3 passes nor publication checks count
toward this native matrix:

| Fixture / invocation | Required runs | Execution state | Native qualification |
| --- | --- | --- | --- |
| System / desktop independent D: workspace | 1 | Completed | Passed 1/1 |
| System / source independent D: workspace | 1 | Completed | Passed 1/1 |
| CPLD / source independent D: workspace | 2 | Completed | Passed 2/2 |
| Programmer / source independent D: workspace | 2 | Completed | Passed 2/2 |

The three-circuit, two-passes-per-circuit native requirement is satisfied.
Final gate status is:

| Gate | Result |
| --- | --- |
| Native whole-board completion | Passed 6/6, three circuits each 2/2 |
| Software regression | 1981 passed, 26 skipped, zero failures/errors |
| Real UI | Passed 9/9 viewports, inspected actual screenshots |
| Published reviews | Passed 3/3 |
| Real MCP QA | Passed 3/3, session `29487` exit 0 |
| Deployment preflight | Passed 225 files |
| [Final deployment report](validation/fixed-v4-deployment-final.json) | Passed 225 files, exit 0 |

The final deployment report actually completed with 225 files passed and exit 0;
this is separate from preflight. The read-only Chrome check of the normal 8765
published system page also exited with code 0 and no JavaScript errors; retain
the [published desktop screenshot](validation/fixed-system-v4-published-desktop.png).
All acceptance gates for this v4 fixed-placement phase have passed. This does
not extend acceptance to other designs or phases. The final deployment report
records the hashes of the files it checked; consistency between these final
documents and the desktop copy is determined by that report.
Single-purpose helpers reporting `wholeboard_phase: not_evaluated` are scoped
checks, not failed phase judgments. No manufacturing authorization is claimed.
Mocked native responses count only as software
regression coverage.
