# Altium Real-Board Integration: Phase 1

Date: 2026-09-13. Target: Altium Designer 26.7.1.11 on this workstation.

**Overall automatic-routing acceptance: incomplete / blocked.** No new routed
board, manufacturing release, or general-purpose Altium adapter is delivered by
this phase. This report separates working native operations from missing design
semantics. Existing KiCad acceptance and desktop configuration were not changed.

## Real Fixture

Source: installed Altium example
`D:\Altium\AD26-Examples\Examples\Mini PC\Mini PC - WiFi\WiFi.PcbDoc`.
Used only for local evaluation; no independent redistribution license is claimed.
Its existing 579 tracks and 86 vias are the original designer's work, not our
autorouter output. It has four signal layers and four polygons, not a two-layer
empty test fixture. The selected board therefore extends the initial small-board
proposal and requires more constraint handling before routing.

Native survey: 28 components, 200 pads, 27 nets, 40 rules, 7 arcs.
Examples include U2 STM32F103RBT7TR, U1 SPWF01SA, P1 mini-PCIe connector,
Y1 crystal, four LEDs, resistors and capacitors. The component comment is source
metadata, not independently verified supply-chain information.

Source SHA256 remains:
`ec13cc84307c6393263624a1a2f4c10ddb767640b2860c3e71e655098a87e031`.
The user's separate `bridge-test.PcbDoc` also remains byte-identical to its
previous hash. All native writes/export attempts used fresh workspace copies.

## Verified Operations

| Operation | Actual result |
| --- | --- |
| Native component/pad/net/rule inventory | Completed; all 297 INI sections present |
| Rule scopes, DRC-enabled flags, priorities | Read natively for 40 rules |
| Snapshot/source and INI/JSON consistency gate | Valid; automatic routing remains blocked |
| Real official-SDK MCP inventory pagination | Passed for 28/200/27/40 rows |
| Silent `RouteCCT:ExportDesignFile` | Actual DSN generated, hash retained |
| `Board.RunBatchDesignRuleCheck` | Actual HTML generated for the copied board |
| Complete DRC coverage | Not established; initial report lists only six checks |
| All-category DRC options probe | Failed: getter not exposed to DelphiScript |
| Freerouting execution and Altium route writeback | Not executed; prerequisite gate blocked |
| Focused software tests | 98 passed, no skipped tests |

Evidence:

- [Real MCP inventory, full object rows and readiness gate](validation/altium-wifi-mcp-inventory.json)
- [Pinned native inventory](validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json)
- [Repeated inventory with response/template/snapshot hashes](validation/altium-native/8fa0893073d749109c4cb5c8fbfa05ff/result.json)
- [Repeated silent export](validation/altium-native/9dcb104cde14458ba7425ff6f306aa0e/result.json)
- [Native DRC invocation](validation/altium-native/5c525fb85c964c329747849c46897dd3/result.json)
- [Native DRC HTML](validation/altium-native/5c525fb85c964c329747849c46897dd3/native-drc.html)
- [Focused test report](validation/altium-phase1-tests.xml)

The DRC report's zero total is scoped to its reported checks. It must not be
advertised as all 40 rules checked, electrical functionality, or manufacturing
qualification. Selecting a RuleSet bit is also not proof that a corresponding
check executed; checked rules and report coverage still need reconciliation.

## Remaining Engineering Work

The gate explicitly marks these as missing: full layerstack, complete padstacks,
keepouts, polygons and numeric rule constraints. Rule expressions are recorded,
not evaluated. Difference-pair clearance and `DiffPairsRouting` are enabled in
this board; they cannot be replaced by one generic clearance/width value.

Observed initial export options had `Export Differential Pairs` and `Export Rule
regions` on, but `Export Polygons` off. No default-export file is admitted as a
complete routing model. Future work must bind export options to the input and
validate their geometric and semantic coverage.

Freerouting produces SES. The installed Altium importer explicitly supports RTE;
renaming SES to RTE is not conversion. A strict, unit-aware wire/via/net/layer
mapping or a verified conversion/import path is still required.

Complete DRC needs an alternative supported options path, such as a verified
DRC-only OutJob or controlled native UI execution, with all check settings and
report coverage recorded. Do not lower rules or accept the existing six-row
report merely to obtain a green status.

## Failure History and Recovery

- Interactive export `0f7aff0fbca84460b0b4ed0544c71bd1` timed out waiting for native
  dialogs. A DSN arrived after the dialogs were completed; the failed harness
  result was retained, not rewritten as passed.
