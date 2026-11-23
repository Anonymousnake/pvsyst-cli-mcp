"""Strict wire request models; no native execution."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator


StrictNum = StrictInt | StrictFloat
_STRICT_CONFIG = ConfigDict(extra="forbid", allow_inf_nan=False)

class GeneratorUpdates(BaseModel):
    model_config = _STRICT_CONFIG
    enabled: StrictBool | None = None
    filename: str | None = None
    operating_power_kw: StrictNum | None = None
    thresholds: dict[str, dict[str, StrictNum]] | None = None

class SoilingUpdates(BaseModel):
    """Explicit native soiling mode and GUI-precision percentage values."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "constant", "monthly"]
    yearly_loss_pct: StrictNum | None = None
    monthly_loss_pct: list[StrictNum] | None = None

class UnavailabilityPeriod(BaseModel):
    """Explicit PVsyst planned outage start timestamp (DD/MM/YY HH:MM) and duration in hours."""
    model_config = _STRICT_CONFIG
    date_beg: StrictStr
    duration_hours: StrictNum

class BifacialUpdates(BaseModel):
    """Mount, update, or remove PVObject_Bifacial=pvBiFacial on an orientation."""
    model_config = _STRICT_CONFIG
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


class SpectralUpdates(BaseModel):
    """Enable, configure, or disable native PVsyst spectral correction (First Solar / precipitable water)."""
    model_config = _STRICT_CONFIG
    enabled: StrictBool
    coeff_set: Literal["SiMono", "SiPoly", "CdTe", "CIS", "aSi", "Custom"] = "SiMono"
    use_default_coeffs: StrictBool | None = None
    use_rel_hum: StrictBool = True
    coefficients: Annotated[list[StrictNum], Field(min_length=6, max_length=6)] | None = None


class NativeEconomicsUpdates(BaseModel):
    """Write or remove native EconomicEval=TEcoEvaluation so PVsyst PDF reports include financial pages."""
    model_config = _STRICT_CONFIG
    mode: Literal["set", "none"] = "set"
    project_life_years: Annotated[StrictInt, Field(ge=1, le=60)] = 25
    currency: StrictStr = "USD"
    capex_total: StrictNum | None = None
    annual_opex: StrictNum = 0.0
    subsidies: StrictNum = 0.0
    discount_rate_pct: StrictNum = 5.0
    inflation_rate_pct: StrictNum = 2.0
    sale_tariff: StrictNum = 0.08
    consumption_tariff: StrictNum = 0.15
    tariff_escalation_pct: StrictNum = 0.0
    inverter_lifetime_years: StrictNum = 12.0
    battery_lifetime_years: StrictNum = 10.0
    fuel_price_per_liter: StrictNum = 1.50


class OrientationConvertParams(BaseModel):
    """Target orientation geometry parameters for pvsyst_convert_orientation."""
    model_config = _STRICT_CONFIG
    tilt_deg: StrictNum | None = None
    azimuth_deg: StrictNum | None = None
    base_slope_deg: StrictNum | None = None
    summer_tilt_deg: StrictNum | None = None
    winter_tilt_deg: StrictNum | None = None
    winter_months: list[StrictInt] | None = None
    pitch_m: StrictNum | None = None
    collector_width_m: StrictNum | None = None
    shed_count: StrictInt | None = None
    axis_azimuth_deg: StrictNum | None = None
    min_phi_deg: StrictNum | None = None
    max_phi_deg: StrictNum | None = None
    min_tilt_deg: StrictNum | None = None
    max_tilt_deg: StrictNum | None = None
    backtracking: StrictBool | None = None


class SystemUnavailabilityUpdates(BaseModel):
    """Configure explicit outage periods, random outage settings, or remove existing native PV-system outages."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "periods", "random"]
    periods: Annotated[list[UnavailabilityPeriod], Field(min_length=1, max_length=1000)] | None = None
    time_fraction_pct: Annotated[StrictNum, Field(gt=0.0, le=100.0)] | None = None
    period_count: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None
    min_duration_hours: Annotated[StrictInt, Field(ge=1, le=8760)] | None = None
    max_duration_hours: Annotated[StrictInt, Field(ge=1, le=8760)] | None = None
    random_seed: Annotated[StrictInt, Field(ge=0, le=999999)] | None = None

class GridLimitUpdates(BaseModel):
    """Native constant active-power limit at the grid injection point."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "constant"]
    limit_kw: StrictNum | None = None

class AuxiliaryUpdates(BaseModel):
    """Explicit native whole-system operating/night auxiliary powers in kW."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "constant", "profile"]
    operating_kw: StrictNum | None = None
    night_kw: StrictNum | None = None
    operating_threshold_kw: StrictNum | None = None
    proportional_w_per_kw: StrictNum | None = None
    proportional_threshold_kw: StrictNum | None = None

class SeasonalUpdates(BaseModel):
    """Explicit native summer/winter angles and winter-month selection."""
    model_config = _STRICT_CONFIG
    summer_tilt: StrictNum
    winter_tilt: StrictNum
    winter_months: list[StrictInt]
    orientation_name: StrictStr | None = None

class HorizonUpdates(BaseModel):
    """Native far-horizon points in PVsyst azimuth/elevation degrees."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "profile"]
    points: list[list[StrictNum]] | None = None
    profile_name: StrictStr | None = None

class InverterTemperatureUpdates(BaseModel):
    """Explicit native temperature mode and both saved temperature values in degC."""
    model_config = _STRICT_CONFIG
    mode: Literal["ambient", "offset", "fixed"]
    offset_c: StrictNum | None = None
    fixed_c: StrictNum | None = None

class AgeingUpdates(BaseModel):
    """Existing activated native template; inputs and recorded RMS/retention controls only."""
    model_config = _STRICT_CONFIG
    simulation_year: StrictInt
    annual_module_degradation_percent: StrictNum
    keep_mc_values: StrictBool | None = None
    modules_rms_year_isc_percent: StrictNum | None = None
    modules_rms_year_voc_percent: StrictNum | None = None

class IamUpdates(BaseModel):
    """Component IAM bit or the ASHRAE model; PAN curve data is never written."""
    model_config = _STRICT_CONFIG
    mode: Literal["component", "ashrae"]
    ashrae_b0: StrictNum | None = None

class StringMismatchUpdates(BaseModel):
    """Native string-voltage-mismatch default bit or one explicit manual percentage."""
    model_config = _STRICT_CONFIG
    mode: Literal["default", "constant"]
    loss_percent: StrictNum | None = None

