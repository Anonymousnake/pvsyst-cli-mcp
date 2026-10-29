"""Strict wire request models; no native execution."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr


StrictNum = StrictInt | StrictFloat

class GeneratorUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    filename: str | None = None
    operating_power_kw: StrictFloat | None = None
    thresholds: dict[str, dict[str, StrictFloat]] | None = None

class SoilingUpdates(BaseModel):
    """Explicit native soiling mode and GUI-precision percentage values."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant", "monthly"]
    yearly_loss_pct: StrictNum | None = None
    monthly_loss_pct: list[StrictNum] | None = None

class SystemUnavailabilityUpdates(BaseModel):
    """Remove existing native PV-system outages without generating a schedule."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none"]

class GridLimitUpdates(BaseModel):
    """Native constant active-power limit at the grid injection point."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant"]
    limit_kw: StrictNum | None = None

class AuxiliaryUpdates(BaseModel):
    """Explicit native whole-system operating/night auxiliary powers in kW."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant", "profile"]
    operating_kw: StrictNum | None = None
    night_kw: StrictNum | None = None
    operating_threshold_kw: StrictNum | None = None
    proportional_w_per_kw: StrictNum | None = None
    proportional_threshold_kw: StrictNum | None = None

class SeasonalUpdates(BaseModel):
    """Explicit native summer/winter angles and winter-month selection."""
    model_config = ConfigDict(extra="forbid")
    summer_tilt: StrictNum
    winter_tilt: StrictNum
    winter_months: list[StrictInt]
    orientation_name: StrictStr | None = None

class HorizonUpdates(BaseModel):
    """Native far-horizon points in PVsyst azimuth/elevation degrees."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "profile"]
    points: list[list[StrictNum]] | None = None
    profile_name: StrictStr | None = None

class InverterTemperatureUpdates(BaseModel):
    """Explicit native temperature mode and both saved temperature values in degC."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["ambient", "offset", "fixed"]
    offset_c: StrictNum | None = None
    fixed_c: StrictNum | None = None

class AgeingUpdates(BaseModel):
    """Existing activated native template; inputs and recorded RMS/retention controls only."""
    model_config = ConfigDict(extra="forbid")
    simulation_year: StrictInt
    annual_module_degradation_percent: StrictNum
    keep_mc_values: StrictBool | None = None
    modules_rms_year_isc_percent: StrictNum | None = None
    modules_rms_year_voc_percent: StrictNum | None = None

class IamUpdates(BaseModel):
    """Component IAM bit or the ASHRAE model; PAN curve data is never written."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["component", "ashrae"]
    ashrae_b0: StrictNum | None = None

class StringMismatchUpdates(BaseModel):
    """Native string-voltage-mismatch default bit or one explicit manual percentage."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["default", "constant"]
    loss_percent: StrictNum | None = None

class PowerFactorUpdates(BaseModel):
    """Explicit native signed tangent values; no cosine or reactive-power calculation."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant", "monthly"]
    pnom_limit: Literal["native", "active", "apparent"]
    tan_phi: StrictNum | None = None
    monthly_tan_phi: list[StrictNum] | None = None

class PowerControlLimitUpdates(BaseModel):
    """Explicit native limit quantity, application point and loss classification."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant"]
    quantity: Literal["active", "apparent"]
    point: Literal["injection", "inverter"]
    limit: StrictNum | None = None
    separate_loss: StrictBool | None = None

class PowerControlsUpdates(BaseModel):
    """One native transaction; the standalone editors retain their existing domains."""
    model_config = ConfigDict(extra="forbid")
    power_factor: PowerFactorUpdates
    grid_limit: PowerControlLimitUpdates

class P50P90Updates(BaseModel):
    """Complete native P50 parameters for an existing GUI-produced result template."""
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    length_m: StrictNum
    metal: Literal["Copper", "Aluminium"] | None = None
    section: Literal["ISO60228_10", "ISO60228_16", "ISO60228_25", "ISO60228_35", "ISO60228_50",
                     "ISO60228_70", "ISO60228_95", "ISO60228_120"] | None = None

class NativeDCUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["resistance", "percent"]
    value: StrictNum

class ShedGeometryUpdates(BaseModel):
    """Explicit native Sheds/SunShields inputs; omitted values stay inherited."""
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    kind: Literal["flat"]

class GableRoof(BaseModel):
    """Symmetric gable roof with the ridge parallel to the house local X axis."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["gable"]
    pitch_deg: StrictNum

