"""Isolated, fixed-fixture Altium MCP spike. Not a general PCB adapter."""
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('D:/Altium/MCP-Test/bridge-test.PcbDoc')
EXPECTED = '2658b6ffa4eb826c577379578a7fc303bbe7f96170ce079bc88fe1ac74b5409a'
EXE = Path('D:/Altium/AD26/X2.EXE')
RUNS = ROOT / 'docs/validation/altium-spike'
LOCK = threading.Lock()
UNCERTAIN = False
mcp = FastMCP('PCB Weaver Altium Test Bridge', instructions=(
    'Fixed test fixture only. Native roundtrip is not DRC or routing acceptance. '
    'Never describe track removal as native Undo or full board equivalence.'))

WIFI_RESULT = ROOT / 'docs/validation/altium-native/a201905ecc7b4c3eb819e531d52c5f1e/result.json'
WIFI_RESULT_SHA = '2d306cfd23c1c5ec5fbdee41991bb494d9a23103012999477c7dc45380f5f4ad'
WIFI_SOURCE_SHA = 'ec13cc84307c6393263624a1a2f4c10ddb767640b2860c3e71e655098a87e031'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def literal(path):
    value = str(path)
    require(not any(c in value for c in '\r\n|"'), 'Unsupported script path')
    return value.replace("'", "''")


def parse_report(path, request, action, board):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(path.read_text(encoding='utf-8-sig'))
    row = dict(parser['bridge'])
    require(row['request'] == request and row['action'] == action, 'Wrong response identity')
    require(Path(row['board']).resolve() == board.resolve(), 'Wrong board response')
    require(row['status'] == 'completed', 'Native operation incomplete')
    for key in ('before', 'after'):
        require(int(row[key]) >= 0, 'Invalid track count')
    return row


