"""Read pinned route-B baseline evidence, never authorize production."""
import json

from altium_project_snapshot import ROOT, _read, _sha
from altium_redesign_compile import validate_revision
from altium_drc_gate import parse_drc_html

FOLDER = ROOT / 'docs/validation/altium-redesign/eb8ce77bbff14657b2c8ee7a040fd0b4'
PINS = {
    'native-drc.json': '78101fd808ad9dbedb058d8cd2aaf9951bd9df492f787646f04f06ad6c5b0819',
    'native-compile.json': 'c3f27027dcacd33bc570b39fb9f8cabe3b7fc158441af514d71a5f44c9eb2672',
    'redesign.json': '7bf0929163f54a421d58c6d93eae6cd52e6db1f65eb6d232fd73474e2e24cfb1',
}


def snapshot(include_findings=False):
    data = {}
    for name, expected in PINS.items():
        raw = _read(FOLDER / name)
        if _sha(raw) != expected:
            raise ValueError('Pinned redesign evidence changed: ' + name)
        data[name] = json.loads(raw)
    _, contract, _ = validate_revision(FOLDER)
    drc = data['native-drc.json']
    for prefix in ('native-compile', 'native-drc'):
        record = data[prefix + '.json']
        if (record['status'] != 'completed' or not record['native_compile']
                or record['project'] != contract['project']
                or _sha(_read(FOLDER / (prefix + '.ini'))) != record['response_sha256']):
            raise ValueError('Invalid native response binding')
    raw = _read(FOLDER / 'native-drc.html')
    if _sha(raw) != drc['report_sha256']:
        raise ValueError('DRC report changed')
    from pathlib import Path
    parsed = parse_drc_html(raw, Path(contract['board']))
    if parsed != drc['drc_report'] or parsed['counts']['violations'] != 159:
        raise ValueError('DRC evidence mismatch')
    result = {
        'scope': 'pinned_route_b_baseline_not_live', 'status': 'redesign_required',
        'project': contract['project'], 'board': contract['board'],
        'native_compile': True, 'native_drc_pass': False,
        'counts': parsed['counts'], 'rule_rows': parsed['rule_rows'],
        'placement_modified': False, 'routing_modified_in_this_revision': False,
        'signal_integrity': 'warnings_observed_not_qualified',
        'manufacturing_release': False, 'requirements': contract['requirements'],
    }
    if include_findings:
        result['rule_details'] = parsed['violation_details']
    return result