RoofSpec = Annotated[FlatRoof | GableRoof, Field(discriminator="kind")]

class HouseSpec(BaseModel):
    """Rectangular footprint with an explicit eave height; no other shapes are supported."""
    model_config = ConfigDict(extra="forbid")
    length_m: StrictNum
    width_m: StrictNum
    eave_height_m: StrictNum
    roof: RoofSpec

class PVTableLayout(BaseModel):
    """One fixed table; tilt and azimuth always come from the source variant."""
    model_config = ConfigDict(extra="forbid")
    module_rows: StrictInt
    module_columns: StrictInt
    orientation: Literal["portrait", "landscape"]
    row_spacing_m: StrictNum
    column_spacing_m: StrictNum
    clearance_m: StrictNum
    edge_margin_m: StrictNum

class HouseMesh(BaseModel):
    """AI-authored named architectural geometry; indices are explicit, zero-based triangles."""
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    vertices: list[Annotated[list[StrictNum], Field(min_length=3, max_length=3)]]
    triangles: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]

class MeshHouseSpec(BaseModel):
    """One house composed by the caller, not selected from a roof-template catalogue."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["mesh"]
    meshes: list[HouseMesh]

class PlacedPVTableLayout(BaseModel):
    """Explicit local placement over one named, planar, upward-facing roof surface mesh."""
    model_config = ConfigDict(extra="forbid")
    module_rows: StrictInt
    module_columns: StrictInt
    orientation: Literal["portrait", "landscape"]
    row_spacing_m: StrictNum
    column_spacing_m: StrictNum
    origin_m: Annotated[list[StrictNum], Field(min_length=3, max_length=3)]
    roof_mesh: StrictStr

class NamedPVTableLayout(PlacedPVTableLayout):
    """One named region; dual-source authoring explicitly selects its stored orientation ID."""
    name: StrictStr
    orientation_id: StrictInt | None = None

class NamedPVTablesLayout(BaseModel):
    """Named regions on one source plane, or one region per two explicit source planes."""
    model_config = ConfigDict(extra="forbid")
    tables: Annotated[list[NamedPVTableLayout], Field(min_length=1, max_length=64)]

class ModuleGeometry(BaseModel):
    """Explicit geometric metadata, checked against a readable loose PAN when present."""
    model_config = ConfigDict(extra="forbid")
    model_name: StrictStr
    nom_power_wp: StrictNum
    width_m: StrictNum
    length_m: StrictNum
    geometry_depth_m: StrictNum

class HouseTaskRequest(BaseModel):
    """Complete authored geometry plus one exact source; no inferred engineering inputs."""
    model_config = ConfigDict(extra="forbid")
    source_project: StrictStr
    source_variant: StrictStr
    new_project: StrictStr
    variant_name: StrictStr
    year: StrictInt
    house: MeshHouseSpec
    layout: NamedPVTablesLayout | PlacedPVTableLayout
    module_geometry: ModuleGeometry | None = None
    report_pages: list[StrictStr] | None = None
    shading_mode: Literal["linear", "module_layout"] | None = None
    scene_import_method: Literal["gui", "native"] | None = None

class AfterSunsetDischarge(BaseModel):
    """Native AfterSunset discharge mode; it carries no hour fields."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["after_sunset"]

class HourWindowDischarge(BaseModel):
    """Native MinMaxHour window with two distinct adapter-bounded hours."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["hour_window"]
    start_hour: StrictInt
    end_hour: StrictInt

PeakDischarge = Annotated[AfterSunsetDischarge | HourWindowDischarge, Field(discriminator="mode")]

class ConstantNativeLoad(BaseModel):
    """Native fixed constant consumption in kW; unknown fields stop at the boundary."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["constant"]
    constant_kw: StrictNum

