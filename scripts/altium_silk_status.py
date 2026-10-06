"""Read hash-pinned evidence of the accepted local silkscreen repair."""
import configparser
import json
from pathlib import Path

from altium_project_snapshot import ROOT, _read, _sha
from altium_redesign_status import snapshot as baseline_snapshot, FOLDER as BASELINE
from altium_silk_repair import verify_objects, verify_drc
from altium_drc_gate import parse_drc_html

FOLDER = ROOT / 'docs/validation/altium-redesign/ba00d483efd34f16a50205b6a3e08c08'
PIN = '0d989fd1f7c9ba102dec034a271b04fc3de93de294c074761ac6dd488e8e7b5f'
SUPPORT_PINS = {
    'redesign.json': 'a4007b1ff0fe166a0c53cd9bde1879806f403576b3620ffcc5cb5576a085d701',
    'manifest.json': '6204b87f548cfdbc83dcc4350fc3bdd674b345abb7473c87c223a358119ff92a',
    'batch-effective.json': 'd64a79ceebee3c941d788cc729428981c911831cfb6ffe28ecaad69d4841b3f4',
    'report-options-effective.json': '24772eecec2a00d0b7d6b50ac882eff4153b8c7c6f8388e3bf7448d58971de8b',
}


def snapshot(include_findings=False):
    baseline_snapshot()
    raw = _read(FOLDER / 'silk-result.json')
    if _sha(raw) != PIN:
        raise ValueError('Pinned silk evidence changed')
    for name, expected in SUPPORT_PINS.items():
        if _sha(_read(FOLDER / name)) != expected:
            raise ValueError('Pinned supporting evidence changed: ' + name)
    record = json.loads(raw)
    board = FOLDER / 'WiFi.PcbDoc'
    if (record['status'] != 'completed' or record['local_repair_accepted'] is not True
            or record['board'] != str(board) or record['project'] != str(FOLDER / 'WiFi_Controller_B.PrjPcb')):
        raise ValueError('Repair identity mismatch')
    for path, expected in [(board, record['board_sha256']),
                           (FOLDER / 'silk-response.ini', record['response_sha256']),
                           (FOLDER / 'native-drc.html', record['report_sha256']),
                           (FOLDER / 'SilkRepair.pas', record['script_sha256'])]:
        if _sha(_read(path)) != expected:
            raise ValueError('Repair artifact changed: ' + path.name)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(_read(FOLDER / 'silk-response.ini').decode('utf-8-sig'))
    native = {s: dict(parser[s]) for s in parser.sections()}
    if native != record['native'] or native['drc']['native_return'] != 'False':
        raise ValueError('Native response mismatch')
    objects = verify_objects(native)
    report = parse_drc_html(_read(FOLDER / 'native-drc.html'), board)
    baseline = json.loads(_read(BASELINE / 'native-drc.json'))['drc_report']
    delta = verify_drc(baseline, report)
    for name in ('batch-effective.json', 'report-options-effective.json'):
        if json.loads(_read(FOLDER / name).decode('utf-8-sig')) != json.loads(_read(BASELINE / name).decode('utf-8-sig')):
            raise ValueError('DRC settings changed')
    contract = json.loads(_read(FOLDER / 'redesign.json'))
    if _sha(_read(Path(record['project']))) != contract['project_sha256']:
        raise ValueError('Project changed')
    manifest = json.loads(_read(FOLDER / 'manifest.json'))
    for row in manifest['files']:
        if row['relative_path'] != 'WiFi.PcbDoc' and _sha(_read(Path(row['copy']))) != row['copy_sha256']:
            raise ValueError('Dependency changed')
    result = {'scope': 'pinned_local_silkscreen_repair_not_live', 'local_repair_accepted': True,
              'project': record['project'], 'board': str(board),
              'before_violations': 159, 'after_violations': 154, 'rule_delta': delta,
              'verified_objects': objects, 'native_drc_pass': False,
              'manufacturing_release': False, 'signal_integrity_verified': False,
              'remaining_rules': [r for r in report['rule_rows'] if r['count']]}
    if include_findings:
        result['findings'] = report['violation_details']
    return result
