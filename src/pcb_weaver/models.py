"""Validated design intent, separated from KiCad's electrical design rules."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BoardRules(StrictModel):
    layers: Literal[2, 4, 6, 8] = 2
    max_voltage: float = Field(default=24, gt=0, le=48)
    edge_clearance_mm: float = Field(default=1.0, ge=0, le=20)
    component_gap_mm: float = Field(default=0.5, ge=0, le=20)


class Proximity(StrictModel):
    reference: str = Field(min_length=1)
    target: str = Field(min_length=1)
    max_distance_mm: float = Field(gt=0)


class Region(StrictModel):
    references: list[str] = Field(min_length=1)
    bounds: tuple[float, float, float, float]

    @model_validator(mode="after")
    def ordered_bounds(self):
        if self.bounds[0] >= self.bounds[2] or self.bounds[1] >= self.bounds[3]:
            raise ValueError("Region bounds must have positive width and height")
        return self


class NetRule(StrictModel):
    nets: list[str] = Field(min_length=1)
    min_width_mm: float = Field(gt=0, le=10)


class Fabrication(StrictModel):
    min_track_mm: float = Field(default=0.25, gt=0, le=10)
    min_clearance_mm: float = Field(default=0.2, gt=0, le=10)
    min_via_drill_mm: float = Field(default=0.3, gt=0, le=10)


class ReleaseRules(StrictModel):
    require_erc: bool = True


class PlacementOptions(StrictModel):
    algorithm: Literal["auto", "slsqp", "block_coordinate", "legalize"] = "auto"
    seed: int = Field(default=1729, ge=0, le=4294967295, strict=True)
    max_iterations: int = Field(default=80, ge=1, le=500, strict=True)
    passes: int = Field(default=4, ge=1, le=30, strict=True)
    block_size: int = Field(default=8, ge=1, le=16, strict=True)


class AutoRepairOptions(StrictModel):
    max_attempts: int = Field(default=6, ge=1, le=24, strict=True)
    time_budget_seconds: int = Field(default=600, ge=30, le=1800, strict=True)
    proposal_timeout_seconds: int = Field(default=60, ge=1, le=180, strict=True)
    max_region_area_mm2: float = Field(default=900, gt=0, le=2500)
    allow_neckdown: bool = Field(default=False, strict=True)
    allow_local_adjustment: bool = Field(default=False, strict=True)
    allow_multinet: bool = Field(default=False, strict=True)


class CompletionOptions(StrictModel):
    placement_mode: Literal["optimize", "preserve"] = "optimize"
    candidate_count: int = Field(default=3, ge=1, le=5, strict=True)
    route_passes: int = Field(default=20, ge=1, le=100, strict=True)
    time_budget_seconds: int = Field(default=1800, ge=60, le=7200, strict=True)
    placement_spread_mm: float = Field(default=0.5, ge=0, le=2, strict=True)
    routing_policy: Literal["strict", "normalize_widths"] = "strict"
    repair: AutoRepairOptions = Field(default_factory=AutoRepairOptions)
    repair_cycles: int = Field(default=1, ge=1, le=3, strict=True)

    @model_validator(mode="after")
    def valid_repair_cycles(self):
        if self.repair_cycles > 1 and self.placement_mode != "preserve":
            raise ValueError("Multiple repair cycles require preserve placement mode")
        return self


class Constraints(StrictModel):
    schema_version: Literal[1] = 1
    board: BoardRules = Field(default_factory=BoardRules)
    fixed_references: list[str] = Field(default_factory=list)
    edge_overhang_references: list[str] = Field(default_factory=list)
    critical_nets: list[str] = Field(default_factory=list)
    proximity: list[Proximity] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)
    net_rules: list[NetRule] = Field(default_factory=list)
    fabrication: Fabrication = Field(default_factory=Fabrication)
    release: ReleaseRules = Field(default_factory=ReleaseRules)
    placement: PlacementOptions = Field(default_factory=PlacementOptions)


class ToolchainConfig(StrictModel):
    kicad_cli: str | None = None
    kicad_python: str | None = None
    java: str | None = None
    freerouting_jar: str | None = None
    wsl_distro: str | None = None
    timeout_seconds: int = Field(default=180, ge=10, le=1800)
    route_timeout_seconds: int | None = Field(default=None, ge=10, le=7200, strict=True)
    controlled_neckdown: bool = Field(default=False, strict=True)
    fanout: bool = Field(default=False, strict=True)
    router_optimizer_max_passes: int | None = Field(default=None, ge=1, le=100, strict=True)
    repair_route_scope: bool | None = Field(default=None, strict=True)
    router_copper_to_edge_clearance_mm: float | None = Field(default=None, ge=0, le=20, strict=True)