class PowerFactorUpdates(BaseModel):
    """Explicit native signed tangent values; no cosine or reactive-power calculation."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "constant", "monthly"]
    pnom_limit: Literal["native", "active", "apparent"]
    tan_phi: StrictNum | None = None
    monthly_tan_phi: list[StrictNum] | None = None

class PowerControlLimitUpdates(BaseModel):
    """Explicit native limit quantity, application point and loss classification."""
    model_config = _STRICT_CONFIG
    mode: Literal["none", "constant"]
    quantity: Literal["active", "apparent"]
    point: Literal["injection", "inverter"]
    limit: StrictNum | None = None
    separate_loss: StrictBool | None = None

class PowerControlsUpdates(BaseModel):
    """Combined power factor and grid limit transaction."""
    model_config = _STRICT_CONFIG
    power_factor: PowerFactorUpdates
    grid_limit: PowerControlLimitUpdates

class P50P90Updates(BaseModel):
    """P50/P90 probabilistic simulation parameters on an existing result block."""
    model_config = _STRICT_CONFIG
    MeteoKind: Literal["SpecYear", "TMY"]
    P90Custom: StrictInt
    PxxCustom: StrictInt
    PxxReport: StrictBool
    PxxMainResults: StrictBool
    PlotKind: StrictInt
    YearDevFromAver: StrictNum
    MeteoAnnualVariab: StrictNum
    PVModelVariab: StrictNum
    InverterVariab: StrictNum
    SoilingVariab: StrictNum
    DegradVariab: StrictNum
    CustomVariab: StrictNum
    CustomVarLabel: StrictStr

class TrackingControls(BaseModel):
    """Explicit controls of the source native tracking type; absent fields remain inherited."""
    model_config = _STRICT_CONFIG
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

class ACCablingUpdates(BaseModel):
    """Explicit native cable geometry; omitted conductor/section inherit active source."""
    model_config = _STRICT_CONFIG
    length_m: StrictNum
    metal: Literal["Copper", "Aluminium"] | None = None
    section: Literal["ISO60228_10", "ISO60228_16", "ISO60228_25", "ISO60228_35", "ISO60228_50",
                     "ISO60228_70", "ISO60228_95", "ISO60228_120"] | None = None

class DCCablingUpdates(BaseModel):
    """DC wiring target: resistance (mOhm) or STC loss percentage."""
    model_config = _STRICT_CONFIG
    mode: Literal["resistance", "percent"]
    value: StrictNum

class ShedGeometryUpdates(BaseModel):
    """Explicit native Sheds/SunShields inputs; omitted values stay inherited."""
    model_config = _STRICT_CONFIG
    field_tilt_deg: StrictNum | None = None
    field_azimuth_deg: StrictNum | None = None
    pitch_m: StrictNum | None = None
    collector_width_m: StrictNum | None = None
    inactive_band_width_1_m: StrictNum | None = None
    inactive_band_width_2_m: StrictNum | None = None
    shed_count: StrictInt | None = None
    module_columns: StrictInt | None = None
    cell_width_m: StrictNum | None = None
    shading_factor: StrictNum | None = None
    electrical_effect: StrictBool | None = None

class FlatRoof(BaseModel):
    """Flat roof: the plate sits at the eave height."""
    model_config = _STRICT_CONFIG
    kind: Literal["flat"]

class GableRoof(BaseModel):
    """Symmetric gable roof with the ridge parallel to the house local X axis."""
    model_config = _STRICT_CONFIG
    kind: Literal["gable"]
    pitch_deg: StrictNum

RoofSpec = Annotated[FlatRoof | GableRoof, Field(discriminator="kind")]

class HouseSpec(BaseModel):
    """Rectangular footprint with an explicit eave height; no other shapes are supported."""
    model_config = _STRICT_CONFIG
    length_m: StrictNum
    width_m: StrictNum
    eave_height_m: StrictNum
    roof: RoofSpec

class PVTableLayout(BaseModel):
    """One fixed table; tilt and azimuth always come from the source variant."""
    model_config = _STRICT_CONFIG
    module_rows: StrictInt
    module_columns: StrictInt
    orientation: Literal["portrait", "landscape"]
    row_spacing_m: StrictNum
    column_spacing_m: StrictNum
    clearance_m: StrictNum
    edge_margin_m: StrictNum

class HouseMesh(BaseModel):
    """AI-authored named architectural geometry; indices are explicit, zero-based triangles."""
    model_config = _STRICT_CONFIG
    name: StrictStr
    vertices: list[Annotated[list[StrictNum], Field(min_length=3, max_length=3)]]
    triangles: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]

class MeshHouseSpec(BaseModel):
    """One house composed by the caller, not selected from a roof-template catalogue."""
    model_config = _STRICT_CONFIG
    kind: Literal["mesh"]
    meshes: list[HouseMesh]

class BuildingObstacle(BaseModel):
    """Surrounding flat- or gable-roofed building obstacle in the house local frame."""
    model_config = _STRICT_CONFIG
    kind: Literal["building"]
    name: StrictStr
    origin_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    length_m: StrictNum
    width_m: StrictNum
    height_m: StrictNum
    rotation_deg: StrictNum = 0.0
    roof: FlatRoof | GableRoof | None = None

class WallObstacle(BaseModel):
    """Vertical barrier/parapet wall extruded along an XY line segment."""
    model_config = _STRICT_CONFIG
    kind: Literal["wall"]
    name: StrictStr
    start_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    end_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    height_m: StrictNum
    thickness_m: StrictNum = 0.2

class MeshObstacle(BaseModel):
    """Arbitrary custom 3D obstacle mesh (vertices + triangles) in the house local frame."""
    model_config = _STRICT_CONFIG
    kind: Literal["mesh"]
    name: StrictStr
    vertices: list[Annotated[list[StrictNum], Field(min_length=3, max_length=3)]]
    triangles: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]

class TreeObstacle(BaseModel):
    """Native PVsyst parametric tree obstacle (ObjetElemType=oeTree)."""
    model_config = _STRICT_CONFIG
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

ObstacleSpec = Annotated[
    BuildingObstacle | WallObstacle | MeshObstacle | TreeObstacle,
    Field(discriminator="kind"),
]

class ModuleGeometry(BaseModel):
    """Explicit geometric metadata, checked against a readable loose PAN when present."""
    model_config = _STRICT_CONFIG
    model_name: StrictStr
    nom_power_wp: StrictNum
    width_m: StrictNum
    length_m: StrictNum
    geometry_depth_m: StrictNum

class PlacedPVTableLayout(BaseModel):
    """Explicit local placement over one named, planar, upward-facing roof surface mesh."""
    model_config = _STRICT_CONFIG
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
    # Optional explicit source SubArray owner when several source subarrays share one NoOrientation.
    source_subarray_id: StrictInt | None = None
    module_geometry: ModuleGeometry | None = None

class NamedPVTablesLayout(BaseModel):
    """List of 1..64 named PV table regions."""
    model_config = _STRICT_CONFIG
    tables: Annotated[list[NamedPVTableLayout], Field(min_length=1, max_length=64)]

class ModuleCellBinding(BaseModel):
    """One module cell (1-based row/col and global_string; 0 marks an unassigned draft cell)."""
    model_config = _STRICT_CONFIG
    row: Annotated[StrictInt, Field(ge=1)]
    col: Annotated[StrictInt, Field(ge=1)]
    global_string: Annotated[StrictInt, Field(ge=0)]

class TableStringBinding(BaseModel):
    """Per-table cell wiring keyed by table name and source subarray_id."""
    model_config = _STRICT_CONFIG
    name: StrictStr
    subarray_id: Annotated[StrictInt, Field(ge=1)]
    cells: list[ModuleCellBinding]

class StringLayoutBinding(BaseModel):
    """Explicit per-module string wiring across named PV tables."""
    model_config = _STRICT_CONFIG
    tables: Annotated[list[TableStringBinding], Field(min_length=1, max_length=64)]

class SynthesisOrientationSpec(BaseModel):
    """Optional fixed-plane orientation for zero-template house synthesis."""
    model_config = _STRICT_CONFIG
    id: Annotated[StrictInt, Field(ge=1)]
    tilt_deg: StrictNum
    azimuth_deg: StrictNum
    base_slope_deg: StrictNum = 0.0
    comment: StrictStr | None = None

class SynthesisSubArraySpec(BaseModel):
    """Optional subarray electrical declaration for zero-template house synthesis."""
    model_config = _STRICT_CONFIG
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

class HouseTaskRequest(BaseModel):
    """3D house task specification: authored house mesh, PV layout, optional source variant (or zero-template synthesis), and run options."""
    model_config = _STRICT_CONFIG
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
        has_proj = self.source_project is not None
        has_var = self.source_variant is not None
        synth_fields = [name for name in ("site_name", "met_name", "pan_name", "ond_name",
                                          "anchor_azimuth_deg", "orientations", "subarrays")
                        if getattr(self, name) is not None]
        if has_proj != has_var:
            raise ValueError("Provide both source_project and source_variant, or omit both for zero-template synthesis")
        if has_proj:
            if synth_fields:
                raise ValueError("Zero-template synthesis fields cannot be combined with source_project/source_variant: "
                                 + ", ".join(synth_fields))
        else:
            if not (self.site_name or "").strip() or not (self.met_name or "").strip():
                raise ValueError("Zero-template house task requires non-empty site_name (.SIT) and met_name (.MET) "
                                 "when source_project/source_variant are omitted")
        return self

class ConstantNativeLoad(BaseModel):
    """Native fixed constant consumption in kW; unknown fields stop at the boundary."""
    model_config = _STRICT_CONFIG
    mode: Literal["constant"]
    constant_kw: StrictNum

class MonthlyNativeLoad(BaseModel):
    """Native monthly loads as twelve declared kWh values; never derived from monthly power."""
    model_config = _STRICT_CONFIG
    mode: Literal["monthly"]
    monthly_energy_kwh: list[StrictNum] = Field(min_length=12, max_length=12)

class DailyNativeLoad(BaseModel):
    """Native daily profile as 24 hourly powers; the trailing slot is native format only."""
    model_config = _STRICT_CONFIG
    mode: Literal["daily"]
    daily_power_kw: list[StrictNum] = Field(min_length=24, max_length=24)

class HourlyNativeLoad(BaseModel):
    """Prepared 8760/8784 hourly load manifest bound as an embedded MJ base62 snapshot."""
    model_config = _STRICT_CONFIG
    mode: Literal["hourly"]
    load_manifest_name: StrictStr

class HouseholdApplianceSpec(BaseModel):
    """One household appliance entry for LoadType=HouseHold."""
    model_config = _STRICT_CONFIG
    name: StrictStr
    count: Annotated[StrictInt, Field(ge=1, le=10000)] = 1
    power_w: StrictNum
    hours: list[StrictInt] | None = None
    half_hours: list[StrictInt] | None = None
    slot_index: Annotated[StrictInt, Field(ge=1, le=10)] | None = None


class HouseholdNativeLoad(BaseModel):
    """Native household appliance schedule (LoadType=HouseHold, yearly or seasonal)."""
    model_config = _STRICT_CONFIG
    mode: Literal["household"]
    modulation: Literal["yearly", "seasonal"] = "yearly"
    appliances: Annotated[list[HouseholdApplianceSpec], Field(min_length=1, max_length=10)] | None = None
    seasons: dict[Literal["Summer", "Autumn", "Winter", "Spring"], Annotated[list[HouseholdApplianceSpec], Field(min_length=1, max_length=10)]] | None = None


class ProbabilityNativeLoad(BaseModel):
    """Hourly probability distribution load (deterministically sampled into native embedded hourly snapshot)."""
    model_config = _STRICT_CONFIG
    mode: Literal["probability"]
    power_step_kw: StrictNum
    probabilities_pct: Annotated[list[StrictNum], Field(min_length=2, max_length=24)]
    seed: Annotated[StrictInt, Field(ge=0)] = 42
    year: Annotated[StrictInt, Field(ge=1969, le=2068)] = 1990


NativeLoadRequest = Annotated[
    ConstantNativeLoad | MonthlyNativeLoad | DailyNativeLoad | HourlyNativeLoad | HouseholdNativeLoad | ProbabilityNativeLoad,
    Field(discriminator="mode"),
]

class GridStorageUpdates(BaseModel):
    """Native existing AC-coupled storage controls and schedules; unknown fields stop at the boundary."""
    model_config = _STRICT_CONFIG
    coupling: Literal["ac_coupled"] = "ac_coupled"
    strategy: Literal["self_consumption", "weak_grid", "peak_shaving", "power_shift"] | None = None
    charging_strategy: Literal[
        "pv_available", "pv_above_thresh", "pv_to_grid_min", "pv_prod_fraction", "spec_charg_power"
    ] | None = None
    discharging_strategy: Literal[
        "immediate", "after_sunset", "night", "hour_window", "specified_hours"
    ] | None = None
    grid_limit_kw: StrictNum | None = None
    charging_pv_fraction: StrictNum | None = None
    charging_power_kw: StrictNum | None = None
    charging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharging_power_kw: StrictNum | None = None
    discharging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharge_start_hour: StrictInt | None = None
    discharge_end_hour: StrictInt | None = None
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

class GridAvailabilityProfile(BaseModel):
    """Explicit 8760/8784-hour CSV or deterministically generated WeakGrid outage schedule."""
    model_config = _STRICT_CONFIG
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

class BatteryPackDesign(BaseModel):
    model_config = _STRICT_CONFIG
    file_name: StrictStr
    library: Literal["workspace", "builtin"] = "workspace"
    series_count: StrictInt
    parallel_count: StrictInt
    monthly_temperature_c: list[StrictNum]
    cycle_sow: StrictNum
    static_sow: StrictNum

class GridStorageDesign(BaseModel):
    model_config = _STRICT_CONFIG
    coupling: Literal["ac_coupled"] = "ac_coupled"
    strategy: Literal["self_consumption", "weak_grid", "peak_shaving", "power_shift"]
    charging_strategy: Literal[
        "pv_available", "pv_above_thresh", "pv_to_grid_min", "pv_prod_fraction", "spec_charg_power"
    ] | None = None
    discharging_strategy: Literal[
        "immediate", "after_sunset", "night", "hour_window", "specified_hours"
    ] | None = None
    grid_limit_kw: StrictNum | None = None
    charging_pv_fraction: StrictNum | None = None
    charging_power_kw: StrictNum | None = None
    charging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharging_power_kw: StrictNum | None = None
    discharging_schedule_48: Annotated[list[StrictNum], Field(min_length=48, max_length=48)] | None = None
    discharge_start_hour: StrictInt | None = None
    discharge_end_hour: StrictInt | None = None
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

class CapacityRequest(BaseModel):
    model_config = _STRICT_CONFIG
    target_dc_kw: StrictInt | StrictFloat | None = None
    min_dc_ac_ratio: StrictInt | StrictFloat | None = None
    max_dc_ac_ratio: StrictInt | StrictFloat | None = None
    capacity_tolerance_pct: StrictInt | StrictFloat = 5
    voltage_standard: StrictStr | None = None
    cold_voc_v: StrictInt | StrictFloat | None = None
    hot_vmp_v: StrictInt | StrictFloat | None = None
    cold_vmp_v: StrictInt | StrictFloat | None = None
    module_max_operating_current_a: StrictInt | StrictFloat | None = None
    module_max_short_circuit_current_a: StrictInt | StrictFloat | None = None
    mppt_max_operating_current_a: StrictInt | StrictFloat | None = None
    mppt_max_short_circuit_current_a: StrictInt | StrictFloat | None = None
    design_min_temperature_c: StrictInt | StrictFloat | None = None
    design_min_operating_temperature_c: StrictInt | StrictFloat | None = None
    design_max_operating_temperature_c: StrictInt | StrictFloat | None = None
    design_basis: StrictStr | None = None

class LossRequest(BaseModel):
    """Optional template loss overrides; only these four parameters are accepted.

    LIDLoss, PMismMPP and ModuleQualityLoss target the subarray, UFactor targets the
    whole main array. Omitted keys keep their template value and are never fitted.
    """
    model_config = _STRICT_CONFIG
    LIDLoss: StrictNum | None = None
    PMismMPP: StrictNum | None = None
    ModuleQualityLoss: StrictNum | None = None
    UFactor: StrictNum | None = None

class DeclaredValue(BaseModel):
    """One design value, optionally with its declared unit and manufacturer source.

    A bare number is also accepted, but module and MPPT current limits without a source
    are reported as issues by the design backend instead of being assumed.
    """
    model_config = _STRICT_CONFIG
    value: StrictNum | None = None
    unit: StrictStr | None = None
    source: StrictStr | None = None

class CoefficientDeclaration(BaseModel):
    """A temperature coefficient that always states its unit and its source.

    The unit of a serialized component coefficient is never assumed, and both fields are
    therefore required.
    """
    model_config = _STRICT_CONFIG
    value: StrictNum
    unit: StrictStr
    source: StrictStr

class EquipmentRequest(BaseModel):
    """The exact loose component identity, or one explicit workspace filename.

    Every field is optional, and an omitted field stays null: the design backend decides
    whether one exact manufacturer+model pair or a filename was supplied, so an empty
    string is never mistaken for a filled-in identity.
    """
    model_config = _STRICT_CONFIG
    file_name: StrictStr | None = None
    manufacturer: StrictStr | None = None
    model: StrictStr | None = None

class WeatherRequest(BaseModel):
    """Where the simulated weather comes from; no mode is picked for the caller.

    mode='synthetic_from_site' runs PVsystCLI with --synthetic-weather directly from a
    workspace .SIT file without requiring a pre-built hourly .MET file.
    Text fields default to null so a missing SIT/MET/CSV name, country code or place
    label is reported by the backend as an unsupplied input instead of an empty value.
    """
    model_config = _STRICT_CONFIG
    mode: Literal["existing", "synthetic_from_site", "csv", "open_meteo", "pvgis_tmy", "nasa_power"] = "existing"
    sit_name: StrictStr | None = None
    met_name: StrictStr | None = None
    csv_name: StrictStr | None = None
    country_code: StrictStr | None = None
    time_basis: StrictStr | None = None
    location_id: StrictInt | None = None
    latitude: StrictNum | None = None
    longitude: StrictNum | None = None
    altitude: StrictNum | None = None
    site_label: StrictStr | None = None
    timezone_name: StrictStr | None = None
    temperature_method: StrictStr = "endpoint_mean"
    weather_manifest_name: StrictStr | None = None
    strict_dhi: StrictBool = False

class TemperatureRequest(BaseModel):
    """Design temperatures in degC; min_ambient_basis names its source or method."""
    model_config = _STRICT_CONFIG
    min_ambient_c: StrictNum | None = None
    min_ambient_basis: StrictStr | None = None
    min_module_operating_c: StrictNum | None = None
    max_module_operating_c: StrictNum | None = None
    annual_min_ambient_c: StrictNum | None = None

class EnvelopeRequest(BaseModel):
    """Direct manufacturer voltage values, each optionally carrying unit and source."""
    model_config = _STRICT_CONFIG
    cold_voc_v: StrictNum | DeclaredValue | None = None
    cold_vmp_v: StrictNum | DeclaredValue | None = None
    hot_vmp_v: StrictNum | DeclaredValue | None = None

class CoefficientsRequest(BaseModel):
    """Declared temperature coefficients, each with an explicit unit and source."""
    model_config = _STRICT_CONFIG
    beta_voc: CoefficientDeclaration | None = None
    beta_vmp: CoefficientDeclaration | None = None
    alpha_isc: CoefficientDeclaration | None = None
    alpha_imp: CoefficientDeclaration | None = None

class CurrentLimitRequest(BaseModel):
    """Declared manufacturer current limits, as a value or a declaring object."""
    model_config = _STRICT_CONFIG
    max_operating_current_a: StrictNum | DeclaredValue | None = None
    max_short_circuit_current_a: StrictNum | DeclaredValue | None = None

class EffectiveIrradianceRequest(BaseModel):
    """The maximum effective irradiance and rear gain used by a coefficient envelope."""
    model_config = _STRICT_CONFIG
    max_irradiance_w_m2: StrictNum | None = None
    rear_gain_factor: StrictNum | None = None
    source: StrictStr | None = None

class DesignEnvelopeRequest(BaseModel):
    """The bounded design-envelope inputs; unknown keys are rejected, not ignored.

    Every value is either a strict number or a declared {value, unit, source} object.
    Omitted fields stay empty and are returned as concrete issues by the backend, so an
    empty object never becomes a ready design.
    """
    model_config = _STRICT_CONFIG
    manufacturer_model: StrictStr | None = None
    target_dc_kw: StrictNum | None = None
    max_dc_ac_ratio: StrictNum | None = None
    voltage_standard: StrictStr | None = None
    capacity_tolerance_pct: StrictNum | None = None
    temperatures: TemperatureRequest | None = None
    envelope: EnvelopeRequest | None = None
    coefficients: CoefficientsRequest | None = None
    module_limits: CurrentLimitRequest | None = None
    mppt_limits: CurrentLimitRequest | None = None
    effective_irradiance: EffectiveIrradianceRequest | None = None
    design_basis_extra: StrictStr | None = None

class TerrainSpec(BaseModel):
    """3D terrain DEM specification (inline XYZ points, workspace CSV, or optional GeoTIFF .tif)."""
    model_config = _STRICT_CONFIG
    name: StrictStr = "Terrain_DEM"
    coordinate_system: Literal["local_m", "lonlat"] = "local_m"
    points: Annotated[list[list[StrictNum]], Field(min_length=3, max_length=2500)] | None = None
    csv_name: StrictStr | None = None
    geotiff_name: StrictStr | None = None
    max_grid_dim: Annotated[StrictInt, Field(ge=4, le=60)] = 25
    drape_tables: StrictBool = True
    follow_slope: StrictBool = True
    max_base_slope_deg: Annotated[StrictNum, Field(ge=0.0, le=45.0)] = 20.0
    max_axis_tilt_deg: Annotated[StrictNum, Field(ge=0.0, le=30.0)] = 15.0


class GroundSubOrientationSpec(BaseModel):
    """One slope/aspect orientation in a multi-orientation unlimited ground_sheds plant."""
    model_config = _STRICT_CONFIG
    label: StrictStr | None = None
    tilt: StrictNum
    azimuth: StrictNum
    target_dc_kw: StrictNum | None = None
    weight: StrictNum | None = None
    pitch_m: StrictNum | None = None
    collector_width_m: StrictNum | None = None
    shed_count: Annotated[StrictInt, Field(ge=1, le=1000)] | None = None
    inactive_band_width_1_m: StrictNum | None = None
    inactive_band_width_2_m: StrictNum | None = None
    electrical_effect: StrictBool | None = None
    shading_factor: StrictNum | None = None
    cell_width_m: StrictNum | None = None
    base_slope_deg: StrictNum | None = None


class GroundLayoutRequest(BaseModel):
    """Ground multi-row fixed sheds or horizontal N-S single-axis tracker geometry."""
    model_config = _STRICT_CONFIG
    model_mode: Literal["unlimited", "3d"] = "unlimited"
    pitch_m: StrictNum | None = None
    collector_width_m: StrictNum | None = None
    shed_count: Annotated[StrictInt, Field(ge=1, le=1000)] = 10
    module_rows: Annotated[StrictInt, Field(ge=1, le=20)] = 2
    module_columns: Annotated[StrictInt, Field(ge=1, le=500)] = 25
    module_orientation: Literal["portrait", "landscape"] = "portrait"
    spacing_x_m: StrictNum = 0.02
    spacing_y_m: StrictNum = 0.02
    inactive_band_width_1_m: StrictNum | None = None
    inactive_band_width_2_m: StrictNum | None = None
    electrical_effect: StrictBool | None = None
    shading_factor: StrictNum | None = None
    cell_width_m: StrictNum | None = None
    axis_azimuth_deg: StrictNum | None = None
    min_phi_deg: StrictNum | None = None
    max_phi_deg: StrictNum | None = None
    backtracking: StrictBool | None = None
    irrad_optimiz: StrictBool | None = None
    central_gap_m: StrictNum | None = None
    pitch_slope_deg: StrictNum | None = None
    wind_stow: StrictBool | None = None
    wind_stow_speed_ms: StrictNum | None = None
    wind_stow_position_deg: StrictNum | None = None
    site_polygon: Annotated[list[list[StrictNum]], Field(min_length=3, max_length=64)] | None = None
    setback_m: StrictNum | None = None
    ground_clearance_m: StrictNum | None = None
    max_table_columns: Annotated[StrictInt, Field(ge=1, le=500)] | None = None
    obstacles: Annotated[list[ObstacleSpec], Field(min_length=1, max_length=64)] | None = None
    base_slope_deg: StrictNum | None = None
    terrain: TerrainSpec | None = None
    sub_orientations: Annotated[list[GroundSubOrientationSpec], Field(min_length=1, max_length=16)] | None = None


class AutoSizingOptions(BaseModel):
    """Optional bounds used when sizing_mode='auto_from_components' derives capacity from PAN/OND."""
    model_config = _STRICT_CONFIG
    voltage_standard: Literal["IEC", "UL"] = "IEC"
    min_ambient_c: StrictNum = -10.0
    min_operating_c: StrictNum = 0.0
    max_operating_c: StrictNum = 65.0
    min_dc_ac_ratio: StrictNum = 0.85
    max_dc_ac_ratio: StrictNum = 1.25
    capacity_tolerance_pct: StrictNum = 8.0
    beta_voc_pct_per_c: StrictNum | None = None
    beta_vmp_pct_per_c: StrictNum | None = None
    alpha_isc_pct_per_c: StrictNum | None = None


class TransformerCableSpec(BaseModel):
    """MV or HV outgoing collection/transmission cable specification."""
    model_config = _STRICT_CONFIG
    length_m: Annotated[StrictNum, Field(ge=0.0, le=100000.0)] = 0.0
    metal: Literal["Copper", "Aluminium"] = "Copper"
    section_mm2: StrictInt = 150


class MvTransformerSpec(BaseModel):
    """Medium-voltage transformer stage specification (single or parallel units).

    In fractional mode (default), PVsystCLI 8.1.7 scales iron_loss_frac and copper_loss_frac
    against inverter nominal AC power (PNomInvAC), not stc_ref_power_kw. Provide
    nominal_power_kva, iron_loss_kw, and copper_loss_kw together (per transformer) for
    explicit datasheet losses (validated against PVsyst's 80%-200% PNomInvAC rating bounds,
    strictly positive datasheet values, and <=1% iron / <=5% copper limits).
    """
    model_config = _STRICT_CONFIG
    count: Annotated[StrictInt, Field(ge=1, le=200)] = 1
    output_voltage_kv: Annotated[StrictNum, Field(ge=1.0, le=66.0)] = 20.0
    stc_ref_power_kw: StrictNum | None = None
    iron_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.05)] = 0.001
    copper_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.15)] = 0.010
    nominal_power_kva: StrictNum | None = None
    iron_loss_kw: StrictNum | None = None
    copper_loss_kw: StrictNum | None = None
    night_disconnect: StrictBool = False
    cable: TransformerCableSpec | None = None


class HvTransformerSpec(BaseModel):
    """High-voltage substation transformer stage nested above MV transformers.

    PVsystCLI 8.1.7 always simulates HV transformers from datasheet fields (HVTrfDS*);
    iron_loss_frac / copper_loss_frac are converted to equivalent nameplate kW values.
    Validated against PVsyst's 80%-200% PNomInvAC rating bounds, strictly positive losses,
    <=1% iron / <=5% copper of nominal kVA, and hv.output_voltage_kv >= mv.output_voltage_kv.
    """
    model_config = _STRICT_CONFIG
    output_voltage_kv: Annotated[StrictNum, Field(ge=30.0, le=500.0)] = 110.0
    stc_ref_power_kw: StrictNum | None = None
    nominal_power_kva: StrictNum | None = None
    iron_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.05)] | None = None
    copper_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.15)] | None = None
    iron_loss_kw: StrictNum | None = None
    copper_loss_kw: StrictNum | None = None
    night_disconnect: StrictBool = False
    cable: TransformerCableSpec | None = None


class TransformersConfig(BaseModel):
    """Optional MV or MV+HV cascaded transformer configuration for grid designs."""
    model_config = _STRICT_CONFIG
    mv: MvTransformerSpec
    hv: HvTransformerSpec | None = None


class StandaloneGensetSpec(BaseModel):
    """Inline backup generator specification (synthesizes a .GEN file in ComposPV/Gensets)."""
    model_config = _STRICT_CONFIG
    file_name: StrictStr | None = None
    model: StrictStr | None = None
    manufacturer: StrictStr = "Generic"
    fuel_type: Literal["Diesel", "Gas", "Essence", "Biogas", "Other"] = "Diesel"
    nominal_power_kw: Annotated[StrictNum, Field(gt=0, le=100000)]
    fuel_consumption_l_per_h: Annotated[StrictNum, Field(gt=0, le=50000)]


class StandaloneEconomicsSpec(BaseModel):
    """Optional off-grid economics parameters including diesel fuel and genset maintenance OPEX."""
    model_config = _STRICT_CONFIG
    capex_total: Annotated[StrictNum, Field(ge=0)]
    annual_om_opex: Annotated[StrictNum, Field(ge=0)] = 0.0
    fuel_price_per_liter: Annotated[StrictNum, Field(ge=0)] = 1.50
    genset_opex_per_hour: Annotated[StrictNum, Field(ge=0)] = 0.0
    genset_fuel_rate_l_per_kwh: Annotated[StrictNum, Field(ge=0)] | None = None
    avoided_tariff_per_kwh: Annotated[StrictNum, Field(ge=0)] = 0.0
    financial: dict[str, Any] | None = None


class StandaloneDesignSpec(BaseModel):
    """Off-grid Stand-Alone PV + BESS + Controller + optional Backup Genset specification (SystemType=Battery)."""
    model_config = _STRICT_CONFIG
    project_comment: StrictStr | None = None
    variant_comment: StrictStr | None = None
    battery_file: StrictStr
    n_mod_serie: Annotated[StrictInt, Field(ge=1)] = 2
    n_string_ch: Annotated[StrictInt, Field(ge=1)] = 4
    n_batt_serie: Annotated[StrictInt, Field(ge=1)] = 4
    n_batt_parall: Annotated[StrictInt, Field(ge=1)] = 2
    controller_type: Literal["mppt", "direct"] = "mppt"
    controller_file: StrictStr | None = None
    mppt_effic_max_pct: Annotated[StrictNum, Field(ge=50, le=99.9)] = 96.0
    mppt_effic_euro_pct: Annotated[StrictNum, Field(ge=50, le=99.9)] = 94.0
    v_ch_decl: Annotated[StrictNum, Field(ge=0.5, le=1.0)] = 0.90
    v_ch_encl: Annotated[StrictNum, Field(ge=0.2, le=1.0)] = 0.75
    v_dech_decl: Annotated[StrictNum, Field(ge=0.05, le=0.8)] = 0.20
    v_dech_encl: Annotated[StrictNum, Field(ge=0.05, le=0.95)] = 0.45
    v_bkup_encl: Annotated[StrictNum, Field(ge=0.05, le=0.9)] = 0.25
    v_bkup_decl: Annotated[StrictNum, Field(ge=0.06, le=0.98)] = 0.55
    batt_voltage_v: Annotated[StrictNum, Field(ge=12, le=1500)] = 48.0
    autonomy_days: Annotated[StrictNum, Field(ge=0.2, le=30)] = 3.0
    target_lol_pct: Annotated[StrictNum, Field(ge=0, le=50)] = 5.0
    batt_temp_c: StrictNum | list[StrictNum] = 20.0
    genset_file: StrictStr | None = None
    genset_spec: StandaloneGensetSpec | None = None
    backup_operating_power_kw: Annotated[StrictNum, Field(gt=0, le=100000)] | None = None
    load: dict[str, Any] | None = None
    daily_load_kwh: Annotated[StrictNum, Field(gt=0)] | None = None
    economics: StandaloneEconomicsSpec | None = None


class SingleDesignRequest(BaseModel):
    """One bounded design request; every nested object rejects unknown keys.

    scope selects fixed_unshaded_grid (default), ground_sheds (unlimited or 3D multi-row sheds),
    ground_tracker (unlimited or 3D horizontal N-S single-axis trackers), or stand_alone
    (off-grid SystemType=Battery PV+BESS+Controller+optional Backup Genset).
    sizing_mode='strict' (default) requires explicit capacity or electrical_design;
    sizing_mode='auto_from_components' estimates capacity from readable PAN/OND files and
    marks the output needs_review with full assumptions.
    """
    model_config = _STRICT_CONFIG
    new_project: StrictStr = ""
    year: StrictInt | None = None
    target_dc_kw: StrictNum | None = None
    tilt: StrictNum | None = None
    azimuth: StrictNum | None = None
    scope: Literal["fixed_unshaded_grid", "ground_sheds", "ground_tracker", "stand_alone"] = "fixed_unshaded_grid"
    sizing_mode: Literal["strict", "auto_from_components"] = "strict"
    auto_sizing: AutoSizingOptions | None = None
    module: EquipmentRequest = Field(default_factory=EquipmentRequest)
    inverter: EquipmentRequest = Field(default_factory=EquipmentRequest)
    template_project: StrictStr | None = None
    variant: StrictStr | None = None
    weather: WeatherRequest = Field(default_factory=WeatherRequest)
    capacity: CapacityRequest | None = None
    electrical_design: DesignEnvelopeRequest | None = None
    losses: LossRequest | None = None
    report_pages: list[StrictStr] | None = None
    report_language: Literal["en", "zh"] = "en"
    ground_layout: GroundLayoutRequest | None = None
    bifacial: BifacialUpdates | None = None
    transformers: TransformersConfig | None = None
    standalone: StandaloneDesignSpec | None = None

class SourceTextRequest(BaseModel):
    """One caller-declared source; its text is checked here and never returned."""
    model_config = _STRICT_CONFIG
    kind: Literal["manufacturer", "site", "assumption"]
    title: Annotated[StrictStr, Field(min_length=1)]
    locator: Annotated[StrictStr, Field(min_length=1)]
    text: Annotated[StrictStr, Field(min_length=1)]
    model: StrictStr | None = None

class SourceDeclarationRequest(BaseModel):
    """One claimed value with the exact citation text and tokens it must agree with."""
    model_config = _STRICT_CONFIG
    field: StrictStr
    value: StrictNum
    unit: StrictStr
    source_id: StrictStr
    excerpt: StrictStr
    source_value_text: StrictStr
    source_unit_text: StrictStr
    page: StrictInt | None = None

class SourcedDesignGoals(BaseModel):
    """Explicit engineering goals only; no envelope, coefficient or limit is injected here."""
    model_config = _STRICT_CONFIG
    target_dc_kw: StrictNum | None = None
    max_dc_ac_ratio: StrictNum | None = None
    capacity_tolerance_pct: StrictNum | None = None
    voltage_standard: Literal["IEC", "UL"] | None = None
    design_basis_extra: StrictStr | None = None

class SourcedHouseTaskRequest(BaseModel):
    """Cited source inputs plus house geometry, layout, and optional prepare_mode."""
    model_config = _STRICT_CONFIG
    design: SingleDesignRequest
    new_project: StrictStr
    variant_name: StrictStr
    house: MeshHouseSpec
    layout: NamedPVTablesLayout | PlacedPVTableLayout
    obstacles: Annotated[list[ObstacleSpec], Field(min_length=1, max_length=64)] | None = None
    string_layout: StringLayoutBinding | None = None
    sources: dict[str, SourceTextRequest] = Field(default_factory=dict)
    declarations: list[SourceDeclarationRequest] = Field(default_factory=list)
    goals: SourcedDesignGoals | None = None
    module_geometry: ModuleGeometry | None = None
    report_pdf: StrictBool | None = None
    shading_mode: Literal["linear", "module_layout"] | None = None
    scene_import_method: Literal["native"] | None = None
    allow_window_creation: StrictBool | None = None
    prepare_mode: Literal["preserve_source_hardware"] | None = None
    source_project: StrictStr | None = None
    source_variant: StrictStr | None = None

    @model_validator(mode="after")
    def _validate_preserve_source_hardware(self):
        """Ensure preserve_source_hardware provides a source project/variant, and legacy calls do not."""
        if self.prepare_mode != "preserve_source_hardware":
            if self.source_project is not None or self.source_variant is not None:
                raise ValueError("source_project/source_variant require prepare_mode='preserve_source_hardware'; "
                                 "a legacy request never silently ignores them")
            return self
        if not (self.source_project or "").strip() or not (self.source_variant or "").strip():
            raise ValueError("preserve_source_hardware requires non-empty source_project and source_variant")
        extra = set(self.design.model_fields_set) - {"year", "report_pages"}
        if extra:
            raise ValueError("preserve_source_hardware keeps the source hardware verbatim and accepts only "
                             f"design.year and design.report_pages; remove {sorted(extra)}")
        if self.design.year is None:
            raise ValueError("preserve_source_hardware requires an explicit design.year in 1969..2068")
        if self.sources or self.declarations or self.goals is not None:
            raise ValueError("preserve_source_hardware reuses the existing source; sources/declarations/goals "
                             "are a source rewrite and are rejected before any native query or write")
        return self

class EngineeringChanges(BaseModel):
    model_config = _STRICT_CONFIG
    tilt: StrictNum | None = None
    azimuth: StrictNum | None = None
    losses: LossRequest | None = None
    tracking: TrackingControls | None = None
    storage: GridStorageUpdates | None = None

class EngineeringRevisionRequest(BaseModel):
    model_config = _STRICT_CONFIG
    source_project: StrictStr
    source_variant: StrictStr
    new_project: StrictStr
    new_variant: StrictStr
    objective: StrictStr
    year: StrictInt
    nominal_dc_power_kw: StrictNum
    power_source: StrictStr
    changes: EngineeringChanges
    declarations: dict[StrictStr, Any] | None = None
    report_pages: list[StrictStr] | None = None
    load_manifest_name: StrictStr = ""
    availability: GridAvailabilityProfile | None = None

class EngineeringScenario(BaseModel):
    model_config = _STRICT_CONFIG
    label: StrictStr
    project: StrictStr
    variant: StrictStr
    csv_name: StrictStr
    declared_changes: list[StrictStr]
    run_manifest_name: StrictStr = ""
    year: StrictInt | None = None
    nominal_dc_power_kw: StrictNum | None = None
    assumptions: list[StrictStr] | None = None

class EngineeringBlock(BaseModel):
    model_config = _STRICT_CONFIG
    id: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    source_subarray_id: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    count: Annotated[StrictInt, Field(ge=1, le=100)]


class BatchSweepRequest(BaseModel):
    """Parametric PVsyst UserBatch sweep specification (Cartesian grid or explicit scenarios)."""
    model_config = _STRICT_CONFIG
    project: StrictStr
    variant: StrictStr
    sweep_name: StrictStr
    grid: dict[StrictStr, list[Any]] | None = None
    scenarios: list[dict[StrictStr, Any]] | None = None
    result_variables: list[StrictStr] | None = None
    sort_by: StrictStr = "E_Grid"
    sort_descending: StrictBool = True
    export_hourly: StrictBool = False
    log_level: StrictInt | None = 3
    allow_window_creation: StrictBool | None = None


class EconomicsReplacementEvent(BaseModel):
    model_config = _STRICT_CONFIG
    year: Annotated[StrictInt, Field(ge=1, le=50)]
    cost: StrictNum
    label: StrictStr | None = None


class EconomicsTouPeriod(BaseModel):
    model_config = _STRICT_CONFIG
    name: StrictStr
    hours: Annotated[list[StrictInt], Field(min_length=1, max_length=24)]
    import_rate: StrictNum | None = None
    export_rate: StrictNum | None = None
    months: list[StrictInt] | None = None
    day_type: Literal["all", "weekday", "weekend"] = "all"


class EconomicsDemandChargeConfig(BaseModel):
    model_config = _STRICT_CONFIG
    billing_mode: Literal["demand", "capacity", "compare"] = "demand"
    demand_rate_per_kw_month: StrictNum = 0.0
    capacity_rate_per_kva_month: StrictNum | None = None
    contract_capacity_kva: StrictNum | None = None
    power_factor: StrictNum = 0.95
    months: list[StrictInt] | None = None


class EconomicsSensitivityConfig(BaseModel):
    model_config = _STRICT_CONFIG
    enabled: StrictBool = True
    factors: list[Literal["tariff", "capex", "yield", "degradation"]] | None = None
    variation_pct: StrictNum = 15.0


class EconomicsTariffConfig(BaseModel):
    model_config = _STRICT_CONFIG
    mode: Literal["feed_in", "self_consumption_flat", "tou"] = "feed_in"
    feed_in_tariff: StrictNum = 0.0
    import_tariff: StrictNum = 0.0
    self_consumption_ratio: StrictNum | None = None
    tou_schedule: list[EconomicsTouPeriod] | None = None
    demand_charge: EconomicsDemandChargeConfig | None = None


class EconomicsVatConfig(BaseModel):
    """Optional region-neutral value-added tax (VAT) and fixed-asset input VAT credit rules."""
    model_config = _STRICT_CONFIG
    rate_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    tariff_includes_vat: StrictBool = True
    input_vat_credit: StrictNum | None = None
    deductible_capex_share: Annotated[StrictNum, Field(ge=0.0, le=1.0)] | None = None
    surtax_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    opex_input_vat_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0


class EconomicsCitConfig(BaseModel):
    """Optional region-neutral corporate income tax (CIT), depreciation, and tax-holiday rules."""
    model_config = _STRICT_CONFIG
    rate_pct: Annotated[StrictNum, Field(ge=0.0, le=60.0)] = 0.0
    exempt_years: Annotated[StrictInt, Field(ge=0, le=30)] = 0
    half_rate_years: Annotated[StrictInt, Field(ge=0, le=30)] = 0
    depreciation_years: Annotated[StrictInt, Field(ge=1, le=50)] | None = None
    residual_value_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    loss_carryforward_years: Annotated[StrictInt, Field(ge=0, le=50)] = 5


class EconomicsFinancingConfig(BaseModel):
    """Optional project-finance debt & equity structure for Equity IRR and DSCR."""
    model_config = _STRICT_CONFIG
    equity_ratio_pct: Annotated[StrictNum, Field(ge=1.0, le=100.0)] = 100.0
    loan_tenor_years: Annotated[StrictInt, Field(ge=1, le=50)] = 15
    loan_grace_years: Annotated[StrictInt, Field(ge=0, le=10)] = 0
    loan_interest_rate_pct: Annotated[StrictNum, Field(ge=0.0, le=30.0)] = 0.0
    repayment_method: Literal["equal_principal", "equal_payment"] = "equal_principal"
    equity_discount_rate_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] | None = None


class EconomicsOpexTier(BaseModel):
    """Optional year-band OPEX override or additive schedule (e.g. staged O&M or land lease)."""
    model_config = _STRICT_CONFIG
    start_year: Annotated[StrictInt, Field(ge=1, le=50)]
    end_year: Annotated[StrictInt, Field(ge=1, le=50)]
    annual_opex: StrictNum | None = None
    opex_per_kw_year: StrictNum | None = None
    opex_pct_capex: StrictNum | None = None
    extra_annual_opex: StrictNum | None = None
    escalate: StrictBool = False
    label: StrictStr | None = None


class EconomicsFinancialConfig(BaseModel):
    model_config = _STRICT_CONFIG
    currency: StrictStr = "CNY"
    project_life_years: Annotated[StrictInt, Field(ge=1, le=50)] = 25
    discount_rate_pct: StrictNum = 6.0
    first_year_degradation_pct: StrictNum = 1.0
    annual_degradation_pct: StrictNum = 0.4
    opex_escalation_pct: StrictNum = 2.0
    tariff_escalation_pct: StrictNum = 0.0
    capex_per_kw: StrictNum | None = None
    battery_capex_per_kwh: StrictNum | None = None
    fixed_capex: StrictNum = 0.0
    opex_per_kw_year: StrictNum = 0.0
    opex_pct_capex: StrictNum = 0.0
    salvage_value_pct: StrictNum = 0.0
    upfront_opex: StrictNum = 0.0
    opex_tiers: list[EconomicsOpexTier] | None = None
    vat: EconomicsVatConfig | None = None
    cit: EconomicsCitConfig | None = None
    financing: EconomicsFinancingConfig | None = None
    battery_replacement_year: Annotated[StrictInt, Field(ge=1, le=50)] | None = None
    battery_replacement_cost_ratio: StrictNum | None = None
    battery_replacement_cost: StrictNum | None = None
    replacement_events: list[EconomicsReplacementEvent] | None = None


class EconomicsScenarioInput(BaseModel):
    model_config = _STRICT_CONFIG
    label: StrictStr
    csv_name: StrictStr | None = None
    csv_names: Annotated[list[StrictStr], Field(min_length=1, max_length=20)] | None = None
    csv_weights: list[StrictNum] | None = None
    annual_yield_kwh: StrictNum | None = None
    installed_dc_kw: StrictNum | None = None
    battery_kwh: StrictNum | None = None
    capex_total: StrictNum | None = None
    annual_opex: StrictNum | None = None
    replacement_events: list[EconomicsReplacementEvent] | None = None


class ReportScenarioInput(BaseModel):
    model_config = _STRICT_CONFIG
    label: StrictStr
    csv_name: StrictStr | None = None
    csv_names: Annotated[list[StrictStr], Field(min_length=1, max_length=20)] | None = None
    csv_weights: list[StrictNum] | None = None
    project: StrictStr | None = None
    variant: StrictStr | None = None
    installed_dc_kw: StrictNum | None = None
    battery_kwh: StrictNum | None = None
    capex_total: StrictNum | None = None
    annual_opex: StrictNum | None = None
    replacement_events: list[EconomicsReplacementEvent] | None = None


class BessSizingParams(BaseModel):
    model_config = _STRICT_CONFIG
    soc_min: StrictNum = 0.10
    soc_max: StrictNum = 0.95
    initial_soc: StrictNum = 0.20
    round_trip_efficiency: StrictNum = 0.88
    allow_grid_charging: StrictBool = True
    cycles_per_day: Annotated[StrictInt, Field(ge=1, le=2)] = 2
    min_spread: StrictNum = 0.05
    peak_shaving_limit_kw: StrictNum | None = None


class BessVerifyConfig(BaseModel):
    model_config = _STRICT_CONFIG
    project: StrictStr
    source_variant: StrictStr
    battery_file_name: StrictStr
    battery_library: Literal["workspace", "builtin"] = "workspace"
    battery_unit_kwh: StrictNum
    series_count: Annotated[StrictInt, Field(ge=1, le=100)] = 1
    monthly_temperature_c: Annotated[list[StrictNum], Field(min_length=12, max_length=12)] | None = None
    cycle_sow: StrictNum = 0.8
    static_sow: StrictNum = 0.8
    load_manifest_name: StrictStr | None = None
    variant_prefix: StrictStr | None = None


class FeasibilityInlineComponent(BaseModel):
    model_config = _STRICT_CONFIG
    component_type: Literal["PAN", "OND", "BTR"]
    new_name: StrictStr
    manufacturer: StrictStr
    model: StrictStr
    params: dict[StrictStr, Any]


class FeasibilityBessSizingConfig(BaseModel):
    model_config = _STRICT_CONFIG
    bess_kwh_values: Annotated[list[StrictNum], Field(min_length=1, max_length=20)]
    pcs_kw_values: Annotated[list[StrictNum], Field(min_length=1, max_length=15)]
    strategies: list[Literal["self_consumption", "tou_arbitrage", "peak_shaving"]] | None = None
    load_profile: ConstantNativeLoad | DailyNativeLoad | None = None
    bess_params: BessSizingParams | None = None
    pcs_capex_per_kw: StrictNum = 0.0
    rank_by: Literal["npv", "irr", "lcoe", "simple_payback_years", "discounted_payback_years"] = "npv"
    verify_top_k: Annotated[StrictInt, Field(ge=0, le=5)] = 0
    verify_config: BessVerifyConfig | None = None


class FeasibilityReportConfig(BaseModel):
    model_config = _STRICT_CONFIG
    title: StrictStr | None = None
    output_name: StrictStr | None = None
    language: Literal["zh", "en"] = "zh"
    plotlyjs: Literal["cdn", "embed"] = "cdn"
    overwrite: StrictBool = False


class FeasibilityStudyRequest(BaseModel):
    """One-shot end-to-end PV & PV+BESS feasibility study request."""
    model_config = _STRICT_CONFIG
    study_name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    design: SingleDesignRequest | None = None
    existing_csv: StrictStr | None = None
    existing_project: StrictStr | None = None
    existing_variant: StrictStr | None = None
    installed_dc_kw: StrictNum | None = None
    synthesize_components: list[FeasibilityInlineComponent] | None = None
    auto_tou_schedule: StrictBool = False
    bess_sizing: FeasibilityBessSizingConfig | None = None
    financial: EconomicsFinancialConfig | None = None
    tariff: EconomicsTariffConfig | None = None
    sensitivity: EconomicsSensitivityConfig | None = None
    report: FeasibilityReportConfig | None = None
    thresholds: dict[StrictStr, StrictNum] | None = None
