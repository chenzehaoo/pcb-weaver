"""Pinned supervised DRC evidence. Enabled checks do not imply successful checks."""
import configparser
import hashlib
import json
from pathlib import Path

from altium_drc_gate import parse_drc_html

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'docs/validation/altium-native/8fbedf606b454e1698f9e891b96135f6'
PINS = {
    'result.json': 'c71690808dbffbe0439a985ecfb879c00aa18f813d5dca4d7667891494b708ef',
    'batch-before.json': 'af92b8cc4b5ab1fa4c3992dd8e17f37ad0de6c26925de9ff21340cd3c0517395',
    'batch-effective.json': 'd64a79ceebee3c941d788cc729428981c911831cfb6ffe28ecaad69d4841b3f4',
    'report-options-effective.json': '24772eecec2a00d0b7d6b50ac882eff4153b8c7c6f8388e3bf7448d58971de8b',
    'native-drc.html': '22f23338861170dd1bd9c71c84658691255957fbbcf533e25219cca40d5268ef',
    'response.ini': '8896d8e6305fc86cd71e9809d6e60300804686d62c47f65eafdb34efbc7c761c',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot():
    raw = {name: (FOLDER / name).read_bytes() for name in PINS}
    require(all(hashlib.sha256(raw[name]).hexdigest() == digest for name, digest in PINS.items()),
            'Pinned DRC evidence changed')
    result = json.loads(raw['result.json'])
    require(result['status'] == 'completed' and result['mode'] == 'DRCSetup', 'Wrong native action')
    require(result['native']['job']['native_return'] == 'False', 'Expected native rejection missing')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(raw['response.ini'].decode('utf-8-sig'))
    require({s: dict(parser[s]) for s in parser.sections()} == result['native'], 'Native response mismatch')
    for key in ('source', 'board'):
        require(hashlib.sha256(Path(result[key]).read_bytes()).hexdigest() == result['source_sha256'],
                'Source or snapshot changed')
    before = json.loads(raw['batch-before.json'].decode('utf-8-sig'))
    effective = json.loads(raw['batch-effective.json'].decode('utf-8-sig'))
    require(len(effective) == 55 and len({row['name'] for row in effective}) == 55,
            'Incomplete native batch observation')
    require(all(row['value'].split()[-1] == 'True' for row in effective), 'Batch check not enabled')
    require({row['name'] for row in before} == {row['name'] for row in effective}, 'Batch identity mismatch')
    options = json.loads(raw['report-options-effective.json'].decode('utf-8-sig'))
    require(any(row['name'] == '100000' and row['value'] == '100000' for row in options),
            'Report threshold not observed')
    report = parse_drc_html(raw['native-drc.html'], Path(result['board']))
    require(all((FOLDER / name).read_bytes() == data for name, data in raw.items()),
            'Evidence changed during read')
    return {'scope': 'pinned_supervised_native_run_not_live', 'status': 'blocked',
            'native_return': False, 'counts': report['counts'],
            'rule_rows': report['rule_rows'], 'batch_before': before, 'batch_effective': effective,
            'report_options_effective': options, 'stop_threshold': 100000,
            'ui_batch_types_enabled': 55, 'full_rule_coverage_verified': False,
            'automatic_routing_authorized': False, 'manufacturing_authorized': False,
            'signal_integrity': 'not_completed_source_schematic_documents_unavailable',
            'limitations': ['Batch enablement is observed, not proof every analysis completed',
                            'Native Signal Integrity emitted missing-source-schematic error',
                            'Waiver coverage and rule-set applicability are not established',
                            'UI settings were observed in memory, not saved into the PCB snapshot'],
            'request': result['request'], 'evidence_sha256': PINS}


if __name__ == '__main__':
    print(json.dumps(snapshot(), indent=2))
