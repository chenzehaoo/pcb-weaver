# Connection repair investigation

## Status

**Not electrically completed. No new PCB candidate was adopted in this delivery.**
The preserved parent is `system-clearance-acceptance / r-b8fb758edb12488f`.
Its eight native unconnected findings remain: GND 4, +3.3V 3, /AN5 1.
The parent still has zero clearance errors; manufacturing remains blocked.

## Implemented and verified software

- `diagnose_pcb_repair` and the workbench now include conservative physical item
  envelopes, projected gap lower bounds, shared copper layers and classification.
  The repair dialog displays these actual diagnostic values without claiming path feasibility.
- The MCP acceptance runner accepts an explicit `--config`, using the existing
  durable runtime snapshot mechanism.
- A DSN class-splitting experiment preserves pin mappings and physical rules in AST
  tests, but failed runtime target-isolation acceptance. Its experimental profile
  is now fail-closed: scoped Freerouting requests return blocked before execution.
  `scoped_dsn.py` remains a testable research utility, not an enabled routing capability.
- `grid_route.py` uses Shapely/GEOS obstacle envelopes and SciPy sparse Dijkstra
  search for bounded, additive-only candidates. It has no arbitrary copper deletion,
  no placement changes, no neckdown and a 2.5M-node limit. `scripts/probe_grid.py`
  is an experimental local acceptance harness, not a production MCP routing mode.
  Candidates require retained-copper merge, native ERC/DRC, width and connectivity
  comparison before acceptance. No grid candidate passed on this complex board.

Geometry distances describe the native report's representative item pair, not the
nearest points between complete electrically connected islands. Conservative 2D
envelopes do not model drill voids, zone fill or high-speed behavior. A failed grid
search is not proof that no physical route exists.

## Real execution evidence

| Attempt | Result |
| --- | --- |
| MCP `job-546ceed1cdc04e44`, hard-width profile | 300-second router timeout; no candidate. Fanout ran on the entire board, then routing reported 138 unrouted engine items. |
| MCP `job-ca0c8858fe3a41f7`, split classes with `-inc` | Target filtering did not hold: logs reported 165 unrouted engine items and non-target routing. Timed out; no candidate. This mode is now blocked. |
| /AN5 additive grid, 0.1 mm | No path with fixed obstacles; no output candidate. |
| /AN5 additive grid, 0.05 mm | 2,137,120 nodes; only 96 reachable, all F.Cu. No path; no output candidate. |
| First GND pair, 0.05 mm | No legal start nodes at the native POWER-class width in this conservative model; no candidate. |

KiCad's eight physical connection findings and the router's much larger unresolved
item counts are different metrics. Do not display or interpret them as interchangeable.

The official [Freerouting 2.4.1 CLI documentation](https://github.com/freerouting/freerouting/blob/v2.4.1/docs/command_line_arguments.md)
documents `-inc`. The local runtime result, not the documented option alone, governs
whether this platform enables the capability. The reason that this runtime did not
honor it has not been proven; no engine-source fix is claimed.

Evidence files under the desktop project:

- `docs/validation/repair-mcp-dc86b794.json`
- `docs/validation/repair-mcp-1de6468e.json`
- `data/projects/system-clearance-acceptance/revisions/r-b8fb758edb12488f/repairs/grid-429b6a938465/result.json`
- `data/projects/system-clearance-acceptance/revisions/r-b8fb758edb12488f/repairs/grid-70feebf28dd9/result.json`
- `data/projects/system-clearance-acceptance/revisions/r-b8fb758edb12488f/repairs/grid-2297330ff749/result.json`
- `docs/validation/connection-regression.xml`: final software regression,
  1,365 passed, 26 environment-dependent skips, two dependency deprecation warnings.
- `docs/validation/connection-ui/`: intercepted-write desktop/mobile repair-dialog checks and screenshots.

## /AN5 escape obstruction

The net has no existing copper. It connects U102 pad 53 at (130.382, 109.148) to
MCU_PORT201 pad 20 at (161.544, 139.446). The projected envelope gap is approximately
41.981 mm, not a microscopic broken segment.

The 0.05 mm grid's reachable rectangle is approximately
`[130.332, 108.198, 130.432, 109.798]`, confined to the pad area on F.Cu.
Nearby /AN6 copper intersects the outward escape area:

- F.Cu segment `bd3c4cc8-3b8f-4e99-8b63-fbad7a234394`, width 0.2 mm,
  from (129.882, 109.9108) to (130.382, 110.4108).
- Through via `565adeac-cac8-4d6c-b016-1bd6d82b1a21`, diameter 0.6 mm,
  drill 0.4 mm, at (130.382, 110.4108).

These are measured nearby obstacles and a local-rework review target, not proof
that deleting either item alone creates a legal, fully routable channel.

## Next decision

Move from additive-only /AN5 routing to a reviewed joint /AN5 + /AN6 local rework
near U102. Identify the exact affected F.Cu/B.Cu segments and via, preserve terminal
attachments and all copper outside the approved area, and obtain approval before
deleting any listed copper. Do not lower widths or clearance, move components, or
erase whole nets. The adoption gate must close /AN5, keep /AN6 complete, keep zero
clearance errors, and introduce no other DRC/ERC findings. No rework has yet been
performed or accepted by this investigation.
