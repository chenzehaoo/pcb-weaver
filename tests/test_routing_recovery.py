import pytest

from pcb_weaver.models import ToolchainConfig
from pcb_weaver.toolchain import Toolchain


@pytest.mark.parametrize("value", [True, False, 0, -1, 101, 1.5, "1"])
def test_optimizer_budget_rejects_invalid(value):
    with pytest.raises(ValueError):
        ToolchainConfig(router_optimizer_max_passes=value)
    with pytest.raises(ValueError):
        Toolchain({"router_optimizer_max_passes": value})


def test_optimizer_budget_preserves_old_runtime_defaults():
    assert "router_optimizer_max_passes" not in ToolchainConfig().model_dump(exclude_none=True)
    assert Toolchain().optimizer_passes == 100
    assert Toolchain({"router_optimizer_max_passes": None}).optimizer_passes == 100
    assert Toolchain({"router_optimizer_max_passes": 1}).optimizer_passes == 1
