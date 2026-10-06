# Manufacturing Regression Fixture

This is a new derivative of the project's original `../routing-demo` RC software
fixture, created on 2026-09-07. It is not an industrial reference circuit or a
substitute for the frozen 160-component system benchmark.

The original fixture has no enabled paste layers and no paste apertures on its
SMD pads. Its real manufacturing attempt was correctly blocked because the
required Gerber layer set was incomplete. That input and failed evidence remain
unchanged in `docs/validation/mcp-unified-grid-release.json`.

Explicit preparation changes:

- Enable front and back paste layers in the board layer table.
- Add front paste to all four existing SMD pads, with unchanged pad dimensions.
- Make the identical aperture-layer change in the local PassiveFixture library.

No electrical net, schematic, component position, copper geometry, design rule,
minimum width, clearance, drill, or error severity was changed. No original
manual routing is introduced. Paste manufacturing suitability still requires
assembly process review; this fixture only tests actual exports and packaging.

Example full-chain invocation, with an independently running workbench/worker:

```powershell
.venv/Scripts/python.exe scripts/accept_system_mcp.py --project manufacturing-check --board examples/manufacturing-demo/two-layer.kicad_pcb --constraints examples/manufacturing-demo/constraints.json --config toolchain.unified.json --passes 10
```
