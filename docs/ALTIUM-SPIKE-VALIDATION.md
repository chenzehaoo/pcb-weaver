# Altium Native MCP Spike

Date: 2026-09-13. Target: installed Altium Designer 26.7.1.11, user-reported
30-day trial. This is an isolated test bridge, not a production EDA adapter.
The existing KiCad MCP, desktop service, configuration and accepted core were not changed.

## Actual Acceptance

Official MCP SDK stdio client initialized the isolated server, listed tools,
called `altium_test_status`, then `altium_test_roundtrip(confirm_test_copy=true)`.
The final acceptance process exited with code 0 (execution session 89514).

Native evidence: [final result](validation/altium-spike/efe48e5029134357b2d67a8bf9ddfd5d/result.json).
All five operations dispatched real Altium DelphiScript and received distinct
request-bound reports. No native response was mocked:

| Operation | Track count before/after |
| --- | --- |
| Read fresh copy | 0/0 |
| Add top-layer test track and save | 0/1 |
| Reopen changed file and read | 1/1 |
| Remove matching test track in a second copy and save | 1/0 |
| Reopen restored file and read | 0/0 |

The test segment is nominally (10,10) to (20,10) mm, width 0.25 mm, top layer.
Actual Altium internal coordinates, width and layer match across save/reopen.
Source `D:\Altium\MCP-Test\bridge-test.PcbDoc` remained byte-identical:
`2658b6ffa4eb826c577379578a7fc303bbe7f96170ce079bc88fe1ac74b5409a`.
Changed and restored files have their own hashes in the evidence. Restored does
not mean binary-identical or proven full-board equivalence.

[Focused contract tests](validation/altium-spike-tests.xml): 14 passed, no skips.
These tests cover confirmation, source pinning, response identity, paths,
counts, and uncertain-request rejection; they do not substitute for native runs.

## Retained Failures

- `696e14694e594803b3a9dbb73228e5a1`: unsupported exception construction syntax;
  native compiler reported "Else expected". No write occurred.
- `558274d480cb454a9d15fdb0832f4402`: read passed, add reached memory but the
  original save message did not save. The generated dirty test copy may remain
  open in Altium. It is not the user's source document. Evidence remains failed.
- `5263188ee60a49b28517acd09af34a55`: five native operations passed after using
  `IServerDocument.DoFileSave`, but the SDK client rejected the valid JSON text
  response because it expected structuredContent. This is native-only success,
  not the final end-to-end client pass.
- `efe48e5029134357b2d67a8bf9ddfd5d`: new independent run after correcting JSON
  response parsing. Both native chain and SDK client passed.

## Boundaries

- Source is pinned to this one test fixture; no arbitrary document access tool.
- Restoration is compensating removal of a precisely matched test segment,
  **not Altium native Undo**. Native Undo remains unverified.
- No net assignment, component inventory, rules/stackup translation, DRC/ERC,
  full-object equivalence, auto-routing, manufacturing or enterprise validation.
- This bridge is a manually supervised, single-client development spike. Its
  lock and uncertain state are process-local, not multi-process/durable. Do not
  install it into unattended agents, run clients concurrently, or retry after a
  timeout without inspecting Altium. Durable arbitration is next-phase work.
- No default desktop MCP registration or deployment was changed. Development
  server: `scripts/altium_bridge_mcp.py`; client: `scripts/accept_altium_spike.py`.
- The configured paths are local to this machine. Native error dialogs require
  supervision; do not kill Altium or blindly dismiss unrelated dialogs.

## References

- [Official script processes](https://www.altium.com/documentation/altium-designer/scripting/running-scripts)
- [Official command-line scripting example](https://resources.altium.com/p/continuous-integration-implementation-using-altium-designer)
- [Official PCB object API](https://www.altium.com/documentation/altium-dxp-developer/pcb-api-system-interfaces-reference)
- [Official document save API](https://www.altium.com/documentation/altium-dxp-developer/system-api)
