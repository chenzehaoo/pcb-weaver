# Implementation contract

Historical v0.1 implementation contract. See `SYSTEM_PLAN.zh-CN.md` and
`SYSTEM_VALIDATION.zh-CN.md` for the current system scope and measured acceptance.

Independent implementation; upstream source is research material, not copied code.
Python 3.11+, Pydantic v2, sexpdata AST, scipy optimization, official MCP SDK.

## Board / planning modules

board.py exports read_board(path: str | Path) -> dict, write_placements(source, destination, placements: list[dict]) -> dict, compare_boards(before, after) -> dict.
read_board result: {version, footprints:[{reference,value,footprint,x,y,rotation,layer,locked,pads:[{number,net,x,y}],bounds:[xmin,ymin,xmax,ymax]}], nets:[{name,pads:[{reference,number,x,y}]}], outline:{bounds:[xmin,ymin,xmax,ymax],supported:bool}, tracks:int,vias:int,unsupported:[str]}.
Units mm/degrees. Empty net names never form a connected network. Net names (not numeric codes) are identities. Keep unknown AST fields on write. Detect unsupported geometry and duplicate references. Writes only copies; routed-board placement changes rejected.
planning.py exports plan_placements(board: dict, constraints: dict, count: int = 3) -> dict and audit_constraints(board: dict, constraints: dict) -> dict.
Constraints: {schema_version:1,board:{layers:2,max_voltage:24,edge_clearance_mm:1.0,component_gap_mm:0.5},fixed_references:[str],critical_nets:[str],proximity:[{reference,target,max_distance_mm}],regions:[{references:[str],bounds:[xmin,ymin,xmax,ymax]}],net_rules:[{nets:[str],min_width_mm:float}],fabrication:{min_track_mm:0.25,min_clearance_mm:0.2,min_via_drill_mm:0.3},release:{require_erc:true}}.
Plans return {candidates:[{id,placements:[{reference,x,y,rotation}],metrics:dict,feasible:bool,violations:list}],...}. Deterministic seed, scipy optimizer, do not claim global optimality. Constraints audited geometrically, report unknowns. Layout estimates not signoff.

## Toolchain module

toolchain.py exports Toolchain(config: dict | None = None), .doctor() -> dict, .run_drc(board: Path, output: Path) -> dict, .run_erc(schematic: Path, output: Path) -> dict, .export_dsn(board: Path, output: Path) -> dict, .route(dsn: Path, ses: Path, passes: int = 10) -> dict, .import_ses(board: Path, ses: Path, output: Path) -> dict, .export_manufacturing(board: Path, output_dir: Path) -> dict.
Commands subprocess shell=False, timeouts, per-operation logs return {status:'ok'|'blocked'|'failed',reason?,...}. Checks return {status, errors:int,warnings:int,unconnected:int,report_path,...}; must distinguish process success from electrical pass, require real JSON reports. Config: {kicad_cli?:str,kicad_python?:str,java?:str,freerouting_jar?:str,wsl_distro?:str,timeout_seconds?:int}. WSL optional config converts Windows absolute paths to /mnt/c/... and invokes wsl.exe argument list. Native PCB engine for DSN/SES operations, no hand-rolled routing. If module/helper missing fail explicitly. Include bundled native_bridge.py package helper. Don't overwrite source or accept same source/destination. Native helper may also make original minimal sample board using pcbnew for integration tests.

## Orchestration (main agent)

Immutable per-project revisions; constraints copied into each revision; input hashes; sqlite events and revision records; per-project advisory lock; all writes in controlled workspace. Imports copy project sidecars and schematic sheets. Candidate placement generates child revision. Routing generates child revision only on successful import; exact DSN/SES hashes recorded. DRC/ERC tied to complete revision digest. Release only after fresh check + constraint pass, manufacturing artifacts actually exist, manifest hashes. Report + CLI + MCP shared service layer. No vendor order submission.
