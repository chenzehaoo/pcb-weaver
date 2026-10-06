# Source-Only Multi-Net Repair

## Acceptance Scope

This increment targets the three missing connections in
`completion-system-review/r-872a1728c20f4f93`, using only this input and newly generated
children. Source SHA-256:
`0092bf967ca1331ca500c3a2a9caf4a5ee02a7694a7051e4a1275fec3b291ecf`.
Historical passed-board copper is not a routing input. This is local repair of an
already routed checkpoint, not placement/routing from scratch or manufacturing
certification. Existing design rules, component placement and native gates remain
unchanged. Incomplete or rejected results must not be reported as passed.

The real MCP run and independent native-chain audit passed on 2026-09-12.
Job `job-4256ed2ac9994d49` completed 16 attempts, adopted three patches, and reduced
missing connections 3 -> 2 -> 1 -> 0 in 1360.703 seconds under the original budget.
Final revision: `completion-system-review/r-08ee661571804502`.
Final board SHA-256:
`18f8d0a04d0eee735c95c7d023263301549f066726e6d370daadd737baa8289d`.
Fresh native results: zero DRC errors, zero missing connections, zero ERC errors;
the original 53 DRC and 16 ERC warnings remain, with no new findings. Desktop,
390px and 320px UI checks passed against this actual job and exact revision.
Deployment hashes are recorded separately in `validation/autonomous-deployment.json`.

## Implementation

- Existing `submit_pcb_auto_repair` now accepts explicit
  `options.allow_multinet: true`. The default is false and legacy serialized request
  hashes are preserved. This mode selects the source-only strategy; it does not
  fall back to reference repair. The enterprise bridge retains its existing denial
  of automatic repair operations.
- The web form exposes the explicit source-only opt-in and its area/time limits.
  Area defaults to 900 mm2 and individual proposals to 60 seconds; selecting the
  mode does not silently raise either value. The accepted diagnostic setup uses
  2500 mm2, 180 seconds per proposal, 24 attempts and 1800 seconds scheduling time,
  with neckdown and local adjustment separately authorized. All mutations in UI
  tests are intercepted; only the MCP evidence represents an executed repair.
- Native missing-pair coordinates are normalized to millimeters. Blocking copper
  is ranked on the actual SMD pad copper layer, with through vias considered across
  layers. Nearby back-layer tracks are not mistaken for front-layer blockers.
- Try single-neighbor hypotheses as well as bounded multi-net combinations.
  Interleave attempts across missing pads before larger retries, so one difficult
  connection does not consume the entire attempt budget first.
- After a small escape-area hypothesis, also consider the selected neighbor's
  unlocked copper wholly inside the already bounded routing region. This avoids
  forcing a reconnect onto an unsuitable retained tail. The same area, net and
  removal-count limits apply; locked or out-of-region copper is never selected.
- The GEOS/SciPy grid router generates new copper. When a neighbor fails restoration,
  restart from the same immutable source with that net prioritized. Orders are
  deduplicated and at most eight are attempted per proposal.
- Global original connectivity partitions are checked explicitly. Merging unrelated
  islands cannot hide a split of a previously connected network. All affected
  original partitions must be restored, not merely the target connection.
- Native feedback has one bounded retry for otherwise acceptable candidates whose
  only new findings are dangling-track warnings. The retry uses the identical
  authorized removal set and region, restoring original cut endpoints. Temporary
  point-like copper seeds restrict graph terminals, and are removed before widening
  and merging. Exact-terminal mode prevents end-cap-only offset landings. Seeds
  never enter an accepted board. Other new warnings/errors are still rejected.
- Candidate limits: eight nets, 64 removed copper items, 2500 mm2. Job limits remain
  at most 24 proposals and an 1800-second cooperative scheduling budget. Each worker
  has a separate hard timeout of at most 180 seconds and is killed on cancellation.
  Native commands retain their own bounded timeout. No new unconditional retry loop
  or promise that arbitrary boards are solvable is introduced.
