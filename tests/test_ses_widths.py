from pathlib import Path

import pytest
import sexpdata

from pcb_weaver.ses_widths import raise_minima


def fixture(tmp_path):
    source = tmp_path / "input.ses"
    source.write_text('(session input (placement (resolution um 10)) (routes (resolution um 10) '
                      '(network_out (net "Net-(U204-R1OUT)" (wire (path F.Cu 1500 1 2 3 4)) '
                      '(wire (path B.Cu 4000 5 6 7 8)) (via "padstack" 2 3)))))')
    return source, tmp_path / "output.ses"


def test_only_widths_increase(tmp_path):
    source, output = fixture(tmp_path)
    before = source.read_bytes()
    audit = raise_minima(source, output, .2, {}, {"Net-(U204-R1OUT)"})
    assert len(audit["changes"]) == 1
    assert audit["changes"][0]["from_units"] == 1500
    assert audit["changes"][0]["to_units"] == 2000
    assert source.read_bytes() == before
    assert sexpdata.loads(output.read_text()) == sexpdata.loads(before.decode().replace("1500", "2000"))
    assert audit["requires_native_drc"] and not audit["manufacturing_authorized"]


@pytest.mark.parametrize("minimum", [.20001, .25, .4])
def test_minimum_rounds_up_and_does_not_shrink_wide_paths(tmp_path, minimum):
    source, output = fixture(tmp_path)
    audit = raise_minima(source, output, .1, {"Net-(U204-R1OUT)": minimum}, {"Net-(U204-R1OUT)"})
    assert len(audit["changes"]) == 1
    assert audit["changes"][0]["to_units"] / 10000 >= minimum


@pytest.mark.parametrize("minimum", [True, 0, -.2, "0.2", float("nan"), float("inf")])
def test_rejects_invalid_minima_without_output(tmp_path, minimum):
    source, output = fixture(tmp_path)
    with pytest.raises(ValueError):
        raise_minima(source, output, minimum, {}, {"Net-(U204-R1OUT)"})
    assert not output.exists()


def test_rejects_unknown_net_and_output_alias(tmp_path):
    source, output = fixture(tmp_path)
    before = source.read_bytes()
    with pytest.raises(ValueError):
        raise_minima(source, output, .2, {}, {"different"})
    with pytest.raises(ValueError):
        raise_minima(source, source, .2, {}, {"Net-(U204-R1OUT)"})
    output.write_text("keep")
    with pytest.raises(ValueError):
        raise_minima(source, output, .2, {}, {"Net-(U204-R1OUT)"})
    assert output.read_text() == "keep" and source.read_bytes() == before
