"""Strict wire request models for 3D scenes, native loads, and grid storage."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

StrictNum = StrictInt | StrictFloat
_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)
class _StrictModel(BaseModel):
    model_config = _STRICT_CONFIG
class BifacialUpdates(_StrictModel):
    """Mount, update, or remove PVObject_Bifacial=pvBiFacial on an orientation."""
    enabled: StrictBool = True
    model: Literal["Sheds2D", "Trackers2D"] | None = None
    param_origin: Literal["UnlimSheds", "RegularSheds"] | None = None
    ground_albedo: StrictNum | None = None
    bifaciality_factor: StrictNum | None = None
    rear_shading_factor: StrictNum | None = None
    mismatch_loss_factor: StrictNum | None = None
    height_above_ground_m: StrictNum | None = None
    pitch_user_m: StrictNum | None = None
    n_sheds_user: StrictInt | None = None
    shed_pitch_from_user: StrictBool | None = None
class TrackingControls(_StrictModel):
    """Explicit controls of the source native tracking type; absent fields remain inherited."""
    axis_azimuth_deg: StrictNum | None = None
    min_rotation_deg: StrictNum | None = None
    max_rotation_deg: StrictNum | None = None
    backtracking: StrictBool | None = None
    irradiance_optimization: StrictBool | None = None
    axis_tilt_deg: StrictNum | None = None
    field_tilt_deg: StrictNum | None = None
    min_azimuth_deg: StrictNum | None = None
    max_azimuth_deg: StrictNum | None = None
    min_tilt_deg: StrictNum | None = None
    max_tilt_deg: StrictNum | None = None
    frame_min_rotation_deg: StrictNum | None = None
    frame_max_rotation_deg: StrictNum | None = None
    frame_min_tilt_deg: StrictNum | None = None
    frame_max_tilt_deg: StrictNum | None = None
    central_gap_m: StrictNum | None = None
    pitch_slope_deg: StrictNum | None = None
    wind_stow_position_deg: StrictNum | None = None
    wind_stow_speed_mps: StrictNum | None = None
    wind_stow: StrictBool | None = None
    electrical_effect: StrictBool | None = None
class FlatRoof(_StrictModel):
    """Flat roof: the plate sits at the eave height."""
    kind: Literal["flat"]
class GableRoof(_StrictModel):
    """Symmetric gable roof with the ridge parallel to the house local X axis."""
    kind: Literal["gable"]
    pitch_deg: StrictNum
RoofSpec = Annotated[FlatRoof | GableRoof, Field(discriminator="kind")]
class HouseMesh(_StrictModel):
    """AI-authored named architectural geometry; indices are explicit, zero-based triangles."""
    name: StrictStr
    vertices: list[Annotated[list[StrictNum], Field(min_length=3, max_length=3)]]
    triangles: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]
class MeshHouseSpec(_StrictModel):
    """One house composed by the caller, not selected from a roof-template catalogue."""
    kind: Literal["mesh"]
    meshes: list[HouseMesh]
class BuildingObstacle(_StrictModel):
    """Surrounding flat- or gable-roofed building obstacle in the house local frame."""
    kind: Literal["building"]
    name: StrictStr
    origin_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    length_m: StrictNum
    width_m: StrictNum
    height_m: StrictNum
    rotation_deg: StrictNum = 0.0
    roof: FlatRoof | GableRoof | None = None
class WallObstacle(_StrictModel):
    """Vertical barrier/parapet wall extruded along an XY line segment."""
    kind: Literal["wall"]
    name: StrictStr
    start_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    end_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    height_m: StrictNum
    thickness_m: StrictNum = 0.2
class MeshObstacle(_StrictModel):
    """Arbitrary custom 3D obstacle mesh (vertices + triangles) in the house local frame."""
    kind: Literal["mesh"]
    name: StrictStr
    vertices: list[Annotated[list[StrictNum], Field(min_length=3, max_length=3)]]
    triangles: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]
class TreeObstacle(_StrictModel):
    """Native PVsyst parametric tree obstacle (ObjetElemType=oeTree)."""
    kind: Literal["tree"]
    name: StrictStr
    origin_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    top_height_m: StrictNum
    middle_height_m: StrictNum
    bottom_height_m: StrictNum
    trunk_height_m: StrictNum
    crown_diameter_m: StrictNum
    trunk_diameter_m: StrictNum
    rotation_deg: StrictNum = 0.0
ObstacleSpec = Annotated[BuildingObstacle | WallObstacle | MeshObstacle | TreeObstacle, Field(discriminator="kind")]
class ModuleGeometry(_StrictModel):
    """Explicit geometric metadata, checked against a readable loose PAN when present."""
    model_name: StrictStr
    nom_power_wp: StrictNum
    width_m: StrictNum
    length_m: StrictNum
    geometry_depth_m: StrictNum
class PlacedPVTableLayout(_StrictModel):
    """Explicit local placement over one named, planar, upward-facing roof surface mesh."""
    module_rows: StrictInt
    module_columns: StrictInt
    orientation: Literal["portrait", "landscape"]
    row_spacing_m: StrictNum
    column_spacing_m: StrictNum
    origin_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    roof_mesh: StrictStr
class NamedPVTableLayout(PlacedPVTableLayout):
    """Named PV region; grouped authoring selects orientation_id, optional source_subarray_id, and optional module_geometry."""
    name: StrictStr
    orientation_id: StrictInt | None = None
    source_subarray_id: StrictInt | None = None  # explicit source SubArray owner when several share one NoOrientation
    module_geometry: ModuleGeometry | None = None
class NamedPVTablesLayout(_StrictModel):
    """List of 1..64 named PV table regions."""
    tables: Annotated[list[NamedPVTableLayout], Field(min_length=1, max_length=64)]
class ModuleCellBinding(_StrictModel):
    """One module cell (1-based row/col and global_string; 0 marks an unassigned draft cell)."""
    row: Annotated[StrictInt, Field(ge=1)]
    col: Annotated[StrictInt, Field(ge=1)]
    global_string: Annotated[StrictInt, Field(ge=0)]
class TableStringBinding(_StrictModel):
    """Per-table cell wiring keyed by table name and source subarray_id."""
    name: StrictStr
    subarray_id: Annotated[StrictInt, Field(ge=1)]
    cells: list[ModuleCellBinding]
class StringLayoutBinding(_StrictModel):
    """Explicit per-module string wiring across named PV tables."""
    tables: Annotated[list[TableStringBinding], Field(min_length=1, max_length=64)]
class SynthesisOrientationSpec(_StrictModel):
    """Optional fixed-plane orientation for zero-template house synthesis."""
    id: Annotated[StrictInt, Field(ge=1)]
    tilt_deg: StrictNum
    azimuth_deg: StrictNum
    base_slope_deg: StrictNum = 0.0
    comment: StrictStr | None = None
class SynthesisSubArraySpec(_StrictModel):
    """Optional subarray electrical declaration for zero-template house synthesis."""
    subarray_id: Annotated[StrictInt, Field(ge=1)]
    orientation_id: Annotated[StrictInt, Field(ge=1)]
    comment: StrictStr | None = None
    pan_name: StrictStr | None = None
    ond_name: StrictStr | None = None
    n_mod_serie: Annotated[StrictInt, Field(ge=1)]
    n_string_ch: Annotated[StrictInt, Field(ge=1)]
    n_inverter: Annotated[StrictInt, Field(ge=1)] = 1
    n_inv_mppt: Annotated[StrictInt, Field(ge=1)] | None = None
    independent_mppt: StrictBool | None = None
    power_sharing_inverter_id: StrictInt = -1
    pnom_ac_in_mppt: StrictNum = 7.5
    lid_loss: StrictNum = 0.0
    pmism_mpp: StrictNum | None = None
    module_quality_loss: StrictNum | None = None
    dc_wiring_loss_pct: StrictNum = 1.5
    dc_wiring_resistance_mohm: StrictNum = 0.0
class HouseTaskRequest(_StrictModel):
    """3D house task specification: authored house mesh, PV layout, optional source variant (or zero-template synthesis), and run options."""
    source_project: StrictStr | None = None
    source_variant: StrictStr | None = None
    site_name: StrictStr | None = None
    met_name: StrictStr | None = None
    pan_name: StrictStr | None = None
    ond_name: StrictStr | None = None
    anchor_azimuth_deg: StrictNum | None = None
    orientations: Annotated[list[SynthesisOrientationSpec], Field(min_length=1, max_length=64)] | None = None
    subarrays: Annotated[list[SynthesisSubArraySpec], Field(min_length=1, max_length=64)] | None = None
    new_project: StrictStr
    variant_name: StrictStr
    year: StrictInt
    house: MeshHouseSpec
    layout: NamedPVTablesLayout | PlacedPVTableLayout
    obstacles: Annotated[list[ObstacleSpec], Field(min_length=1, max_length=64)] | None = None
    string_layout: StringLayoutBinding | None = None
    module_geometry: ModuleGeometry | None = None
    report_pages: list[StrictStr] | None = None
    report_pdf: StrictBool | None = None
    shading_mode: Literal["linear", "module_layout"] | None = None
    scene_import_method: Literal["native"] | None = None
    allow_window_creation: StrictBool | None = None

    @model_validator(mode="after")
    def _validate_source_or_zero_template(self):
        has_proj, has_var = self.source_project is not None, self.source_variant is not None
        synth_fields = [name for name in ("site_name", "met_name", "pan_name", "ond_name", "anchor_azimuth_deg", "orientations", "subarrays") if getattr(self, name) is not None]
        if has_proj != has_var:
            raise ValueError("Provide both source_project and source_variant, or omit both for zero-template synthesis")
        if has_proj and synth_fields:
            raise ValueError("Zero-template synthesis fields cannot be combined with source_project/source_variant: " + ", ".join(synth_fields))
        if not has_proj and (not (self.site_name or "").strip() or not (self.met_name or "").strip()):
            raise ValueError("Zero-template house task requires non-empty site_name (.SIT) and met_name (.MET) when source_project/source_variant are omitted")
        return self
class ConstantNativeLoad(_StrictModel):
    """Native fixed constant consumption in kW; unknown fields stop at the boundary."""
    mode: Literal["constant"]
    constant_kw: StrictNum
class MonthlyNativeLoad(_StrictModel):
    """Native monthly loads as twelve declared kWh values; never derived from monthly power."""
    mode: Literal["monthly"]
    monthly_energy_kwh: list[StrictNum] = Field(min_length=12, max_length=12)
class DailyNativeLoad(_StrictModel):
    """Native daily profile as 24 hourly powers; the trailing slot is native format only."""
    mode: Literal["daily"]
    daily_power_kw: list[StrictNum] = Field(min_length=24, max_length=24)
class HourlyNativeLoad(_StrictModel):
    """Prepared 8760/8784 hourly load manifest bound as an embedded MJ base62 snapshot."""
    mode: Literal["hourly"]
    load_manifest_name: StrictStr
class HouseholdApplianceSpec(_StrictModel):
    """One household appliance entry for LoadType=HouseHold."""
    name: StrictStr
    count: Annotated[StrictInt, Field(ge=1, le=10000)] = 1
    power_w: StrictNum
    hours: list[StrictInt] | None = None
    half_hours: list[StrictInt] | None = None
    slot_index: Annotated[StrictInt, Field(ge=1, le=10)] | None = None
class HouseholdNativeLoad(_StrictModel):
    """Native household appliance schedule (LoadType=HouseHold, yearly or seasonal)."""
    mode: Literal["household"]
    modulation: Literal["yearly", "seasonal"] = "yearly"
    appliances: Annotated[list[HouseholdApplianceSpec], Field(min_length=1, max_length=10)] | None = None
    seasons: dict[Literal["Summer", "Autumn", "Winter", "Spring"], Annotated[list[HouseholdApplianceSpec], Field(min_length=1, max_length=10)]] | None = None
class ProbabilityNativeLoad(_StrictModel):
    """Hourly probability distribution load (deterministically sampled into native embedded hourly snapshot)."""
    mode: Literal["probability"]
    power_step_kw: StrictNum
    probabilities_pct: Annotated[list[StrictNum], Field(min_length=2, max_length=24)]
    seed: Annotated[StrictInt, Field(ge=0)] = 42
    year: Annotated[StrictInt, Field(ge=1969, le=2068)] = 1990
NativeLoadRequest = Annotated[ConstantNativeLoad | MonthlyNativeLoad | DailyNativeLoad | HourlyNativeLoad | HouseholdNativeLoad | ProbabilityNativeLoad, Field(discriminator="mode")]
class _GridStorageScheduleBase(_StrictModel):
    coupling: Literal["ac_coupled"] = "ac_coupled"
    charging_strategy: Literal["pv_available", "pv_above_thresh", "pv_to_grid_min", "pv_prod_fraction", "spec_charg_power"] | None = None
    discharging_strategy: Literal["immediate", "after_sunset", "night", "hour_window", "specified_hours"] | None = None
    grid_limit_kw: StrictNum | None = None
    charging_pv_fraction: StrictNum | None = None
    charging_power_kw: StrictNum | None = None
    charging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharging_power_kw: StrictNum | None = None
    discharging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharge_start_hour: StrictInt | None = None
    discharge_end_hour: StrictInt | None = None
class GridStorageUpdates(_GridStorageScheduleBase):
    """Native existing AC-coupled storage controls and schedules; unknown fields stop at the boundary."""
    strategy: Literal["self_consumption", "weak_grid", "peak_shaving", "power_shift"] | None = None
    soc_min: StrictNum | None = None
    soc_max: StrictNum | None = None
    initial_soc: StrictNum | None = None
    reserve_soc: StrictNum | None = None
    max_charge_ac_kw: StrictNum | None = None
    max_discharge_ac_kw: StrictNum | None = None
    charger_max_efficiency_pct: StrictNum | None = None
    charger_euro_efficiency_pct: StrictNum | None = None
    battery_inverter_max_efficiency_pct: StrictNum | None = None
    battery_inverter_euro_efficiency_pct: StrictNum | None = None
class GridAvailabilityProfile(_StrictModel):
    """Explicit 8760/8784-hour CSV or deterministically generated WeakGrid outage schedule."""
    mode: Literal["csv", "generated"] | None = None
    csv_name: StrictStr | None = None
    year: StrictInt
    utc_offset_hours: Annotated[StrictInt, Field(ge=-12, le=14)] | None = None
    time_basis: Literal["local_standard_interval_start"] | None = None
    outage_count: Annotated[StrictInt, Field(ge=1, le=365)] | None = None
    min_duration_hours: Annotated[StrictInt, Field(ge=1, le=168)] | None = None
    max_duration_hours: Annotated[StrictInt, Field(ge=1, le=168)] | None = None
    peak_hours: list[StrictInt] | None = None
    seed: Annotated[StrictInt, Field(ge=0)] | None = None
class BatteryPackDesign(_StrictModel):
    file_name: StrictStr
    library: Literal["workspace", "builtin"] = "workspace"
    series_count: StrictInt
    parallel_count: StrictInt
    monthly_temperature_c: list[StrictNum]
    cycle_sow: StrictNum
    static_sow: StrictNum
class GridStorageDesign(_GridStorageScheduleBase):
    strategy: Literal["self_consumption", "weak_grid", "peak_shaving", "power_shift"]
    soc_min: StrictNum
    soc_max: StrictNum
    initial_soc: StrictNum
    reserve_soc: StrictNum
    max_charge_ac_kw: StrictNum
    max_discharge_ac_kw: StrictNum
    charger_max_efficiency_pct: StrictNum
    charger_euro_efficiency_pct: StrictNum
    battery_inverter_max_efficiency_pct: StrictNum
    battery_inverter_euro_efficiency_pct: StrictNum
class ElectricalShadingSpec(_StrictModel):
    """3D ground partition-based electrical shading options (PVsyst 8.1 pvShading Flags=$000A0300)."""
    mode: Literal["linear", "partition"] = "partition"
    partitions_x: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None
    partitions_y: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None
    electrical_effect_fraction: Annotated[StrictNum, Field(ge=0.0, le=1.0)] | None = None
    auto_half_cut: StrictBool | None = None

    @model_validator(mode="after")
    def _validate_linear_rejects_partition_opts(self):
        if self.mode == "linear":
            disallowed = [
                k for k in ("partitions_x", "partitions_y", "electrical_effect_fraction", "auto_half_cut")
                if getattr(self, k) is not None
            ]
            if disallowed:
                raise ValueError(
                    "electrical_shading mode='linear' cannot set partition-only option(s): " + ", ".join(disallowed)
                )
        return self
