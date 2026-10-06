"""Native, read-only compilation of a newly created route-B revision."""
import argparse
import configparser
import json
from pathlib import Path
import subprocess
import time
import uuid

from altium_project_snapshot import ROOT, _read, _sha, _write_new


def validate_revision(folder):
    folder = Path(folder).absolute()
    root = ROOT / 'docs/validation/altium-redesign'
    if folder.parent != root:
        raise ValueError('Requires direct child of redesign evidence root')
    contract = json.loads(_read(folder / 'redesign.json'))
    project, board = folder / 'WiFi_Controller_B.PrjPcb', folder / 'WiFi.PcbDoc'
    if contract['route'] != 'B' or contract['project'] != str(project) or contract['board'] != str(board):
        raise ValueError('Revision identity mismatch')
    if _sha(_read(project)) != contract['project_sha256'] or _sha(_read(board)) != contract['board_sha256']:
        raise ValueError('Revision hash mismatch')
    manifest = json.loads(_read(folder / 'manifest.json'))
    hashes = {project: contract['project_sha256']}
    for row in manifest['files']:
        name = row['relative_path']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Non-local dependency')
        path = folder / name
        if _sha(_read(path)) != row['copy_sha256']:
            raise ValueError('Dependency hash mismatch')
        hashes[path] = row['copy_sha256']
    return folder, contract, hashes


def run(folder, with_drc=False):
    folder, contract, hashes = validate_revision(folder)
    prefix = 'native-drc' if with_drc else 'native-compile'
    result_path = folder / (prefix + '.json')
    if result_path.exists():
        raise ValueError('Native result already exists; do not overwrite')
    pending = ROOT / 'docs/validation/altium-native/pending.json'
    request = uuid.uuid4().hex
    response = folder / (prefix + '.ini')
    template = _read(ROOT / 'scripts/altium/ProjectCompile.pas.template').decode()
    values = {'REQUEST': request, 'PROJECT': contract['project'], 'BOARD': contract['board'],
              'SCHEMATIC': str(folder / 'Connector_WiFi.SchDoc'), 'REPORT': str(response),
              'DRC_BLOCK': ''}
    block = """ResetParameters;
        RunProcess('PCB:DesignRuleCheck');
        ResetParameters;
        Ok := Board.RunBatchDesignRuleCheck('@@DRC@@', eDRC_HTML, False, False);
        Report.Add('native_drc_return=' + BoolToStr(Ok, True));
        If Not FileExists('@@DRC@@') Then Exit;""" if with_drc else ''
    template = template.replace('@@DRC_BLOCK@@', block)
    values['DRC'] = str(folder / 'native-drc.html')
    for key, value in values.items():
        template = template.replace('@@' + key + '@@', value.replace("'", "''"))
    if '@@' in template:
        raise ValueError('Unresolved native template')
    script_name = 'NativeDRC' if with_drc else 'NativeCompile'
    script = folder / (script_name + '.PrjScr')
    _write_new(folder / (script_name + '.pas'), template.encode('ascii'))
    _write_new(script, f'[Design]\nVersion=1.0\n[Document1]\nDocumentPath={script_name}.pas\n'.encode())
    _write_new(pending, json.dumps({'mode': 'RedesignCompile', 'request': request,
                                  'folder': str(folder), 'recovery': 'manual_native_inspection_required'}).encode())
    result = {'status': 'failed', 'request': request, 'project': contract['project'],
              'native_compile': False, 'native_drc_pass': False, 'manufacturing_release': False}
    print(str(folder), flush=True)
    try:
        subprocess.Popen(f'"D:\\Altium\\AD26\\X2.EXE" -RScriptingSystem:RunScript(ProjectName="{script}"|ProcName="{script_name}.pas>Run")')
        deadline = time.monotonic() + (600 if with_drc else 180)
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(.5)
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(_read(response).decode('utf-8-sig'))
        job = parser['job']
        if (job['request'] != request or job['compile_return'] != 'True'
                or job['flattened_available'] != 'True'
                or job['pcb_project'] != contract['project']
                or job['board'] != contract['board']
                or parser['completion']['status'] != 'completed'):
            raise ValueError('Native identity or compilation check failed')
        for path, expected in hashes.items():
            if _sha(_read(path)) != expected:
                raise ValueError('Document changed during compile: ' + str(path))
        result.update(status='completed', native_compile=True, files_unchanged=True,
                      response_sha256=_sha(_read(response)),
                      native={s: dict(parser[s]) for s in parser.sections()})
        if with_drc:
            from altium_drc_gate import parse_drc_html
            raw = _read(folder / 'native-drc.html')
            result.update(drc_report=parse_drc_html(raw, Path(contract['board'])),
                          report_sha256=_sha(raw),
                          native_drc_return=job['native_drc_return'],
                          full_rule_coverage_verified=False,
                          signal_integrity_verified=False)
    except Exception as error:
        result['error'] = str(error)
        raise
    finally:
        _write_new(result_path, (json.dumps(result, indent=2) + '\n').encode())
    pending.unlink()
    print(json.dumps(result, indent=2), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--drc', action='store_true')
    args = parser.parse_args()
    run(args.folder, args.drc)
