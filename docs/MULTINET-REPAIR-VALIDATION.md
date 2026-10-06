# Reference-Guided Multi-Net Repair Acceptance

## Scope

This checkpoint implements a bounded local multi-net ECO repair using copper from
an explicitly identified compatible reference revision. It does not replace the
whole board, solve arbitrary unrouted boards, certify industrial performance, or
authorize manufacturing. The four-layer autonomous completion benchmark without a
reference remains incomplete. Earlier failed attempts remain available as evidence.

Production entry points: local workbench automatic-repair dialog, persisted queue
operation `reference_repair`, and MCP tool
`submit_pcb_reference_repair(project, revision, reference_project, reference_revision)`.
The existing restricted enterprise bridge deliberately rejects this new operation:
cross-project reference access has not been added to its authorization model.
The local MCP and workbench use their existing local trust boundary.

## Implementation

1. Authenticate both immutable revisions. Require matching normalized constraints,
   electrical identity, component placement, layer stack, outline and project rules.
   Reject declared critical nets requiring dedicated engineering review.
2. Run fresh native checks on the source and reference. The reference must pass;
   its historical passed label alone is insufficient.
3. Require at least 70% unchanged source copper geometry. Cluster only geometry
   differences with GEOS/STRtree and SciPy connected components. Find local
   clusters touching actual native unconnected-item pads or copper, not hand-entered
   board coordinates.
4. Limit each patch to eight nets, 2500 mm2 and 1000 removed copper items. Preserve
   locked copper. The fixed run limit is 12 attempts and a cooperative 1800-second
   budget; native subprocess timeouts remain separate, not a wall-clock SLA.
5. Reuse only selected reference copper, remap net codes by name, and apply the
   existing AST-preserving local merger. Reject any out-of-scope additions. Create
   a sealed child with reference digest and scope hash; never overwrite the source.
6. Re-run full native checks. Adopt only strict connectivity improvements with no
   per-net regression, no new native DRC/ERC findings, and all existing gates met.
   Keep evidence for rejected candidates and the last adopted version on failure.
7. Persist progress for disconnected MCP clients. Return completed only when this
   operation reaches a genuinely passing child. No release operation is requested.

## Real Board Result

Source: `completion-system-review/r-872a1728c20f4f93`.
Reference: `system-clearance-acceptance/r-1bbf979cb3cc4206`.
Job: `job-48dcbf94f19d4f2c`; submitter disconnected and reconnected for polling.
Final: `completion-system-review/r-2356718636b34fc3`.

The board retains 160 components, 825 physical pads, 278 nets and four copper layers.

| Patch nets | Area mm2 | Removed | Added | Missing after |
| --- | ---: | ---: | ---: | ---: |
| GND, /XTAL | 2.8042 | 3 | 35 | 2 |
| /AN5, /AN6 | 939.3426 | 5 | 256 | 1 |
| +3.3V, /QSPI_CS1, /QSPI_CS2, /QSPI_CS3 | 103.0134 | 17 | 179 | 0 |

Only 25 of the 3030 original copper items are replaced; 3005 retain their UUIDs and
exact AST content. The final board has 3222 tracks and 253 vias. All noncopper AST
content is preserved at every step. New copper matches the selected local reference
geometry, not an entire reference board import.

Fresh independent KiCad 9.0.9 result: **passed**, DRC errors **0**, unconnected **0**,
ERC errors **0**. Existing DRC warnings **53** and ERC warnings **16** remain visible.
There is no claim of zero warnings, manufacturing release, SI/PI, EMC, thermal,
high-speed timing validation, fabrication, or measured hardware behavior.

SHA-256:

- Source: `0092bf967ca1331ca500c3a2a9caf4a5ee02a7694a7051e4a1275fec3b291ecf`.
- Reference: `3f871730ae06e4c6c401555d0be4cb85effb43f7106385bb94387205c14584c5`.
- New board: `0581c25bf9f3d69108fa7946635f408e64ab3dbbfba713578d7b0c95b2ae83eb`.

## Evidence and Reproduction

Final software regression: **1531 passed, 26 skipped**, with two dependency
deprecation warnings. The skipped tests are not counted as acceptance. Separate
real native, MCP and browser runs provide the board-specific execution evidence.
Deployment audit: **50 files matched**, prior board and configuration hashes
preserved, and the three earlier completion MCP records still valid.

- `validation/multinet-reference-mcp.json`: actual persisted MCP task and all scopes.
- `validation/multinet-native-final.json`: independent fresh native verification,
  authenticated queue/evidence comparison, per-child AST and geometry audit.
- `validation/multinet-regression.xml`: complete software regression, with skips
  reported separately, not counted as passed native execution.
- `validation/multinet-ui/results.json`: 1440/390/320 viewport checks, nonblank canvas
  pixel diversity, intercepted form payloads, stale-target rejection, out-of-order
  reference-load protection, real completed-job display, and no page errors.
- `validation/multinet-deployment.json`: scoped source/deployed hash comparison and
  unchanged prior boards/configuration. Validation artifacts are copied separately.

Run from the source root with the project Python environment:

```powershell
python scripts/verify_multinet_acceptance.py --root C:\path\to\pcb-weaver-system --run docs/validation/multinet-reference-mcp.json --output docs/validation/multinet-native-final.json
python -m pytest -q --junitxml=docs/validation/multinet-regression.xml
node scripts/check_auto_repair_ui.cjs "http://127.0.0.1:8765/?project=completion-system-review&revision=r-2356718636b34fc3&tab=checks" docs/validation/multinet-ui job-48dcbf94f19d4f2c completed 0 reference_repair
```

## Research That Did Not Pass

At this historical checkpoint, `ripup_planning.py` and `negotiated_reroute.py` were
experimental helpers, not exposed through MCP, HTTP or the Skill. Their later
[source-only repair increment](AUTONOMOUS-REPAIR-VALIDATION.md) is separately verified.
Two native Freerouting scope attempts
failed routing; see `validation/multinet-native-probe.json`. Six geometric
negotiation probes reconnected the target but failed to restore neighboring
CANRX/QSPI_CS3 islands; see `validation/negotiated-probe-v1`.

The geometric probe ran the earlier direct-grid path. The later fine-pad fallback
has unit coverage only, not a passed complex-board native run. Neither experiment
is presented as a successful universal rip-up/reroute engine. A compatible passing
reference is mandatory for the delivered production strategy; incompatible or
unavailable references must stop with an honest reason rather than bypass checks.
