"""Read-only, pinned native DRC control acceptance; never authorizes routing."""
import hashlib
import json
from pathlib import Path

from altium_drc_gate import parse_drc_html, validate_drc

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'docs/validation/altium-native'
NEGATIVE = BASE / '2c5259b0dcbb4d0eb0f983445d9dea79'
CONTROL = BASE / 'cd102d2ca95947cbae434e9cf1ec0721'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def accept():
    baseline = validate_drc(CONTROL / 'result.json')
    require(baseline['report_valid'], 'Invalid baseline evidence')
    require(baseline['counts']['violations'] == 0, 'Baseline has violations')
    result = json.loads((NEGATIVE / 'result.json').read_text())
    require(result['status'] == 'completed' and result['mode'] == 'DRCNegative',
            'Wrong negative-control action')
    require(result['native']['job']['purpose'] == 'negative_width_control_not_routing_output',
            'Wrong negative-control purpose')
    require(result['native']['job']['native_return'] == 'False', 'Native rejection missing')
    for path, digest in (
        (Path(result['source']), result['source_sha256']),
        (Path(result['board']), result['source_snapshot_sha256']),
        (NEGATIVE / 'response.ini', result['response_sha256']),
        (NEGATIVE / 'native-drc.html', result['artifacts']['native-drc.html']),
    ):
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, 'Evidence hash mismatch')
    require(result['source_sha256'] == baseline['integrity']['source_sha256'],
            'Controls do not share the same source')
    raw = (NEGATIVE / 'native-drc.html').read_bytes()
    require(hashlib.sha256(raw).hexdigest() ==
            'bef53af124b121a1ad2ae1bdc95b8b22f83ebf243792bdf9adff21415b7cf780',
            'Negative report does not match captured native evidence')
    parsed = parse_drc_html(raw, Path(result['board']))
    require(parsed['counts']['violations'] == 2, 'Unexpected negative violation total')
    require(any(row['description'].startswith('Width Constraint') and row['count'] == 1
                for row in parsed['rule_rows']), 'Width violation not detected')
    require(b'Actual Width = 0.001mm, Target Width = 0.25mm' in raw,
            'Injected width mismatch')
    return {'control_acceptance': 'passed', 'baseline_violations': 0,
            'negative_violations': 2, 'width_violation_detected': True,
            'full_rule_coverage_verified': False, 'automatic_routing_accepted': False,
            'negative_report': str(NEGATIVE / 'native-drc.html')}


if __name__ == '__main__':
    print(json.dumps(accept(), indent=2))
