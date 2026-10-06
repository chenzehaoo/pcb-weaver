"""Native acceptance fixture with a Chinese project filename on a disposable copy."""
import json
from pathlib import Path
import shutil

from altium_beta import Beta, ROOT, _read, _sha


def main():
    source = ROOT / 'docs/validation/altium-redesign/ba00d483efd34f16a50205b6a3e08c08'
    folder = ROOT / 'docs/validation/altium-redesign/中文工程内测'
    folder.mkdir(exist_ok=True)
    names = {'WiFi_Controller_B.PrjPcb': '中文工程.PrjPcb',
             'Connector_WiFi.SchDoc': 'Connector_WiFi.SchDoc', 'WiFi.PcbDoc': 'WiFi.PcbDoc'}
    for old, new in names.items():
        target = folder / new
        if not target.exists():
            shutil.copyfile(source / old, target)
        if _sha(_read(source / old)) != _sha(_read(folder / new)):
            raise ValueError('Fixture copy differs')
    beta = Beta()
    result = beta.check(str(folder / '中文工程.PrjPcb'))
    if not result['native_compile'] or not result['files_unchanged'] or result['full_drc_pass']:
        raise ValueError('Chinese filename native acceptance failed')
    output = ROOT / 'docs/validation/altium-beta-chinese-acceptance.json'
    with output.open('x', encoding='utf-8') as stream:
        json.dump({'status': 'passed', 'run_id': result['run_id'],
                   'project': str(folder / '中文工程.PrjPcb'),
                   'native_compile': True, 'source_files_unchanged': True,
                   'full_drc_pass': False}, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'status': 'passed', 'run_id': result['run_id']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
