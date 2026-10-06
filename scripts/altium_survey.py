"""Read bundled boards through native Altium on fresh copies; no format conversion."""
import configparser
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = Path('D:/Altium/AD26-Examples/Examples')
BOARDS = ('Mini PC/Mini PC - WiFi/WiFi.PcbDoc',
          'Bluetooth Sentinel/Bluetooth_Sentinel.PcbDoc',
          'SpiritLevel-SL1/SL1 Xilinx Spartan-IIE PQ208 Rev1.02.PcbDoc')


def main():
    run = ROOT / 'docs/validation/altium-survey' / uuid.uuid4().hex
    run.mkdir(parents=True)
    print(str(run), flush=True)
    summaries = []
    for i, name in enumerate(BOARDS):
        folder = run / str(i)
        folder.mkdir()
        source = BASE / name
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        board = folder / 'survey.PcbDoc'
        shutil.copy2(source, board)
        report = folder / 'response.ini'
        request = uuid.uuid4().hex
        template = (ROOT / 'scripts/altium/Survey.pas.template').read_text()
        for key, value in {'BOARD': str(board), 'REPORT': str(report), 'REQUEST': request}.items():
            template = template.replace('@@' + key + '@@', value.replace("'", "''"))
        (folder / 'Survey.pas').write_text(template, encoding='ascii')
        project = folder / 'Survey.PrjScr'
        project.write_text('[Design]\nVersion=1.0\n[Document1]\nDocumentPath=Survey.pas\n')
        subprocess.Popen(f'"D:\\Altium\\AD26\\X2.EXE" -RScriptingSystem:RunScript(ProjectName="{project}"|ProcName="Survey.pas>Run")')
        deadline = time.monotonic() + 120
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        if not report.exists():
            raise RuntimeError(f'Native timeout, inspect Altium before retry: {folder}')
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(report, encoding='utf-8-sig')
        row = dict(parser['survey'])
        if row.get('request') != request or row.get('status') != 'completed':
            raise RuntimeError(str(row))
        if hashlib.sha256(source.read_bytes()).hexdigest() != original:
            raise RuntimeError('Original bundled example changed')
        row.update(source=str(source), source_sha256=original)
        summaries.append(row)
        print(json.dumps(row), flush=True)
        (run / 'summary.json').write_text(json.dumps(summaries, indent=2))


if __name__ == '__main__':
    main()
