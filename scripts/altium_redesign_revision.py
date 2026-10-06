"""Create a non-released route-B working project, preserving all baseline files."""
import configparser
import io
import json
from pathlib import Path

from altium_project_snapshot import ROOT, _read, _sha, _write_new, snapshot_project

BASELINE = ROOT / 'docs/validation/altium-project/246e8c0e8a6c4e5e8ecd3f1404d97077/WiFi_miniPCIe.PrjPcb'
BOARD_SHA = '1715160bd08abbc1c649b719a8f4bbfe67e976e80e1aca2537f8edf8c0d568a4'


def working_project(raw):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    parser.read_string(raw.decode('utf-8-sig'))
    documents = [s for s in parser.sections() if s.startswith('Document')]
    retained = []
    for section in documents:
        row = dict(parser[section])
        if row.get('DocumentPath') in {'Connector_WiFi.SchDoc', 'WiFi.PcbDoc'}:
            retained.append(row)
        parser.remove_section(section)
    if {r['DocumentPath'] for r in retained} != {'Connector_WiFi.SchDoc', 'WiFi.PcbDoc'} or len(retained) != 2:
        raise ValueError('Expected one schematic and one PCB')
    for index, row in enumerate(retained, 1):
        parser[f'Document{index}'] = row
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    return output.getvalue().encode('utf-8')


def create_revision(source=BASELINE, output=None, expected_board_sha=BOARD_SHA):
    source = Path(source)
    if _sha(_read(source.parent / 'WiFi.PcbDoc')) != expected_board_sha:
        raise ValueError('Baseline board hash mismatch')
    manifest = snapshot_project(source, output or ROOT / 'docs/validation/altium-redesign')
    if manifest['status'] != 'completed':
        raise ValueError(str(manifest.get('errors')))
    folder = Path(manifest['snapshot_directory'])
    if _sha(_read(folder / 'WiFi.PcbDoc')) != expected_board_sha:
        raise ValueError('Copied baseline board hash mismatch')
    project = folder / 'WiFi_Controller_B.PrjPcb'
    _write_new(project, working_project(_read(source)))
    contract = {
        'schema': 1, 'route': 'B', 'status': 'baseline_ready_for_redesign',
        'project': str(project), 'board': str(folder / 'WiFi.PcbDoc'),
        'baseline_manifest': str(folder / 'manifest.json'),
        'project_sha256': _sha(_read(project)), 'board_sha256': expected_board_sha,
        'mini_pcie_mechanical_compatibility_claimed': False,
        'preserve': ['components', 'pin_net_mapping', 'connector_electrical_interface',
                     'power_architecture', 'original_rule_values'],
        'excluded_from_active_project': ['WiFi.PCBDwf', 'WiFi_miniPCIe.BomDoc',
                                         'WiFi_panel.PcbDoc', 'WiFi_panel.PCBDwf'],
        'exclusion_reason': 'Baseline production documents are stale for redesign; retained only as historical copies.',
        'requirements': {
            'mechanics': {'status': 'provisional', 'use': 'open bench prototype',
                          'enclosure_fit_verified': False, 'new_outline_defined': False},
            'manufacturing': {'status': 'unqualified', 'mask_process_approved': False,
                              'rule_relaxation_authorized': False},
            'test_access': {'status': 'unverified', 'logical_nets': 27,
                            'fabrication_and_assembly_roles': True},
        },
        'acceptance': {'native_compile': False, 'native_drc': False,
                       'automatic_routing': False, 'signal_integrity': False,
                       'manufacturing_release': False},
        'baseline_observation': {'full_project_violations': 159,
                                  'not_a_measurement_of_new_revision': True},
        'next_actions': ['native_compile', 'define_outline_and_access_constraints',
                         'placement_and_routing', 'native_full_drc', 'manufacturing_review'],
    }
    _write_new(folder / 'redesign.json', (json.dumps(contract, indent=2) + '\n').encode())
    return contract


if __name__ == '__main__':
    print(json.dumps(create_revision(), indent=2))
