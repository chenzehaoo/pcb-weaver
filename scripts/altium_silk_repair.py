"""Supervised R11 silkscreen-only candidate with native save/reopen and DRC."""
from collections import Counter
import configparser
import json
from pathlib import Path
import subprocess
import time
import uuid

from altium_project_snapshot import ROOT, _read, _sha, _write_new
from altium_redesign_revision import create_revision
from altium_redesign_status import snapshot, FOLDER as BASELINE
from altium_drc_gate import parse_drc_html


def verify_objects(native):
    def rows(phase, kind):
        prefix = phase + '.' + kind + '.'
        return [v for k, v in native.items() if k.startswith(prefix)]
    def counts(items):
        return Counter(tuple(sorted(v.items())) for v in items)
    for kind, total in [('track', 579), ('pad', 200)]:
        before, after = rows('before', kind), rows('after', kind)
        if len(before) != total or len(after) != total or counts(before) != counts(after):
            raise ValueError('Unexpected copper change: ' + kind)
    before, after = rows('before', 'component'), rows('after', 'component')
    if len(before) != 28 or len(after) != 28:
        raise ValueError('Component count mismatch')
    if len({v['name'] for v in before}) != 28 or len({v['name'] for v in after}) != 28:
        raise ValueError('Duplicate component')
    old = {v['name']: v for v in before}
    new = {v['name']: v for v in after}
    if old.keys() != new.keys() or 'R11' not in old:
        raise ValueError('Component identity mismatch')
    for name in old:
        a, b = dict(old[name]), dict(new[name])
        if name == 'R11':
            for key in ('text_x', 'text_y'):
                a.pop(key)
                b.pop(key)
        if a != b:
            raise ValueError('Unexpected component/text change: ' + name)
    target = new['R11']
    if abs(int(target['text_x']) * .00000254 - 110.8) > .000003 or abs(int(target['text_y']) * .00000254 - 121.0) > .000003:
        raise ValueError('Text position did not survive save/reopen')
    if old['R11'] == target or target['text_visible'] != 'True':
        raise ValueError('Text was not moved visibly')
    return {'components': 28, 'pads': 200, 'tracks': 579,
            'before': old['R11'], 'after': target,
            'scope': 'enumerated_fields_only_not_full_board_equivalence'}


def verify_drc(before, after):
    a = {r['description']: r['count'] for r in before['rule_rows']}
    b = {r['description']: r['count'] for r in after['rule_rows']}
    if len(a) != len(before['rule_rows']) or len(b) != len(after['rule_rows']) or a.keys() != b.keys():
        raise ValueError('Rule identity mismatch')
    delta = {k: b[k] - a[k] for k in a if b[k] != a[k]}
    if (before['counts']['violations'] != 159 or after['counts']['violations'] != 154
            or len(delta) != 2 or sorted(delta.values()) != [-4, -1]
            or not all('Silk' in k for k in delta)):
        raise ValueError('Candidate did not remove exactly five silk findings')
    for key in ('warnings', 'health_issues', 'waived'):
        if after['counts'][key] != before['counts'][key]:
            raise ValueError('Unexpected DRC coverage/count change: ' + key)
    return delta


def run():
    snapshot()
    pending = ROOT / 'docs/validation/altium-native/pending.json'
    request = uuid.uuid4().hex
    _write_new(pending, json.dumps({'mode': 'SilkRepair', 'request': request,
                                  'recovery': 'manual_native_inspection_required'}).encode())
    contract = create_revision()
    folder = Path(contract['project']).parent
    response = folder / 'silk-response.ini'
    report = folder / 'native-drc.html'
    template_path = ROOT / 'scripts/altium/SilkRepair.pas.template'
    template = _read(template_path).decode()
    for key, value in {'REQUEST': request, 'PROJECT': contract['project'], 'BOARD': contract['board'],
                       'SCHEMATIC': str(folder / 'Connector_WiFi.SchDoc'),
                       'REPORT': str(response), 'DRC': str(report)}.items():
        template = template.replace('@@' + key + '@@', value.replace("'", "''"))
    if '@@' in template:
        raise ValueError('Unresolved template')
    _write_new(folder / 'SilkRepair.pas', template.encode('ascii'))
    script = folder / 'SilkRepair.PrjScr'
    _write_new(script, b'[Design]\nVersion=1.0\n[Document1]\nDocumentPath=SilkRepair.pas\n')
    manifest = json.loads(_read(folder / 'manifest.json'))
    result = {'status': 'failed', 'local_repair_accepted': False, 'request': request,
              'project': contract['project'], 'board': contract['board'],
              'native_drc_pass': False, 'manufacturing_release': False,
              'signal_integrity_verified': False, 'full_board_equivalence': False,
              'baseline': str(BASELINE)}
    print(str(folder), flush=True)
    try:
        subprocess.Popen(f'"D:\\Altium\\AD26\\X2.EXE" -RScriptingSystem:RunScript(ProjectName="{script}"|ProcName="SilkRepair.pas>Run")')
        deadline = time.monotonic() + 600
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(.5)
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(_read(response).decode('utf-8-sig'))
        native = {s: dict(parser[s]) for s in parser.sections()}
        result['native'] = native
        if (native['job']['request'] != request or native['job']['board'] != contract['board']
                or native['job']['project'] != contract['project'] or native['job']['compile_return'] != 'True'
                or native['completion']['status'] != 'completed'):
            raise ValueError('Incomplete native repair')
        result['objects'] = verify_objects(native)
        before = json.loads(_read(BASELINE / 'native-drc.json'))['drc_report']
        after = parse_drc_html(_read(report), Path(contract['board']))
        result['drc_report'] = after
        result['rule_delta'] = verify_drc(before, after)
        for name in ('batch-effective.json', 'report-options-effective.json'):
            if json.loads(_read(folder / name).decode('utf-8-sig')) != json.loads(_read(BASELINE / name).decode('utf-8-sig')):
                raise ValueError('Native check options differ: ' + name)
        for row in manifest['files']:
            if _sha(_read(Path(row['source']))) != row['source_sha256']:
                raise ValueError('Source changed')
            if row['relative_path'] != 'WiFi.PcbDoc' and _sha(_read(Path(row['copy']))) != row['copy_sha256']:
                raise ValueError('Non-PCB dependency changed')
        if _sha(_read(Path(contract['project']))) != contract['project_sha256']:
            raise ValueError('Project changed')
        result.update(status='completed', local_repair_accepted=True, source_unchanged=True,
                      board_sha256=_sha(_read(Path(contract['board']))),
                      response_sha256=_sha(_read(response)), report_sha256=_sha(_read(report)),
                      script_sha256=_sha(_read(folder / 'SilkRepair.pas')))
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        _write_new(folder / 'silk-result.json', (json.dumps(result, indent=2) + '\n').encode())
    pending.unlink()
    print(json.dumps({k: v for k, v in result.items() if k not in {'native', 'drc_report'}}), flush=True)
    return result


if __name__ == '__main__':
    run()