- A Python audit hook rejects other `.kicad_pcb` files and writes to the source.
  The worker records board access paths. This is a diagnostic/read restriction on
  the trusted Python worker, not an OS security sandbox for hostile native code.
- Each worker records SHA-256 for every top-level Python module in its package and
  rejects module changes, additions or removals during execution. Older v5/v10
  runtime files are retained separately for interpreting their failed candidates.
- After adopting a patch, prioritize remaining untried scope/rule hypotheses before
  repeating prior ones. Deferred hypotheses are not omitted or reused as results;
  every attempt reads the current source and still needs a fresh native pass.
- Cancellation checkpoints read only the control flag, rather than decoding full
  job evidence. Landing-region unions are batched per copper layer. A two-entry,
  content-keyed AST cache returns independent deep copies; inputs over two million
  characters bypass it. File digests and finite-value checks remain unchanged.
- Full AST-preserving merge and native DRC/ERC determine adoption. Reject altered
  scopes, missing/out-of-scope access audits, lost partitions, new native findings,
  and per-net regressions. Progress survives MCP disconnection through the existing
  persisted job queue. Manufacturing authorization stays false.

## Real Board Evidence

Exploratory native checks, independent of the final end-to-end run:

| Region | Result | Missing before / after | Evidence |
| --- | --- | --- | --- |
| GND neighborhood | Native improvement accepted | 3 / 2 | `validation/autonomous-ground-native-v4.json` |
| +3.3V neighborhood | Native improvement accepted | 3 / 2 | `validation/autonomous-power-native-v5.json` |
| AN5 / AN4 neighborhood | Native improvement accepted | 3 / 2 | `validation/autonomous-analog-native-v10.json` |
| GND after runtime optimization | Native improvement accepted | 3 / 2 | `validation/autonomous-ground-native-v12.json` |

These exploratory children are not treated as a combined passed board. The real
MCP task restarts at the original three-missing checkpoint and recomputes proposals
against each newly adopted source. Its baseline and every candidate are natively
verified. The native audit subsequently runs again on the final exact revision.

Main evidence:

- `validation/autonomous-system-mcp.json`: actual queue status, retained revision,
  disconnected submission, per-attempt outcomes, and source-only access records.
- `validation/autonomous-native-final.json`: independent per-child AST, scope,
  source access and rules audit, followed by fresh full native checks.
- `validation/autonomous-regression.xml`: full software suite; skipped tests are
  not counted as native acceptance.
- `validation/autonomous-ui/results.json`: actual task/revision rendering and
  1440/390/320 viewport checks, source-only form payload and stale-input protection.
- `validation/autonomous-deployment.json`: final scoped deployment hash audit.

Software cases include same-layer crossing congestion, cross-layer terminals,
an immutable two-layer wall that must remain blocked, stale input, out-of-scope
removal, split partitions hidden by equal island counts, failed-net reordering,
other-layer blocker exclusion, unauthorized scopes and foreign-board audit
rejection, and rejection of newly introduced native warnings via fault injection.
These tests are explicitly separate from the complex-board native result.

The independently audited adopted chain is:

| Target | Adopted child | Removed / added copper | Missing before / after |
| --- | --- | --- | --- |
| GND | `r-6ec05efb76674297` | 18 / 26 | 3 / 2 |
| AN5 | `r-a140428173f64105` | 23 / 214 | 2 / 1 |
| +3.3V | `r-08ee661571804502` | 8 / 59 | 1 / 0 |

The audit checks all 45 deployed Python module hashes against each adopted
proposal, every retained copper/noncopper AST, scope bounds, original source hash,
electrical identity and unchanged rules, then runs native checks again. The UI
asserts all 16 attempt rows, the exact displayed revision and native error counts,
modal/page bounds, nonblank PCB canvases, valid opt-in payloads and stale-input
protection. Its test submissions are intercepted; the MCP job above is real.