- Inventory `3f415c7727254ea8a6f796aa878f9693` was delayed by the earlier modal
  and timed out. Later fresh inventory runs completed independently.
- Native `DoFileSave('PCB ASCII')` in the exploratory survey returned success
  but left a binary file. It is not accepted as ASCII conversion evidence;
  this call has been removed from the survey implementation.
- `a059b7fbe39e496fb567d0ca59b8d0fe` all-category DRC probe failed at
  `GetState_DesignRuleCheckerOptions` with "Undeclared identifier". The method
  exists in local implementation metadata but is not exposed through this tested
  script interface. The error dialog was captured and dismissed; no constraints
  were changed by this compile-failed script.
- New native jobs use an exclusive persistent pending marker. Failed/uncertain
  calls retain it and reject further dispatch. The DRCAll marker is archived only
  after operator inspection of this specific compile error and application state;
  this does not turn the DRCAll result into a pass.

## Code and Use Boundaries

Later constraint capture and expanded DRC results are documented in
[ALTIUM-CONSTRAINT-PHASE2.md](ALTIUM-CONSTRAINT-PHASE2.md). In that fresh run,
expanded checking found 158 violations; the earlier narrow zero-count report
must not be read as full-board acceptance.

### Native Script Runtime Recovery Follow-up

Added `scripts/altium_drc_gate.py`: independently checks the existing DRC
evidence hashes, board identity, HTML totals, rule rows and waiver reporting.
The real baseline report validates structurally, but remains blocked: six
reported rule rows versus forty enabled inventory rules; exact mapping and
execution options are unproven, and absent waived totals remain unknown.
This is an offline gate, not a deployed workbench status change.

Follow-up regression: 148 tests passed (including 48 DRC-gate tests), recorded
in `docs/validation/altium-drc-followup-tests.xml`. These are software tests,
not a successful native negative-control or complete routing acceptance.

The new `DRCNegative` action creates a fresh example copy and injects a 0.001 mm
top-layer track as an intentional width-rule negative control. It is not an
autorouting output. Its native execution has NOT passed.

Requests `2752a6fbd24a4a19a40316b20e702b96` and
`50b76383e363416b9bbbbf59c75177a0` were rejected by Altium with
"Another script executing now". The native Run > Stop command was invoked
between attempts, but did not release the scripting runtime. The second pending
marker is retained. Do not dispatch further scripts until the runtime is
recovered. A responsive window and absence of modals do not establish script
engine readiness. No application process was killed, and failed results are
retained. The source example SHA-256 remained unchanged at the recovery check.

Recovery requires preserving user documents and restarting Altium, followed by
an independent fresh read-only probe before another negative-control attempt.

### Confirmed Recovery and Native Controls

After the user restarted Altium, the old process 24692 was absent and the new
main window was process 26484. The retained marker was archived as
`pending-resolved-user-restart-50b763.json`; historical failures were unchanged.
Read-only inventory `f1fc677dd82e4b23b3542dc19f1989b2` then completed with 297
sections, confirming script execution recovered.

Native negative control `2c5259b0dcbb4d0eb0f983445d9dea79` completed and reported
two violations, including one width violation: actual 0.001 mm against a
0.25 mm minimum. `native_return=False` is retained. This is the EXPECTED
rejection of intentionally invalid copper, not a passing board or routing output.
Fresh unmodified control `cd102d2ca95947cbae434e9cf1ec0721` completed separately.
The original example and user `D:/Altium/MCP-Test/bridge-test.PcbDoc` hashes
remained unchanged. `scripts/accept_altium_drc_controls.py` checks these captured
controls without launching Altium. Full rule coverage remains unproven.

Control acceptance completed successfully: baseline 0 violations versus injected
copy 2 violations, with the expected width violation detected. The HTML parser
now supports native violation detail tables and checks them against summary rows.
Combined regression: 163 passed, recorded in
`docs/validation/altium-restart-controls-tests.xml`. This supersedes the earlier
statement that the native negative control had not run, but does not supersede
the incomplete full-routing/full-coverage status.

`scripts/altium_native_job.py` is a supervised developer runner, not a production
job service. `scripts/altium_inventory_gate.py` validates existing snapshots and
never authorizes full routing. The isolated test MCP adds the read-only
`altium_wifi_inventory_snapshot` tool; it reads a pinned snapshot, not the live
application. No default desktop MCP registration was changed.

Sources: [official Specctra interface](https://www.altium.com/documentation/altium-designer/design-tools-interfacing/specctra-router),
[official DRC setup](https://www.altium.com/documentation/altium-designer/pcb/drc/setting-up-running),
[official system API](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-system-interfaces-reference).
