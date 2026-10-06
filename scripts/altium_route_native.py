"""Run isolated Altium route probes with a verifiable native response."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / 'docs/validation/altium-route-v1'
SOURCE = Path('D:/Altium/MCP-Test/bridge-test.PcbDoc')
SOURCE_SHA256 = '2658b6ffa4eb826c577379578a7fc303bbe7f96170ce079bc88fe1ac74b5409a'
EXE = Path('D:/Altium/AD26/X2.EXE')


def require_closed_altium():
    if os.name != 'nt':
        return
    check = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'if (Get-Process -Name X2 -ErrorAction SilentlyContinue) { exit 2 } else { exit 0 }'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
    )
    if check.returncode == 2:
        raise RuntimeError('Close all Altium X2.EXE instances before a native route probe')
    if check.returncode != 0:
        raise RuntimeError('Could not verify that all Altium instances are closed')


def require_open_altium():
    if os.name != 'nt':
        raise RuntimeError('Current-instance Altium dispatch requires Windows')
    check = subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'if (Get-Process -Name X2 -ErrorAction SilentlyContinue) { exit 2 } else { exit 0 }'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
    )
    if check.returncode != 2:
        raise RuntimeError('Current-instance dispatch requires an open Altium X2.EXE')


def validate_completion(mode, before, after, status, values, native):
    if mode == 'Import':
        raise ValueError('Altium route import is not implemented or acceptance-tested')
    if status != 'completed':
        raise ValueError('Altium did not report a completed native operation')
    if mode == 'Fixture':
        if before == after:
            raise ValueError('Fixture reported completion without changing the copied PCB')
    elif before != after:
        raise ValueError('Read-only native operation changed the copied PCB')
    if mode == 'Inspect':
        inventory = native.get('job', {})
        for key in ('components', 'pads', 'nets', 'tracks', 'vias', 'polygons'):
            value = inventory.get(key, '')
            if not value.isdecimal():
                raise ValueError(f'Native inventory is missing {key}')
    if mode == 'Export':
        output = values.get('OUTPUT')
        if not output or not Path(output).is_file() or Path(output).stat().st_size == 0:
            raise ValueError('Altium did not produce a nonempty DSN')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare():
    if sha(SOURCE) != SOURCE_SHA256:
        raise ValueError('Original blank PCB changed')
    folder = RUN_ROOT / 'fixture'
    folder.mkdir(parents=True, exist_ok=True)
    board = folder / 'RouteDemo.PcbDoc'
    if board.exists():
        if sha(board) != SOURCE_SHA256:
            raise ValueError('Existing route fixture has been modified')
    else:
        shutil.copy2(SOURCE, board)
    return board


def run(mode, board, values=None, timeout=180, dispatch='cli'):
    if dispatch not in ('cli', 'current'):
        raise ValueError('Unknown Altium dispatch mode')
    if not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise ValueError('Native response timeout must be 1-900 seconds')
    if mode == 'Import':
        raise ValueError('Altium route import is not implemented or acceptance-tested')
    board = Path(board).resolve(strict=True)
    if not board.is_relative_to(RUN_ROOT.resolve()) or board.suffix.lower() != '.pcbdoc':
        raise ValueError('Route probe may only edit a copied local PcbDoc')
    if mode == 'Fixture' and sha(board) != SOURCE_SHA256:
        raise ValueError('Fixture creation requires the untouched blank PCB copy')
    values = values or {}
    if mode == 'Export':
        output = values.get('OUTPUT')
        if not output or Path(output).suffix.lower() != '.dsn':
            raise ValueError('Export requires a DSN output path')
        if Path(output).exists():
            raise ValueError('Export output must be new for this native run')
    if not EXE.is_file():
        raise ValueError('Altium executable is missing')
    if dispatch == 'cli':
        require_closed_altium()
    else:
        require_open_altium()
    template = ROOT / 'scripts/altium' / f'Route{mode}.pas.template'
    if not template.is_file():
        raise ValueError('Unknown route probe mode')
    job = RUN_ROOT / 'native' / uuid.uuid4().hex
    job.mkdir(parents=True)
    request = uuid.uuid4().hex
    response = job / 'response.ini'
    replacements = {'BOARD': str(board), 'REQUEST': request, 'REPORT': str(response), **values}
    for key in ('INPUT', 'OUTPUT'):
        if key in replacements and not Path(replacements[key]).resolve().is_relative_to(RUN_ROOT.resolve()):
            raise ValueError('Native route artifact must remain inside the run root')
    text = template.read_text(encoding='utf-8')
    for key, value in replacements.items():
        value = str(value)
        if any(c in value for c in '\r\n|'):
            raise ValueError('Unsafe native script value')
        text = text.replace('@@' + key + '@@', value.replace("'", "''"))
    if '@@' in text:
        raise ValueError('Unresolved route template placeholder')
    script = job / 'Job.pas'
    script.write_bytes(text.encode('mbcs'))
    project = job / 'Job.PrjScr'
    project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Job.pas\n', encoding='ascii')
    lock = ROOT / 'docs/validation/altium-native/pending.json'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('x', encoding='utf-8') as stream:
        json.dump({'mode': 'AltiumRoute' + mode, 'request': request, 'job': str(job),
                   'started': time.time(), 'recovery': 'inspect Altium and response.ini'}, stream)
    before = sha(board)
    result = {'status': 'running', 'mode': mode, 'board': str(board), 'before_sha256': before,
              'request': request, 'job': str(job), 'response': str(response), 'dispatch': dispatch}
    completed = False
    try:
        if dispatch == 'cli':
            process = subprocess.Popen(f'"{EXE}" -RScriptingSystem:RunScript(ProjectName="{project}"|ProcName="Job.pas>Run")')
            result['pid'] = process.pid
        deadline = time.monotonic() + timeout
        while not response.exists() and time.monotonic() < deadline:
            time.sleep(.5)
        if not response.exists():
            raise TimeoutError('No native response; Altium requires manual inspection')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read(response, encoding='utf-8-sig')
        if parser.get('job', 'request', fallback='') != request or parser.get('job', 'board', fallback='') != str(board):
            raise ValueError('Native response identity mismatch')
        result['native'] = {section: dict(parser[section]) for section in parser.sections()}
        result['response_sha256'] = sha(response)
        result['after_sha256'] = sha(board)
        result['status'] = parser.get('completion', 'status', fallback='failed')
        validate_completion(mode, before, result['after_sha256'], result['status'], values, result['native'])
        completed = True
    except Exception as error:
        result.update(status='failed', error=str(error))
        raise
    finally:
        if board.exists() and 'after_sha256' not in result:
            result['after_sha256'] = sha(board)
        (job / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if completed:
            lock.unlink()
    return result


def reconcile_inspect(job):
    job = Path(job).resolve(strict=True)
    if not job.is_relative_to((RUN_ROOT / 'native').resolve()):
        raise ValueError('Route inspection job must be inside the native run root')
    result_path = job / 'result.json'
    result = json.loads(result_path.read_text(encoding='utf-8'))
    if (result.get('job') != str(job) or result.get('mode') != 'Inspect'
            or result.get('dispatch') != 'current' or result.get('status') != 'failed'
            or result.get('error') != 'No native response; Altium requires manual inspection'):
        raise ValueError('Job is not a timed-out current-instance inspection')
    board = Path(result['board']).resolve(strict=True)
    if not board.is_relative_to(RUN_ROOT.resolve()) or board.suffix.lower() != '.pcbdoc':
        raise ValueError('Inspection board is outside the isolated run root')
    response = job / 'response.ini'
    if result.get('response') != str(response):
        raise ValueError('Native response path mismatch')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    if not parser.read(response, encoding='utf-8-sig'):
        raise ValueError('Native response is missing')
    if (parser.get('job', 'request', fallback='') != result['request']
            or parser.get('job', 'board', fallback='') != str(board)):
        raise ValueError('Native response identity mismatch')
    native = {section: dict(parser[section]) for section in parser.sections()}
    after = sha(board)
    status = parser.get('completion', 'status', fallback='')
    validate_completion('Inspect', result['before_sha256'], after, status, {}, native)
    result['orchestration_error'] = result.pop('error')
    result.update(status=status, orchestration_status='timed_out', late_response=True,
                  native=native, response_sha256=sha(response), after_sha256=after)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=['prepare', 'Inspect', 'Fixture', 'Export', 'Import', 'ReconcileInspect'])
    ap.add_argument('--board')
    ap.add_argument('--input')
    ap.add_argument('--output')
    ap.add_argument('--job')
    ap.add_argument('--current-instance', action='store_true')
    ap.add_argument('--timeout', type=int, default=180)
    args = ap.parse_args()
    if args.mode == 'prepare':
        print(json.dumps({'board': str(prepare())}))
    elif args.mode == 'ReconcileInspect':
        if not args.job:
            ap.error('ReconcileInspect requires --job')
        print(json.dumps(reconcile_inspect(args.job), ensure_ascii=False))
    else:
        board = args.board or str(prepare())
        values = {}
        if args.input:
            values['INPUT'] = str(Path(args.input).resolve(strict=True))
        if args.output:
            values['OUTPUT'] = str(Path(args.output).resolve())
        print(json.dumps(run(args.mode, board, values, timeout=args.timeout,
                             dispatch='current' if args.current_instance else 'cli'), ensure_ascii=False))