After the runtime optimizations, full software regression is 1555 passed, 26
skipped, zero failures, with two dependency deprecation warnings. Skipped tests
are not native qualification. The end-to-end native result is recorded separately.

## Reproduce

Use the configured desktop project environment. The following submit command creates
a real job and is authorized only when the user has approved bounded copper removal
and neckdown; it never releases manufacturing output.

```powershell
python scripts/resume_authorized_repair.py --root C:\path\to\pcb-weaver-system --project completion-system-review --revision r-872a1728c20f4f93 --output docs/validation/autonomous-system-mcp.json --authorized --max-region-area 2500 --multinet
python scripts/verify_autonomous_acceptance.py --root C:\path\to\pcb-weaver-system --run docs/validation/autonomous-system-mcp.json --output docs/validation/autonomous-native-final.json
python -m pytest -q --junitxml=docs/validation/autonomous-regression.xml
```

## Preserved Failures

`autonomous-probe-v2` records that fine grids alone did not restore neighbors; the
remaining old run was stopped after five recorded failures when superseded.
`autonomous-priority-v3` and `autonomous-ground-v3` record rejected fixed-neighborhood
orders. `autonomous-ground-v4` demonstrates the layer-aware improvement.
`autonomous-ground-v5` contains the audited worker candidate; its probe caller
initially rejected an absolute output compared with a relative working path.
The caller now normalizes the working directory before checking output containment.
None of these failed or interrupted runs is counted as full acceptance.

`autonomous-system-mcp-v5-cancelled.json` records the deliberately cancelled old
end-to-end run: GND improved from three missing to two, but AN5 candidates introduced
a new dangling AN4 endpoint and were rejected. The full final run must start again
at the original three-missing revision, not reuse an exploratory passed label.
`autonomous-analog-native-v6.json` records that approximate endpoint anchoring fixed
the dangling warning but introduced a copper-sliver warning. The v7 same-net body
reservations were infeasible in this scope. These failures remain distinct from
the subsequent exact-terminal implementation and its actual native results.
`autonomous-analog-native-v8.json` and `autonomous-analog-native-v9.json` still
introduced a copper-sliver warning and were rejected. In v10 the wider authorized
AN4 scope removes 23 copper items and recomputes both AN5 and AN4; its independent
native comparison accepts the reduction from three missing connections to two.
The sliver diagnostic was investigated against the official KiCad 9.0 checker:
https://gitlab.com/kicad/code/kicad/-/raw/9.0/pcbnew/drc/drc_test_provider_sliver_checker.cpp
No native severity or sliver tolerance was changed.

`autonomous-system-mcp-v10-budget.json` retains a real run that exhausted its
scheduling budget after 14 attempts with three missing connections still present.
Its GND and wider AN5 candidates hit the proposal timeout; it is not acceptance.
`autonomous-ground-v11` also records timed-out endpoint-preserving probes. Later
diagnostics default to the same endpoint policy as MCP, with explicit opt-in for
endpoint-preserving experiments.

Read-only runtime diagnostics are in `autonomous-checkpoint-benchmark.json` and
`autonomous-parse-benchmark.json`. The parsing measurement improves six reads from
about 4.89 to 4.15 seconds on this run; it is not proof of an end-to-end speedup or
an explanation of every timeout. The function-level profile is retained under
`autonomous-ground-profile` to identify the remaining cost.

The profile completed with a geometric proposal and identified AST copying/parsing
and lossless serialization as dominant cumulative costs. AST copies now clone
lists and symbols directly while preserving independent mutable objects, and
serialization primes the content cache only after its full round-trip equality
check. The second read benchmark records 3.51 seconds cold versus 1.77 seconds
cached for six reads (`autonomous-parse-benchmark-v2.json`). The normal v12 GND
worker completes in approximately 64 seconds under the unchanged 180-second cap;
its new child passes independent native improvement checks with no new findings.
