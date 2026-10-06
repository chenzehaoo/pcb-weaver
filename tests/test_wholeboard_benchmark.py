"""Fixture integrity tests, independent of native router availability."""
import importlib.util
from pathlib import Path
import pytest
import sexpdata

from pcb_weaver.service import EngineeringService
from pcb_weaver.storage import digest, read_json
from pcb_weaver.repair_geometry import _read, _serialize

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('wholeboard_benchmark', ROOT / 'scripts/wholeboard_benchmark.py')
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def source(tmp_path):
    engine = EngineeringService(tmp_path / 'data')
    data = engine.import_project('fixture', str(ROOT / 'examples/manufacturing-demo/two-layer.kicad_pcb'))['revision']
    return engine._verified('fixture',data['id'])[1]


def test_preparation_preserves_all_nonrouting_nodes_and_rules(tmp_path):
    original = source(tmp_path)
    data = read_json(original / 'revision.json')
    board = original / 'design' / data['board']
    before = digest(board)
    target = tmp_path / 'fixture'
    manifest = benchmark.prepare(original,target)
    assert digest(board) == before
    assert manifest == benchmark.fixture_verified(target)
    assert digest(original / 'constraints.json') == digest(target / 'constraints.json')
    assert _read(board)[0] == _read(target / data['board'])[0]
    with pytest.raises(ValueError,match='new output'):
        benchmark.prepare(original,target)


def test_fixture_tampering_is_rejected(tmp_path):
    original = source(tmp_path)
    target = tmp_path / 'fixture'
    manifest = benchmark.prepare(original,target)
    board = target / manifest['board']
    board.write_bytes(board.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='changed'):
        benchmark.fixture_verified(target)


def test_unsealed_source_is_rejected_before_copy(tmp_path):
    original = source(tmp_path)
    data = read_json(original / 'revision.json')
    board = original / 'design' / data['board']
    board.write_bytes(board.read_bytes()+b'\n')
    target = tmp_path / 'fixture'
    with pytest.raises(ValueError,match='hashes'):
        benchmark.prepare(original,target)
    assert not target.exists()


def test_only_routing_is_removed(tmp_path):
    original = source(tmp_path)
    data = read_json(original / 'revision.json')
    board = original / 'design' / data['board']
    ast = _read(board)[0]
    ast.append(sexpdata.loads('(segment (start 20 20) (end 21 20) (width 0.25) (layer "F.Cu") (net 1) (uuid "00000000-0000-0000-0000-000000000001"))'))
    board.write_bytes(_serialize(ast))
    # Re-import makes a new authentic fixture, without editing the revision ledger.
    engine = EngineeringService(tmp_path / 'second')
    imported = engine.import_project('routed',str(board))['revision']
    source_root = engine._verified('routed',imported['id'])[1]
    target = tmp_path / 'fixture'
    manifest = benchmark.prepare(source_root,target)
    assert manifest['removed'] == {'segment':1}
    assert _read(target / data['board'])[0] == ast[:-1]