class MonthlyNativeLoad(BaseModel):
    """Native monthly loads as twelve declared kWh values; never derived from monthly power."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["monthly"]
    monthly_energy_kwh: list[StrictNum] = Field(min_length=12, max_length=12)

class DailyNativeLoad(BaseModel):
    """Native daily profile as 24 hourly powers; the trailing slot is native format only."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["daily"]
    daily_power_kw: list[StrictNum] = Field(min_length=24, max_length=24)

NativeLoadRequest = Annotated[
    ConstantNativeLoad | MonthlyNativeLoad | DailyNativeLoad,
    Field(discriminator="mode"),
]

class GridStorageUpdates(BaseModel):
    """Native existing AC-coupled storage scalars; unknown fields stop at the boundary."""
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["self_consumption", "weak_grid"] | None = None
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
    model_config = ConfigDict(extra="forbid")
    csv_name: StrictStr
    year: StrictInt
    utc_offset_hours: StrictInt
    time_basis: Literal["local_standard_interval_start"]

class BatteryPackDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: StrictStr
    series_count: StrictInt
    parallel_count: StrictInt
    monthly_temperature_c: list[StrictNum]
    cycle_sow: StrictNum
    static_sow: StrictNum

class GridStorageDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["self_consumption", "weak_grid"]
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
    model_config = ConfigDict(extra="forbid")
    target_dc_kw: StrictInt | StrictFloat | None = None
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
    model_config = ConfigDict(extra="forbid")
    LIDLoss: StrictNum | None = None
    PMismMPP: StrictNum | None = None
    ModuleQualityLoss: StrictNum | None = None
    UFactor: StrictNum | None = None

class DeclaredValue(BaseModel):
    """One design value, optionally with its declared unit and manufacturer source.

    A bare number is also accepted, but module and MPPT current limits without a source
    are reported as issues by the design backend instead of being assumed.
    """
    model_config = ConfigDict(extra="forbid")
    value: StrictNum | None = None
    unit: StrictStr | None = None
    source: StrictStr | None = None

class CoefficientDeclaration(BaseModel):
    """A temperature coefficient that always states its unit and its source.

    The unit of a serialized component coefficient is never assumed, and both fields are
    therefore required.
    """
    model_config = ConfigDict(extra="forbid")
    value: StrictNum
    unit: StrictStr
    source: StrictStr

class EquipmentRequest(BaseModel):
    """The exact loose component identity, or one explicit workspace filename.

    Every field is optional, and an omitted field stays null: the design backend decides
    whether one exact manufacturer+model pair or a filename was supplied, so an empty
    string is never mistaken for a filled-in identity.
    """
    model_config = ConfigDict(extra="forbid")
    file_name: StrictStr | None = None
    manufacturer: StrictStr | None = None
    model: StrictStr | None = None

class WeatherRequest(BaseModel):
    """Where the simulated weather comes from; no mode is picked for the caller.

    Text fields default to null so a missing SIT/MET/CSV name, country code or place
    label is reported by the backend as an unsupplied input instead of an empty value.
    """
    model_config = ConfigDict(extra="forbid")
    mode: Literal["existing", "csv", "open_meteo"] = "existing"
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

class TemperatureRequest(BaseModel):
    """Design temperatures in degC; min_ambient_basis names its source or method."""
    model_config = ConfigDict(extra="forbid")
    min_ambient_c: StrictNum | None = None
    min_ambient_basis: StrictStr | None = None
    min_module_operating_c: StrictNum | None = None
    max_module_operating_c: StrictNum | None = None
    annual_min_ambient_c: StrictNum | None = None

class EnvelopeRequest(BaseModel):
    """Direct manufacturer voltage values, each optionally carrying unit and source."""
    model_config = ConfigDict(extra="forbid")
    cold_voc_v: StrictNum | DeclaredValue | None = None
    cold_vmp_v: StrictNum | DeclaredValue | None = None
    hot_vmp_v: StrictNum | DeclaredValue | None = None

