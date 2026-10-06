"""Create a reviewable Altium beta demo from the accepted silk revision."""
import json
from pathlib import Path

from altium_project_snapshot import ROOT, _read, _sha, _write_new
from altium_silk_status import snapshot


def prepare():
    accepted = snapshot()
    source = Path(accepted['project']).parent
    target = ROOT / 'docs/validation/altium-redesign/developer-demo-v1'
    target.mkdir(exist_ok=False)
    mapping = {'WiFi_Controller_B.PrjPcb': 'Controller_Beta_Demo.PrjPcb',
               'Connector_WiFi.SchDoc': 'Connector_WiFi.SchDoc',
               'WiFi.PcbDoc': 'WiFi.PcbDoc'}
    rows = []
    for old, new in mapping.items():
        original = source / old
        copied = target / new
        data = _read(original)
        _write_new(copied, data)
        if _sha(_read(copied)) != _sha(data):
            raise ValueError('Demo file changed during copy: ' + old)
        rows.append({'source': str(original), 'copy': str(copied),
                     'source_sha256': _sha(data), 'copy_sha256': _sha(data),
                     'bytes': len(data)})
    if rows[-1]['source_sha256'] != _sha(_read(Path(accepted['board']))):
        raise ValueError('Unexpected board identity')
    manifest = {'schema': 1, 'status': 'prepared', 'example_version': '1.0',
                'project': str(target / mapping['WiFi_Controller_B.PrjPcb']),
                'board': str(target / 'WiFi.PcbDoc'), 'files': rows,
                'parent_revision': str(source), 'parent_silk_violations': accepted['after_violations'],
                'original_files_unchanged': True, 'automatic_routing_claimed': False}
    _write_new(target / 'demo-manifest.json', (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    return manifest


if __name__ == '__main__':
    print(json.dumps(prepare(), ensure_ascii=False, indent=2))
