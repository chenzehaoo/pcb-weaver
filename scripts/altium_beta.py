"""Local, read-only Altium developer beta for explicitly allowed project folders."""
import configparser
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import time
import uuid

from altium_project_snapshot import ROOT, _no_links, _read, _sha, _write_new
from altium_drc_gate import parse_drc_html


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _inside(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    _no_links(path)
    _no_links(root)
    _require(path == root or path.is_relative_to(root), 'Path outside allowed roots')
    return path


def _parts(value):
    _require(isinstance(value, str) and value and value.strip() == value, 'Invalid document path')
    win = PureWindowsPath(value)
    _require(not win.drive and not win.root and not any(c in value for c in '/:<>"|?*')
             and not any(ord(c) < 32 for c in value), 'Document path is not relative')
    parts = win.parts
    _require(bool(parts) and len(parts) <= 8, 'Document path depth unsupported')
    for part in parts:
        _require(part not in {'.', '..'} and part == part.strip() and not part.endswith('.')
                 and not re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I),
                 'Unsafe document path')
    return parts


def _decode_native(raw):
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('mbcs')


def project_documents(raw):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(_decode_native(raw))
    _require(not parser.defaults(), 'Inherited project options unsupported')
    indexed = {}
    for section in parser.sections():
        if 'documentpath' not in parser[section]:
            continue
        match = re.fullmatch(r'Document([1-9][0-9]*)', section)
        _require(match is not None, 'DocumentPath outside numbered document section')
        number = int(match.group(1))
        _require(number not in indexed, 'Duplicate document number')
        indexed[number] = parser[section]['documentpath']
    _require(1 <= len(indexed) <= 64 and set(indexed) == set(range(1, len(indexed) + 1)),
             'Requires 1-64 contiguous documents')
    names = [indexed[i] for i in sorted(indexed)]
    keys = [tuple(p.casefold() for p in _parts(name)) for name in names]
    _require(len(keys) == len(set(keys)), 'Duplicate document path')
    boards = [name for name in names if name.lower().endswith('.pcbdoc')]
    schematics = [name for name in names if name.lower().endswith('.schdoc')]
    _require(bool(boards) and bool(schematics), 'Requires a PCB and a schematic')
    return names, boards, schematics