class CoefficientsRequest(BaseModel):
    """Declared temperature coefficients, each with an explicit unit and source."""
    model_config = ConfigDict(extra="forbid")
    beta_voc: CoefficientDeclaration | None = None
    beta_vmp: CoefficientDeclaration | None = None
    alpha_isc: CoefficientDeclaration | None = None
    alpha_imp: CoefficientDeclaration | None = None

class CurrentLimitRequest(BaseModel):
    """Declared manufacturer current limits, as a value or a declaring object."""
    model_config = ConfigDict(extra="forbid")
    max_operating_current_a: StrictNum | DeclaredValue | None = None
    max_short_circuit_current_a: StrictNum | DeclaredValue | None = None

class EffectiveIrradianceRequest(BaseModel):
    """The maximum effective irradiance and rear gain used by a coefficient envelope."""
    model_config = ConfigDict(extra="forbid")
    max_irradiance_w_m2: StrictNum | None = None
    rear_gain_factor: StrictNum | None = None
    source: StrictStr | None = None

class DesignEnvelopeRequest(BaseModel):
    """The bounded design-envelope inputs; unknown keys are rejected, not ignored.

    Every value is either a strict number or a declared {value, unit, source} object.
    Omitted fields stay empty and are returned as concrete issues by the backend, so an
    empty object never becomes a ready design.
    """
    model_config = ConfigDict(extra="forbid")
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

class SingleDesignRequest(BaseModel):
    """One bounded design request; every nested object rejects unknown keys.

    scope is fixed to fixed_unshaded_grid and is never guessed from other inputs.
    """
    model_config = ConfigDict(extra="forbid")
    new_project: StrictStr = ""
    year: StrictInt | None = None
    target_dc_kw: StrictNum | None = None
    tilt: StrictNum | None = None
    azimuth: StrictNum | None = None
    scope: Literal["fixed_unshaded_grid"] = "fixed_unshaded_grid"
    module: EquipmentRequest = Field(default_factory=EquipmentRequest)
    inverter: EquipmentRequest = Field(default_factory=EquipmentRequest)
    template_project: StrictStr | None = None
    variant: StrictStr | None = None
    weather: WeatherRequest = Field(default_factory=WeatherRequest)
    capacity: CapacityRequest | None = None
    electrical_design: DesignEnvelopeRequest | None = None
    losses: LossRequest | None = None
    report_pages: list[StrictStr] | None = None

class SourceTextRequest(BaseModel):
    """One caller-declared source; its text is checked here and never returned."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["manufacturer", "site", "assumption"]
    title: Annotated[StrictStr, Field(min_length=1)]
    locator: Annotated[StrictStr, Field(min_length=1)]
    text: Annotated[StrictStr, Field(min_length=1)]
    model: StrictStr | None = None

class SourceDeclarationRequest(BaseModel):
    """One claimed value with the exact citation text and tokens it must agree with."""
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    target_dc_kw: StrictNum | None = None
    max_dc_ac_ratio: StrictNum | None = None
    capacity_tolerance_pct: StrictNum | None = None
    voltage_standard: Literal["IEC", "UL"] | None = None
    design_basis_extra: StrictStr | None = None

class SourcedHouseTaskRequest(BaseModel):
    """Cited source inputs plus the existing preparation and house schemas."""
    model_config = ConfigDict(extra="forbid")
    design: SingleDesignRequest
    new_project: StrictStr
    variant_name: StrictStr
    house: MeshHouseSpec
    layout: NamedPVTablesLayout | PlacedPVTableLayout
    sources: dict[str, SourceTextRequest] = Field(default_factory=dict)
    declarations: list[SourceDeclarationRequest] = Field(default_factory=list)
    goals: SourcedDesignGoals | None = None
    module_geometry: ModuleGeometry | None = None

class EngineeringChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tilt: StrictNum | None = None
    azimuth: StrictNum | None = None
    losses: LossRequest | None = None
    tracking: TrackingControls | None = None
    storage: GridStorageUpdates | None = None

class EngineeringRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
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
    model_config = ConfigDict(extra="forbid")
    id: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    source_subarray_id: Annotated[StrictInt, Field(ge=1, le=2147483647)]
    count: Annotated[StrictInt, Field(ge=1, le=100)]
