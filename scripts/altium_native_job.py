"""Developer-only sequential Altium jobs on immutable fresh example copies."""
import argparse
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('D:/Altium/AD26-Examples/Examples/Mini PC/Mini PC - WiFi/WiFi.PcbDoc')
EXPECTED = 'ec13cc84307c6393263624a1a2f4c10ddb767640b2860c3e71e655098a87e031'


def _run(mode):
    folder = ROOT / 'docs/validation/altium-native' / uuid.uuid4().hex
    folder.mkdir(parents=True)
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    if source_sha != EXPECTED:
        raise ValueError('Bundled source changed')
    board = folder / 'WiFi.PcbDoc'
    shutil.copy2(SOURCE, board)
    template = (ROOT / 'scripts/altium' / (mode + '.pas.template')).read_text()
    report = folder / 'response.ini'
    request = uuid.uuid4().hex
    for key, value in {'REQUEST': request, 'BOARD': str(board), 'REPORT': str(report),
                       'OUTPUT': str(folder / 'export.dsn'), 'DRC': str(folder / 'native-drc.html')}.items():
        template = template.replace('@@' + key + '@@', value.replace("'", "''"))
    if '@@' in template:
        raise ValueError('Unresolved template')
    (folder / 'Job.pas').write_text(template, encoding='ascii')
    project = folder / 'Job.PrjScr'
    project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Job.pas\n')
    print(str(folder), flush=True)
    result = {'status': 'running', 'request': request, 'mode': mode, 'source': str(SOURCE),
              'source_sha256': source_sha, 'board': str(board)}
    try:
        subprocess.Popen(f'"D:\\Altium\\AD26\\X2.EXE" -RScriptingSystem:RunScript(ProjectName="{project}"|ProcName="Job.pas>Run")')
        deadline = time.monotonic() + (600 if mode in ('DRCSetup', 'StubRepair') else 120)
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        if not report.exists():
            raise TimeoutError('Native timeout; manually inspect before any further dispatch')
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(report, encoding='utf-8-sig')
        if parser['job']['request'] != request or parser['completion']['status'] != 'completed':
            raise ValueError('Incomplete or mismatched native result')
        if Path(parser['job']['board']).resolve() != board.resolve():
            raise ValueError('Wrong native board identity')
        result['native'] = {section: dict(parser[section]) for section in parser.sections()}
        if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != source_sha:
            raise ValueError('Original example changed')
        result.update(status='completed', source_unchanged=True)
        result['source_snapshot_sha256'] = hashlib.sha256(board.read_bytes()).hexdigest()
        result['template_sha256'] = hashlib.sha256(template.encode('ascii')).hexdigest()
        result['response_sha256'] = hashlib.sha256(report.read_bytes()).hexdigest()
        result['artifacts'] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.iterdir() if p.suffix.lower() in ('.dsn', '.html')}
        print(json.dumps({'status': result['status'], 'sections': len(parser.sections())}), flush=True)
    except Exception as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return folder


def run(mode):
    if mode not in ('Inventory', 'Export', 'DRC', 'DRCAll', 'DRCNegative', 'Constraints', 'Geometry', 'DRCSetup', 'Copper', 'StubRepair'):
        raise ValueError('Unknown native action')
    base = ROOT / 'docs/validation/altium-native'
    base.mkdir(parents=True, exist_ok=True)
    pending = base / 'pending.json'
    # A timeout can leave an Altium modal or late script alive. Never auto-clear it.
    with pending.open('x', encoding='utf-8') as handle:
        json.dump({'mode': mode, 'started': time.time(), 'recovery': 'manual_native_inspection_required'}, handle)
    result = _run(mode)
    pending.unlink()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['Inventory', 'Export', 'DRC', 'DRCAll', 'DRCNegative', 'Constraints', 'Geometry', 'DRCSetup', 'Copper', 'StubRepair'])
    run(parser.parse_args().mode)
