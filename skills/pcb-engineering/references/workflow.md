# Supported workflow

## Input contract

The import is a project-directory snapshot of KiCad boards, schematics, design rules,
project JSON and local symbol/footprint libraries. Input files remain untouched.
Use a focused directory; snapshots limit file count and per-file size.
External library dependencies are not fetched automatically. Missing symbols or sheets
must remain visible in engine output. Use project-relative child sheet paths.

The headless native adapter uses KiCad's pcbnew bindings for DSN/SES. It is a compatibility
adapter for the tested KiCad release, not an implementation of KiCad IPC.
The official long-term IPC API is documented in the repository research report.

## Operation semantics

| Tool result | Meaning |
|---|---|
| imported / created | A design snapshot or child revision exists |
| feasible candidate | Supported placement constraints pass; no electrical signoff |
| routed_unverified | Actual router output was imported; check this revision next |
| verification passed | Available ERC/DRC, parity and supported constraints meet configured gates |
| released | A newly checked local package and manifest exist; no order was submitted |
| blocked / error | An explicit unmet requirement; retain diagnostics and do not fabricate an artifact |

## Revision discipline

Every plan is tied to the complete design and constraint digest, and its content hash
is recorded in the local ledger. Applying an edited or stale plan must fail.
Sealed snapshots are immutable to the application. External edits are detected on the next
operation and require a new import. Locking serializes concurrent operations on one project.
The local ledger is hash chained, but an owner able to rewrite the whole database can also
rewrite the chain. It is not a signed compliance or regulatory audit trail.

## Manufacturing evidence

Inspect unconnected counts, error counts and exclusions. Warnings may still need engineer review.
The package includes design snapshots, declaration of constraints, actual checks, Gerber,
drill and position exports plus schematic-derived BOM when available.
Stock availability, approved-vendor mapping, assembly rotation conventions, special process
notes and a prototype test report remain separate engineering inputs.

## Integration

Use the generated absolute-path MCP JSON after running the repository bootstrap.
This avoids depending on the agent's working directory or an unverified plugin-root placeholder.
The CLI exposes the same operations for reproducibility and CI use.

## System-scale work

Use the workbench's project/revision views to inspect actual footprints and nets, not a representative illustration.
Large boards should use durable jobs. A pipeline accepts either `board_path` plus explicit import constraints,
or an existing `revision`, never both. An existing revision's constraints cannot be silently replaced;
create a constraints child revision first. Candidate generation and application also exist as separate jobs.

The auto placement strategy selects the supported scalable solver for larger designs. Use the current
schema's placement options to bound iterations, block size and seed. Compare feasibility first and estimated
HPWL second. Do not advertise improved signal integrity based on shorter HPWL.

The bundled ColdFire/CPLD example is a licensed derivative of a real KiCad demo. Preserve its provenance
and distinguish original topology, benchmark preparation, newly optimized placement and newly routed copper.
Removing original routing in a benchmark preparation script is not authorization to erase routing in a user's project.

Queue states `queued`, `running`, `completed`, `blocked`, `failed`, `cancelled` and `interrupted` are distinct.
A completed layout-only job has not passed ERC/DRC. Read its step results and the exact final revision ID.
Do not use the number of tests or the size of the demonstration as evidence of untested industrial capabilities.