class Beta:
    def __init__(self, roots=None, run_root=None, exe=None):
        roots = roots if roots is not None else [p for p in os.environ.get('ALTIUM_BETA_PROJECT_ROOTS', '').split(os.pathsep) if p]
        self.roots = [Path(p).absolute() for p in roots]
        self.run_root = Path(run_root or os.environ.get('ALTIUM_BETA_RUN_ROOT', ROOT / 'docs/validation/altium-beta')).absolute()
        self.exe = Path(exe or os.environ.get('ALTIUM_BETA_EXE', '')).absolute()
        self.lock = ROOT / 'docs/validation/altium-native/pending.json'
        _require(bool(self.roots), 'Configure ALTIUM_BETA_PROJECT_ROOTS')
        for root in self.roots:
            _no_links(root)
            _require(root.is_dir(), 'Missing project root: ' + str(root))
        _no_links(self.run_root)
        _require(not any(self.run_root.is_relative_to(r) or r.is_relative_to(self.run_root) for r in self.roots),
                 'Evidence root must be independent of project roots')

    def ready(self):
        return {'protocol': 'stdio', 'scope': 'local_read_only_developer_beta',
                'project_roots': [str(r) for r in self.roots], 'evidence_root': str(self.run_root),
                'altium_available': self.exe.is_file(), 'native_busy_or_uncertain': self.lock.exists(),
                'automatic_routing': False, 'manufacturing_release': False,
                'drc_coverage': 'native_selected_batch_rules_only; full coverage unverified'}

    def _source(self, project):
        path = Path(project)
        _require(path.is_absolute() and path.suffix.lower() == '.prjpcb', 'Expected absolute PrjPcb path')
        _require(any(path.is_relative_to(r) for r in self.roots), 'Project outside allowed roots')
        _no_links(path)
        _require(path.is_file(), 'Project not found')
        _require(path.stat().st_size <= 4 * 1024 * 1024, 'Project file too large')
        return path

    def inspect(self, project, board_path=None):
        source = self._source(project)
        raw = _read(source)
        names, boards, schematics = project_documents(raw)
        if board_path is None:
            _require(len(boards) == 1, 'Multiple PCBs; specify board_path')
            board = boards[0]
        else:
            _require(isinstance(board_path, str) and board_path.casefold() in {b.casefold() for b in boards},
                     'Board not a project document')
            board = next(b for b in boards if b.casefold() == board_path.casefold())
        _require(source.name.casefold() not in {p.casefold() for p in names}, 'Self-reference')
        total, files = len(raw), []
        for name in names:
            path = source.parent.joinpath(*_parts(name))
            _inside(path, source.parent)
            data = _read(path)
            total += len(data)
            _require(len(data) <= 64 * 1024 * 1024 and total <= 256 * 1024 * 1024,
                     'Project exceeds beta size limit')
            files.append({'relative_path': name, 'source': str(path), 'bytes': len(data),
                          'sha256': _sha(data)})
        return {'project': str(source), 'project_sha256': _sha(raw), 'files': files,
                'board': board, 'available_boards': boards, 'schematics': schematics, 'file_count': len(files) + 1,
                'coverage': 'explicit numbered DocumentPath files only; external libraries and managed dependencies unverified'}

    def _snapshot(self, inspection):
        self.run_root.mkdir(parents=True, exist_ok=True)
        _no_links(self.run_root)
        folder = self.run_root / uuid.uuid4().hex
        folder.mkdir(exist_ok=False)
        source = Path(inspection['project'])
        manifest = {'schema': 1, 'source': str(source), 'copy': str(folder / source.name),
                    'project_sha256': inspection['project_sha256'], 'files': [],
                    'board': inspection['board'], 'schematics': inspection['schematics']}
        try:
            raw = _read(source)
            _require(_sha(raw) == inspection['project_sha256'], 'Project changed before copy')
            _write_new(folder / source.name, raw)
            for row in inspection['files']:
                original = Path(row['source'])
                raw = _read(original)
                _require(_sha(raw) == row['sha256'], 'Document changed before copy')
                target = folder.joinpath(*_parts(row['relative_path']))
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_new(target, raw)
                manifest['files'].append({**row, 'copy': str(target)})
            self._verify_files(manifest)
            _write_new(folder / 'manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
            return folder, manifest
        except Exception as error:
            _write_new(folder / 'snapshot-error.json', json.dumps({'error': str(error)}).encode())
            raise

    @staticmethod
    def _verify_files(manifest):
        for source, copy, expected in [(manifest['source'], manifest['copy'], manifest['project_sha256']),
                                        *((r['source'], r['copy'], r['sha256']) for r in manifest['files'])]:
            if _sha(_read(Path(source))) != expected or _sha(_read(Path(copy))) != expected:
                raise ValueError('Source or copy changed: ' + source)

    def check(self, project, board_path=None, run_drc=False):
        _require(type(run_drc) is bool, 'Invalid DRC option')
        _require(self.exe.is_file(), 'Altium executable unavailable')
        _require(not any(c in str(self.exe) for c in '"|\r\n'), 'Unsupported executable path')
        inspection = self.inspect(project, board_path)
        self.run_root.mkdir(parents=True, exist_ok=True)
        _no_links(self.run_root)
        _no_links(self.lock.parent)
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.lock.open('x', encoding='utf-8') as stream:
            json.dump({'mode': 'AltiumBeta', 'started': time.time(),
                       'recovery': 'inspect Altium and result before removing this file'}, stream)
        result = {'status': 'failed', 'native_compile': False, 'native_drc_pass': False,
                  'manufacturing_release': False, 'automatic_routing': False,
                  'full_drc_pass': False}
        folder = None
        native_dispatched = False
        try:
            folder, manifest = self._snapshot(inspection)
            request = uuid.uuid4().hex
            project_copy = Path(manifest['copy'])
            board = folder.joinpath(*_parts(manifest['board']))
            schematic = folder.joinpath(*_parts(manifest['schematics'][0]))
            response, drc = folder / 'response.ini', folder / 'native-drc.html'
            template = _read(ROOT / 'scripts/altium/ProjectCompile.pas.template').decode()
            block = """Ok := Board.RunBatchDesignRuleCheck('@@DRC@@', eDRC_HTML, False, False);
        Report.Add('native_drc_return=' + BoolToStr(Ok, True));
        If Not FileExists('@@DRC@@') Then Exit;""" if run_drc else ''
            template = template.replace('@@DRC_BLOCK@@', block)
            values = {'PROJECT': str(project_copy), 'BOARD': str(board), 'SCHEMATIC': str(schematic),
                      'REQUEST': request, 'REPORT': str(response), 'DRC': str(drc)}
            for key, value in values.items():
                _require(not any(c in value for c in "\r\n|"), 'Unsupported native path')
                template = template.replace('@@' + key + '@@', value.replace("'", "''"))
            _require('@@' not in template, 'Unresolved native template')
            _write_new(folder / 'Check.pas', template.encode('mbcs'))
            script = folder / 'Check.PrjScr'
            _write_new(script, b'[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Check.pas\n')
            result.update(run_id=folder.name, source=manifest['source'], project=str(project_copy),
                          board=str(board), request=request, drc_requested=run_drc)
            subprocess.Popen(f'"{self.exe}" -RScriptingSystem:RunScript(ProjectName="{script}"|ProcName="Check.pas>Run")')
            native_dispatched = True
            deadline = time.monotonic() + (600 if run_drc else 180)
            while not response.exists() and time.monotonic() < deadline:
                time.sleep(.5)
            if not response.exists():
                raise TimeoutError('Native check timed out; manual inspection required')
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(_decode_native(_read(response)))
            job = parser['job']
            _require(job['request'] == request and job['project'] == str(project_copy)
                     and job['board'] == str(board) and job['pcb_project'] == str(project_copy)
                     and job['schematic'] == str(schematic)
                     and job['compile_return'] == 'True' and job['flattened_available'] == 'True'
                     and parser['completion']['status'] == 'completed', 'Native compile or identity check failed')
            self._verify_files(manifest)
            result.update(status='completed', native_compile=True, files_unchanged=True,
                          manifest_sha256=_sha(_read(folder / 'manifest.json')),
                          response_sha256=_sha(_read(response)),
                          drc_coverage='not_requested' if not run_drc else 'selected_native_batch_rules_only')
            if run_drc:
                report = parse_drc_html(_read(drc), board)
                result.update(drc_counts=report['counts'], drc_rule_rows=report['rule_rows'],
                              report_sha256=_sha(_read(drc)),
                              native_drc_return=job['native_drc_return'],
                              selected_rules_pass=job['native_drc_return'] == 'True'
                              and report['counts']['violations'] == 0,
                              full_drc_pass=False)
        except Exception as error:
            result['error'] = str(error)
            raise
        finally:
            if folder is not None:
                _write_new(folder / 'result.json', (json.dumps(result, indent=2) + '\n').encode())
            if not native_dispatched:
                self.lock.unlink(missing_ok=True)
        self.lock.unlink()
        return result

    def report(self, run_id, offset=0, limit=50):
        _require(isinstance(run_id, str) and re.fullmatch(r'[0-9a-f]{32}', run_id), 'Invalid run ID')
        _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 200,
                 'Invalid pagination')
        folder = self.run_root / run_id
        _inside(folder, self.run_root)
        result = json.loads(_read(folder / 'result.json'))
        _require(result['run_id'] == run_id and result['status'] == 'completed', 'Run not completed')
        _require(_sha(_read(folder / 'manifest.json')) == result['manifest_sha256'], 'Manifest changed')
        _require(_sha(_read(folder / 'response.ini')) == result['response_sha256'], 'Response changed')
        rows = []
        if result['drc_requested']:
            board = folder.joinpath(*_parts(json.loads(_read(folder / 'manifest.json'))['board']))
            raw = _read(folder / 'native-drc.html')
            _require(_sha(raw) == result['report_sha256'], 'Report changed')
            parsed = parse_drc_html(raw, board)
            rows = [item for group in parsed['violation_details'] for item in group['items']]
            _require(len(rows) == result['drc_counts']['violations'], 'Report count changed')
        result.pop('native_drc_pass', None)
        result['full_drc_pass'] = False
        if result['drc_requested']:
            result['selected_rules_pass'] = result['native_drc_return'] == 'True' and result['drc_counts']['violations'] == 0
        return {**result, 'findings_total': len(rows), 'findings_offset': offset,
                'findings_limit': limit, 'findings': rows[offset:offset + limit]}
