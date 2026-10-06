# Real KiCad Footprint Test Fixtures

Unmodified footprint data from the KiCad community's official
https://github.com/KiCad/kicad-footprints repository, fetched 2026-09-07.
These assets are CC-BY-SA 4.0 with the KiCad design-use exception; see LICENSE.md.
They are test data, not copied implementation code or a production board design.

Original paths on the repository's master branch:

- Resistor_SMD.pretty/R_0603_1608Metric.kicad_mod
- Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod
- Package_TO_SOT_SMD.pretty/SOT-23.kicad_mod
- Connector_PinHeader_2.54mm.pretty/PinHeader_1x04_P2.54mm_Vertical.kicad_mod
- Package_QFP.pretty/LQFP-48_7x7mm_P0.5mm.kicad_mod
- Capacitor_THT.pretty/CP_Radial_D5.0mm_P2.00mm.kicad_mod

The tests assemble these stored, offline assets into generated software fixtures.
No network access, external footprint library installation or 3D models are
required for the geometry tests. Missing 3D assets are outside placement coverage.
