"""Supervised native compile of a complete fresh project copy; not SI acceptance."""
import argparse
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from altium_project_snapshot import snapshot_project

ROOT = Path(__file__).resolve().parents[1]


def run(with_drc=False, repaired_board=False):
    pending = ROOT / 'docs/validation/altium-native/pending.json'
    with pending.open('x', encoding='utf-8') as handle:
        json.dump({'mode': 'ProjectDRC' if with_drc else 'ProjectCompile', 'started': time.time(),
                   'recovery': 'manual_native_inspection_required'}, handle)
    manifest = snapshot_project()
    if manifest['status'] != 'completed':
        raise ValueError('Project snapshot failed')
    project = Path(next(row['copy'] for row in manifest['files'] if row['role'] == 'project'))
    folder = project.parent
    repair = None
    repaired_sha = None
    if repaired_board:
        from accept_altium_repair import accept
        repair = accept()
        if repair['repair_acceptance'] != 'passed':
            raise ValueError('Local repair evidence rejected')
        repaired = Path(repair['board'])
        repaired_sha = hashlib.sha256(repaired.read_bytes()).hexdigest()
        if repaired_sha != '1715160bd08abbc1c649b719a8f4bbfe67e976e80e1aca2537f8edf8c0d568a4':
            raise ValueError('Repaired board changed')
        shutil.copy2(repaired, folder / 'WiFi.PcbDoc')
    request = uuid.uuid4().hex
    response = folder / 'compile-response.ini'
    template = (ROOT / 'scripts/altium/ProjectCompile.pas.template').read_text()
    drc_block = """ResetParameters;
        RunProcess('PCB:DesignRuleCheck');
        ResetParameters;
        Ok := Board.RunBatchDesignRuleCheck('@@DRC@@', eDRC_HTML, False, False);
        Report.Add('native_drc_return=' + BoolToStr(Ok, True));
        If Not FileExists('@@DRC@@') Then Exit;""" if with_drc else ''
    template = template.replace('@@DRC_BLOCK@@', drc_block)
    values = {'PROJECT': str(project), 'BOARD': str(folder / 'WiFi.PcbDoc'),
              'SCHEMATIC': str(folder / 'Connector_WiFi.SchDoc'), 'REQUEST': request,
              'REPORT': str(response), 'DRC': str(folder / 'native-drc.html')}
    for key, value in values.items():
        template = template.replace('@@' + key + '@@', value.replace("'", "''"))
    if '@@' in template:
        raise ValueError('Unresolved native template')
    (folder / 'Compile.pas').write_text(template, encoding='ascii')
    script_project = folder / 'Compile.PrjScr'
    script_project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Compile.pas\n', encoding='ascii')
    result = {'status': 'running', 'request': request, 'project': str(project),
              'board': values['BOARD'], 'scope': 'native_project_binding_and_compile_not_si',
              'drc_requested': with_drc}
    result.update(board_variant='accepted_local_gnd_repair' if repaired_board else 'original_example',
                  repaired_board_sha256=repaired_sha,
                  repair_evidence=repair['candidate'] if repair else None)
    print(str(folder), flush=True)
    try:
        subprocess.Popen(f'"D:\\Altium\\AD26\\X2.EXE" -RScriptingSystem:RunScript(ProjectName="{script_project}"|ProcName="Compile.pas>Run")')
        deadline = time.monotonic() + (600 if with_drc else 180)
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        if not response.exists():
            raise TimeoutError('Native compile timeout; inspect before retry')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read(response, encoding='utf-8-sig')
        result['native'] = {section: dict(parser[section]) for section in parser.sections()}
        if (parser['job']['request'] != request or parser['completion']['status'] != 'completed'
                or parser['job']['compile_return'] != 'True'
                or Path(parser['job']['pcb_project']).resolve() != project.resolve()):
            raise ValueError('Incomplete or mismatched project compilation')
        for row in manifest['files']:
            for key in ('source', 'copy'):
                expected_sha = repaired_sha if repaired_board and key == 'copy' and row['relative_path'] == 'WiFi.PcbDoc' else row['source_sha256']
                if hashlib.sha256(Path(row[key]).read_bytes()).hexdigest() != expected_sha:
                    raise ValueError('Source or snapshot document changed: ' + row['relative_path'])
        result.update(status='completed', source_and_snapshot_unchanged=not repaired_board,
                      source_unchanged=True, snapshot_unchanged_since_compile=True,
                      response_sha256=hashlib.sha256(response.read_bytes()).hexdigest(),
                      manifest_sha256=hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest(),
                      signal_integrity_verified=False)
        if with_drc:
            result['report_sha256'] = hashlib.sha256((folder / 'native-drc.html').read_bytes()).hexdigest()
    except Exception as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        (folder / 'compile-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    pending.unlink()
    print(json.dumps(result), flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--drc', action='store_true')
    parser.add_argument('--repaired-board', action='store_true')
    args = parser.parse_args()
    run(args.drc, args.repaired_board)