def invoke(folder, board, action):
    global UNCERTAIN
    require(action in ('inspect', 'add', 'remove'), 'Unsupported action')
    require(board.resolve().is_relative_to(folder.parent.resolve()), 'Board outside run')
    folder.mkdir()
    request = uuid.uuid4().hex
    report = folder / 'response.ini'
    template = (ROOT / 'scripts/altium/Bridge.pas.template').read_text()
    values = {'REQUEST': request, 'ACTION': action, 'BOARD': literal(board), 'REPORT': literal(report)}
    for key, value in values.items():
        template = template.replace('@@' + key + '@@', value)
    require('@@' not in template, 'Unresolved template')
    (folder / 'Bridge.pas').write_text(template, encoding='ascii')
    project = folder / 'Bridge.PrjScr'
    project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Bridge.pas\n', encoding='ascii')
    command = (f'"{EXE}" -RScriptingSystem:RunScript('
               f'ProjectName="{project}"|ProcName="Bridge.pas>Run")')
    proc = subprocess.Popen(command, cwd=EXE.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if report.exists():
            # The application writes a tiny report only after closing its test document.
            time.sleep(0.2)
            return parse_report(report, request, action, board)
        time.sleep(0.25)
    UNCERTAIN = True
    raise TimeoutError(f'No native reply; do not retry or kill Altium. Inspect PID {proc.pid}, {folder}')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_test_status() -> dict:
    """Inspect fixed source bytes and executable availability, not license status."""
    return {'scope': 'fixed_fixture_spike', 'source': str(SOURCE),
            'source_exists': SOURCE.is_file(), 'source_sha256': sha(SOURCE) if SOURCE.is_file() else None,
            'expected_sha256': EXPECTED, 'executable_exists': EXE.is_file(),
            'uncertain_native_request': UNCERTAIN, 'industrial_qualified': False}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_inventory_snapshot(
    section: Literal['summary', 'components', 'pads', 'nets', 'rules'] = 'summary',
    offset: int = 0, limit: int = 100,
) -> dict:
    """Read a pinned native Wi-Fi example inventory snapshot; never authorizes routing."""
    from altium_inventory_gate import validate_inventory
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 500,
            'Invalid pagination')
    raw = WIFI_RESULT.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == WIFI_RESULT_SHA, 'Pinned native result changed')
    gate = validate_inventory(WIFI_RESULT, expected_source_sha256=WIFI_SOURCE_SHA)
    require(gate['inventory_valid'], str(gate.get('errors')))
    result = {'scope': 'pinned_snapshot_not_live_read', 'gate': gate}
    if section != 'summary':
        prefixes = {'components': 'component', 'pads': 'pad', 'nets': 'net', 'rules': 'rule'}
        prefix = prefixes[section]
        native = json.loads(raw)['native']
        count = gate['counts'][section]
        result.update(section=section, total=count, offset=offset, limit=limit,
                      items=[native[f'{prefix}.{i}'] for i in range(offset, min(offset + limit, count))])
    require(sha(WIFI_RESULT) == WIFI_RESULT_SHA, 'Native result changed during read')
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_constraints_snapshot() -> dict:
    """Read validated native numeric rule fields; incomplete constraints never authorize routing."""
    from altium_constraints_snapshot import snapshot
    return snapshot()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_geometry_snapshot(
    section: Literal['summary', 'pads', 'polygons'] = 'summary',
    offset: int = 0, limit: int = 50,
) -> dict:
    """Read pinned electrical layers, padstack fields and polygon definitions; not full geometry."""
    from altium_geometry_snapshot import snapshot
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
            'Invalid pagination')
    data = snapshot()
    pads = data.pop('pads')
    polygons = data.pop('polygon_definitions')
    if section != 'summary':
        rows = pads if section == 'pads' else polygons
        data.update(section=section, offset=offset, limit=limit, total=len(rows),
                    items=rows[offset:offset + limit])
    return data


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_dsn_audit(include_details: bool = False) -> dict:
    """Audit the pinned existing DSN export against native inventory, without running a router."""
    from altium_dsn_audit import audit_export
    path = ROOT / 'docs/validation/altium-native/9dcb104cde14458ba7425ff6f306aa0e/result.json'
    expected = '8461ef819880fe56aaa7501f51152f85410d1b0fa8b190926994c190df5a73ed'
    require(sha(path) == expected, 'Pinned DSN result changed')
    data = audit_export(path)
    require(sha(path) == expected, 'DSN result changed during read')
    if not include_details:
        for key in ('geometry_evidence', 'placement_evidence', 'rules'):
            data.pop(key, None)
    return data


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_drc_coverage_snapshot() -> dict:
    """Read expanded DRC evidence, including actual violations and incomplete SI status."""
    from altium_drc_coverage_snapshot import snapshot
    return snapshot()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_local_repair_status() -> dict:
    """Read verified isolated-board GND repair evidence; not full-project or manufacturing acceptance."""
    from accept_altium_repair import accept
    return accept()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_redesign_status(include_findings: bool = False) -> dict:
    """Read route-B baseline evidence; compile success is not routing or production acceptance."""
    from altium_redesign_status import snapshot
    return snapshot(include_findings)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def altium_wifi_silk_repair_status(include_findings: bool = False) -> dict:
    """Read accepted R11 silk repair: 159 to 154 findings, not whole-board acceptance."""
    from altium_silk_status import snapshot
    return snapshot(include_findings)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def altium_test_roundtrip(confirm_test_copy: bool = False) -> dict:
    """Explicitly run add/save/reopen/remove/reopen on new copies of the pinned test PCB."""
    require(confirm_test_copy is True, 'Explicit test-copy confirmation required')
    require(LOCK.acquire(blocking=False), 'Native test already running')
    try:
        require(not UNCERTAIN, 'Previous native request outcome uncertain; manual inspection required')
        require(sha(SOURCE) == EXPECTED, 'Source changed; refuse automatic repinning')
        folder = RUNS / uuid.uuid4().hex
        folder.mkdir(parents=True)
        result = {'schema': 1, 'status': 'running', 'scope': 'test_track_roundtrip',
                  'source': str(SOURCE), 'source_sha256': EXPECTED, 'steps': [],
                  'native_drc': 'not_run', 'full_board_equivalence': 'not_verified',
                  'restoration_method': 'compensating_test_track_removal_not_native_undo'}
        try:
            changed = folder / 'changed.PcbDoc'
            restored = folder / 'restored.PcbDoc'
            shutil.copy2(SOURCE, changed)
            require(sha(changed) == EXPECTED, 'Snapshot differs from input')
            for index, action in enumerate(('inspect', 'add', 'inspect', 'remove', 'inspect')):
                board = changed if index < 3 else restored
                if index == 3:
                    shutil.copy2(changed, restored)
                step = invoke(folder / str(index), board, action)
                result['steps'].append(step)
                expected_counts = ((0, 0), (0, 1), (1, 1), (1, 0), (0, 0))[index]
                require((int(step['before']), int(step['after'])) == expected_counts, 'Track count mismatch')
            geometry = ('x1', 'y1', 'x2', 'y2', 'width', 'layer')
            require(all(result['steps'][1][key] == result['steps'][2][key] for key in geometry),
                    'Saved track geometry changed on native reopen')
            require(sha(SOURCE) == EXPECTED, 'Original source changed')
            result.update(status='passed', original_unchanged=True, changed_sha256=sha(changed),
                          restored_sha256=sha(restored), evidence=str(folder / 'result.json'))
        except Exception as error:
            result.update(status='failed', error=str(error), original_unchanged=sha(SOURCE) == EXPECTED)
            raise
        finally:
            (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    finally:
        LOCK.release()


if __name__ == '__main__':
    mcp.run(transport='stdio')
