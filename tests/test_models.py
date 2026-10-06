import pytest
from pydantic import ValidationError

from pcb_weaver.models import Constraints, PlacementOptions


@pytest.mark.parametrize("layers", [2, 4, 6, 8])
def test_supported_layer_profiles(layers):
    model = Constraints.model_validate({"board": {"layers": layers}})
    assert model.board.layers == layers
    assert model.placement.algorithm == "auto"


@pytest.mark.parametrize("layers", [0, 1, 3, 5, 7, 10, "4", True])
def test_invalid_layer_profiles(layers):
    with pytest.raises(ValidationError):
        Constraints.model_validate({"board": {"layers": layers}})


@pytest.mark.parametrize("field,value", [
    ("algorithm", "unknown"), ("seed", -1), ("seed", True), ("seed", "5"),
    ("max_iterations", 0), ("max_iterations", 501), ("passes", 0),
    ("passes", 31), ("block_size", 17), ("block_size", 0), ("block_size", 1.2),
])
def test_placement_options_have_bounded_strict_controls(field, value):
    with pytest.raises(ValidationError):
        PlacementOptions.model_validate({field: value})


def test_constraints_schema_advertises_compatible_placement_extension():
    schema = Constraints.model_json_schema()
    assert schema["$defs"]["BoardRules"]["properties"]["layers"]["enum"] == [2, 4, 6, 8]
    assert schema["$defs"]["PlacementOptions"]["properties"]["algorithm"]["enum"] == ["auto", "slsqp", "block_coordinate", "legalize"]
    assert "placement" not in schema.get("required", [])
    assert Constraints.model_validate({}).placement.seed == 1729
