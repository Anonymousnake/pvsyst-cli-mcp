"""Strict wire request models; no native execution."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from pvsyst_auto.api.schemas_economics import (
    BessSizingParams,
    BessVerifyConfig,
    EconomicsCitConfig,
    EconomicsDemandChargeConfig,
    EconomicsFinancialConfig,
    EconomicsFinancingConfig,
    EconomicsOpexTier,
    EconomicsReplacementEvent,
    EconomicsScenarioInput,
    EconomicsSensitivityConfig,
    EconomicsTariffConfig,
    EconomicsTouPeriod,
    EconomicsVatConfig,
    FeasibilityBessSizingConfig,
    FeasibilityInlineComponent,
    P50P90Spec,
    ReportScenarioInput,
)
from pvsyst_auto.api.schemas_scene import (
    _STRICT_CONFIG,
    _GridStorageScheduleBase,
    _StrictModel,
    BatteryPackDesign,
    BifacialUpdates,
    BuildingObstacle,
    ConstantNativeLoad,
    DailyNativeLoad,
    ElectricalShadingSpec,
    FlatRoof,
    GableRoof,
    GridAvailabilityProfile,
    GridStorageDesign,
    GridStorageUpdates,
    HourlyNativeLoad,
    HouseholdApplianceSpec,
    HouseholdNativeLoad,
    HouseMesh,
    HouseTaskRequest,
    MeshHouseSpec,
    MeshObstacle,
    ModuleCellBinding,
    ModuleGeometry,
    MonthlyNativeLoad,
    NamedPVTableLayout,
    NamedPVTablesLayout,
    NativeLoadRequest,
    ObstacleSpec,
    PlacedPVTableLayout,
    ProbabilityNativeLoad,
    RoofSpec,
    StrictNum,
    StringLayoutBinding,
    SynthesisOrientationSpec,
    SynthesisSubArraySpec,
    TableStringBinding,
    TrackingControls,
    TreeObstacle,
    WallObstacle,
)


class CapacityRequest(_StrictModel):
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
    allow_asymmetric_mppt: StrictBool | None = None
class AgeingLossSpec(_StrictModel):
    """Zero-template PVsyst ageing configuration (pvAgeing) applied during synthesis or simulation."""
    enabled: StrictBool = True
    simulation_year: Annotated[StrictInt, Field(ge=1, le=50)] = 1
    annual_module_degradation_percent: StrictNum = 0.4
    imp_degrad_sharing_percent: StrictNum = 80.0
    keep_mc_values: StrictBool = False
    modules_rms_year_isc_percent: StrictNum = 0.0
    modules_rms_year_voc_percent: StrictNum = 0.0
class LossRequest(_StrictModel):
    """Optional loss overrides; LIDLoss, PMismMPP, ModuleQualityLoss, UFactor, and ageing."""
    LIDLoss: StrictNum | None = None
    PMismMPP: StrictNum | None = None
    ModuleQualityLoss: StrictNum | None = None
    UFactor: StrictNum | None = None
    ageing: AgeingLossSpec | None = None
class DeclaredValue(_StrictModel):
    """One design value, optionally with its declared unit and manufacturer source.

    A bare number is also accepted, but module and MPPT current limits without a source
    are reported as issues by the design backend instead of being assumed.
    """
    value: StrictNum | None = None
    unit: StrictStr | None = None
    source: StrictStr | None = None
class CoefficientDeclaration(_StrictModel):
    """A temperature coefficient that always states its unit and its source.

    The unit of a serialized component coefficient is never assumed, and both fields are
    therefore required.
    """
    value: StrictNum
    unit: StrictStr
    source: StrictStr
class EquipmentRequest(_StrictModel):
    """The exact loose component identity, or one explicit workspace filename.

    Every field is optional, and an omitted field stays null: the design backend decides
    whether one exact manufacturer+model pair or a filename was supplied, so an empty
    string is never mistaken for a filled-in identity.
    """
    file_name: StrictStr | None = None
    manufacturer: StrictStr | None = None
    model: StrictStr | None = None
class WeatherRequest(_StrictModel):
    """Where the simulated weather comes from; no mode is picked for the caller.

    mode='synthetic_from_site' runs PVsystCLI with --synthetic-weather directly from a
    workspace .SIT file without requiring a pre-built hourly .MET file.
    Text fields default to null so a missing SIT/MET/CSV name, country code or place
    label is reported by the backend as an unsupplied input instead of an empty value.
    """
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
    fetch_horizon: StrictBool = False
class HorizonRequest(_StrictModel):
    """Opt-in far-horizon specification (explicit points, PVGIS v5.3 fetch, or none)."""
    mode: Literal["none", "points", "pvgis"] = "points"
    points: list[list[StrictNum]] | None = None
    profile_name: StrictStr | None = None
    latitude: StrictNum | None = None
    longitude: StrictNum | None = None
    timeout_seconds: StrictNum | None = None

    @model_validator(mode="after")
    def _validate_horizon(self):
        from pvsyst_auto.weather.horizon import normalize_horizon_spec
        normalize_horizon_spec(self.model_dump(exclude_none=True))
        return self
class TemperatureRequest(_StrictModel):
    """Design temperatures in degC; min_ambient_basis names its source or method."""
    min_ambient_c: StrictNum | None = None
    min_ambient_basis: StrictStr | None = None
    min_module_operating_c: StrictNum | None = None
    max_module_operating_c: StrictNum | None = None
    annual_min_ambient_c: StrictNum | None = None
class EnvelopeRequest(_StrictModel):
    """Direct manufacturer voltage values, each optionally carrying unit and source."""
    cold_voc_v: StrictNum | DeclaredValue | None = None
    cold_vmp_v: StrictNum | DeclaredValue | None = None
    hot_vmp_v: StrictNum | DeclaredValue | None = None
class CoefficientsRequest(_StrictModel):
    """Declared temperature coefficients, each with an explicit unit and source."""
    beta_voc: CoefficientDeclaration | None = None
    beta_vmp: CoefficientDeclaration | None = None
    alpha_isc: CoefficientDeclaration | None = None
    alpha_imp: CoefficientDeclaration | None = None
class CurrentLimitRequest(_StrictModel):
    """Declared manufacturer current limits, as a value or a declaring object."""
    max_operating_current_a: StrictNum | DeclaredValue | None = None
    max_short_circuit_current_a: StrictNum | DeclaredValue | None = None
class EffectiveIrradianceRequest(_StrictModel):
    """The maximum effective irradiance and rear gain used by a coefficient envelope."""
    max_irradiance_w_m2: StrictNum | None = None
    rear_gain_factor: StrictNum | None = None
    source: StrictStr | None = None
class DesignEnvelopeRequest(_StrictModel):
    """The bounded design-envelope inputs; unknown keys are rejected, not ignored.

    Every value is either a strict number or a declared {value, unit, source} object.
    Omitted fields stay empty and are returned as concrete issues by the backend, so an
    empty object never becomes a ready design.
    """
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
class TerrainSpec(_StrictModel):
    """3D terrain DEM specification (inline XYZ points, workspace CSV, or optional GeoTIFF .tif)."""
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
class GroundSubOrientationSpec(_StrictModel):
    """One slope/aspect orientation in a multi-orientation unlimited ground_sheds plant."""
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
class GroundLayoutRequest(_StrictModel):
    """Ground multi-row fixed sheds or horizontal N-S single-axis tracker geometry."""
    model_mode: Literal["unlimited", "3d"] | None = None
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
    exclusion_zones: Annotated[
        list[Annotated[list[list[StrictNum]], Field(min_length=3, max_length=64)]],
        Field(max_length=128),
    ] | None = None
    setback_m: StrictNum | None = None
    ground_clearance_m: StrictNum | None = None
    max_table_columns: Annotated[StrictInt, Field(ge=1, le=500)] | None = None
    obstacles: Annotated[list[ObstacleSpec], Field(min_length=1, max_length=64)] | None = None
    base_slope_deg: StrictNum | None = None
    terrain: TerrainSpec | None = None
    sub_orientations: Annotated[list[GroundSubOrientationSpec], Field(min_length=1, max_length=16)] | None = None
    top_spacing_m: StrictNum | None = None
    dome_wiring: Literal["auto", "dual_mppt", "mixed"] | None = None
    pvc_name: StrictStr | None = None
    pvc_subset: Literal["fixed", "tracker"] | None = None
    pvc_orientation_tolerance_deg: StrictNum | None = None
    dxf_name: StrictStr | None = None
    dxf_layers: dict[StrictStr, StrictStr | list[StrictStr]] | None = None
    dxf_unit_scale: StrictNum | None = None
    electrical_shading: Literal["linear", "partition"] | ElectricalShadingSpec | None = None
class AutoSizingOptions(_StrictModel):
    """Optional bounds used when sizing_mode='auto_from_components' derives capacity from PAN/OND."""
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
    allow_asymmetric_mppt: StrictBool | None = None

class TransformerCableSpec(_StrictModel):
    """MV or HV outgoing collection/transmission cable specification."""
    length_m: Annotated[StrictNum, Field(ge=0.0, le=100000.0)] = 0.0
    metal: Literal["Copper", "Aluminium"] = "Copper"
    section_mm2: StrictInt = 150
class MvTransformerSpec(_StrictModel):
    """Medium-voltage transformer stage specification (single or parallel units).

    In fractional mode (default), PVsystCLI 8.1.7 scales iron_loss_frac and copper_loss_frac
    against inverter nominal AC power (PNomInvAC), not stc_ref_power_kw. Provide
    nominal_power_kva, iron_loss_kw, and copper_loss_kw together (per transformer) for
    explicit datasheet losses (validated against PVsyst's 80%-200% PNomInvAC rating bounds,
    strictly positive datasheet values, and <=1% iron / <=5% copper limits).
    """
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
class HvTransformerSpec(_StrictModel):
    """High-voltage substation transformer stage nested above MV transformers.

    PVsystCLI 8.1.7 always simulates HV transformers from datasheet fields (HVTrfDS*);
    iron_loss_frac / copper_loss_frac are converted to equivalent nameplate kW values.
    Validated against PVsyst's 80%-200% PNomInvAC rating bounds, strictly positive losses,
    <=1% iron / <=5% copper of nominal kVA, and hv.output_voltage_kv >= mv.output_voltage_kv.
    """
    output_voltage_kv: Annotated[StrictNum, Field(ge=30.0, le=500.0)] = 110.0
    stc_ref_power_kw: StrictNum | None = None
    nominal_power_kva: StrictNum | None = None
    iron_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.05)] | None = None
    copper_loss_frac: Annotated[StrictNum, Field(ge=0.0, le=0.15)] | None = None
    iron_loss_kw: StrictNum | None = None
    copper_loss_kw: StrictNum | None = None
    night_disconnect: StrictBool = False
    cable: TransformerCableSpec | None = None
class TransformersConfig(_StrictModel):
    """Optional MV or MV+HV cascaded transformer configuration for grid designs."""
    mv: MvTransformerSpec
    hv: HvTransformerSpec | None = None
class StandaloneGensetSpec(_StrictModel):
    """Inline backup generator specification (synthesizes a .GEN file in ComposPV/Gensets)."""
    file_name: StrictStr | None = None
    model: StrictStr | None = None
    manufacturer: StrictStr = "Generic"
    fuel_type: Literal["Diesel", "Gas", "Essence", "Biogas", "Other"] = "Diesel"
    nominal_power_kw: Annotated[StrictNum, Field(gt=0, le=100000)]
    fuel_consumption_l_per_h: Annotated[StrictNum, Field(gt=0, le=50000)]
class StandaloneEconomicsSpec(_StrictModel):
    """Optional off-grid economics parameters including diesel fuel and genset maintenance OPEX."""
    capex_total: Annotated[StrictNum, Field(ge=0)]
    annual_om_opex: Annotated[StrictNum, Field(ge=0)] = 0.0
    fuel_price_per_liter: Annotated[StrictNum, Field(ge=0)] = 1.50
    genset_opex_per_hour: Annotated[StrictNum, Field(ge=0)] = 0.0
    genset_fuel_rate_l_per_kwh: Annotated[StrictNum, Field(ge=0)] | None = None
    avoided_tariff_per_kwh: Annotated[StrictNum, Field(ge=0)] = 0.0
    financial: dict[str, Any] | None = None
class StandaloneDesignSpec(_StrictModel):
    """Off-grid Stand-Alone PV + BESS + Controller + optional Backup Genset specification (SystemType=Battery)."""
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
class SingleDesignRequest(_StrictModel):
    """One bounded design request; every nested object rejects unknown keys.

    scope selects fixed_unshaded_grid (default), ground_sheds (unlimited or 3D multi-row sheds),
    ground_tracker (unlimited or 3D horizontal N-S single-axis trackers), or stand_alone
    (off-grid SystemType=Battery PV+BESS+Controller+optional Backup Genset).
    sizing_mode='strict' (default) requires explicit capacity or electrical_design;
    sizing_mode='auto_from_components' estimates capacity from readable PAN/OND files and
    marks the output needs_review with full assumptions.
    """
    new_project: StrictStr = ""
    year: StrictInt | None = None
    target_dc_kw: StrictNum | None = None
    tilt: StrictNum | None = None
    azimuth: StrictNum | None = None
    scope: Literal["fixed_unshaded_grid", "ground_sheds", "ground_tracker", "ground_dome", "stand_alone"] = "fixed_unshaded_grid"
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
    p50_p90: P50P90Spec | None = None
    horizon: HorizonRequest | None = None
class SourceTextRequest(_StrictModel):
    """One caller-declared source; its text is checked here and never returned."""
    kind: Literal["manufacturer", "site", "assumption"]
    title: Annotated[StrictStr, Field(min_length=1)]
    locator: Annotated[StrictStr, Field(min_length=1)]
    text: Annotated[StrictStr, Field(min_length=1)]
    model: StrictStr | None = None
class SourceDeclarationRequest(_StrictModel):
    """One claimed value with the exact citation text and tokens it must agree with."""
    field: StrictStr
    value: StrictNum
    unit: StrictStr
    source_id: StrictStr
    excerpt: StrictStr
    source_value_text: StrictStr
    source_unit_text: StrictStr
    page: StrictInt | None = None
class SourcedDesignGoals(_StrictModel):
    """Explicit engineering goals only; no envelope, coefficient or limit is injected here."""
    target_dc_kw: StrictNum | None = None
    max_dc_ac_ratio: StrictNum | None = None
    capacity_tolerance_pct: StrictNum | None = None
    voltage_standard: Literal["IEC", "UL"] | None = None
    design_basis_extra: StrictStr | None = None
class SourcedHouseTaskRequest(_StrictModel):
    """Cited source inputs plus house geometry, layout, and optional prepare_mode."""
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
                raise ValueError("source_project/source_variant require prepare_mode='preserve_source_hardware'; a legacy request never silently ignores them")
            return self
        if not (self.source_project or "").strip() or not (self.source_variant or "").strip():
            raise ValueError("preserve_source_hardware requires non-empty source_project and source_variant")
        extra = set(self.design.model_fields_set) - {"year", "report_pages"}
        if extra:
            raise ValueError(f"preserve_source_hardware keeps the source hardware verbatim and accepts only design.year and design.report_pages; remove {sorted(extra)}")
        if self.design.year is None:
            raise ValueError("preserve_source_hardware requires an explicit design.year in 1969..2068")
        if self.sources or self.declarations or self.goals is not None:
            raise ValueError("preserve_source_hardware reuses the existing source; sources/declarations/goals are a source rewrite and are rejected before any native query or write")
        return self
_PROJECTS_MODELS = (
    "ACCablingUpdates", "AgeingUpdates", "AuxiliaryUpdates", "DCCablingUpdates",
    "GeneratorUpdates", "GridLimitUpdates", "HorizonUpdates", "HouseSpec",
    "IamUpdates", "InverterTemperatureUpdates", "NativeEconomicsUpdates",
    "OrientationConvertParams", "P50P90Updates", "PVTableLayout",
    "PowerControlLimitUpdates", "PowerControlsUpdates", "PowerFactorUpdates",
    "SeasonalUpdates", "ShedGeometryUpdates", "SoilingUpdates",
    "SpectralUpdates", "StringMismatchUpdates", "SystemUnavailabilityUpdates",
    "UnavailabilityPeriod",
)
_SIMULATION_MODELS = (
    "BatchSweepRequest", "EngineeringBlock", "EngineeringChanges",
    "EngineeringRevisionRequest", "EngineeringScenario",
    "FeasibilityReportConfig", "FeasibilityStudyRequest",
)
__all__ = sorted(
    {"StrictNum"}
    | {k for k, v in list(globals().items()) if not k.startswith("_") and isinstance(v, type) and issubclass(v, _StrictModel)}
    | set(_PROJECTS_MODELS)
    | set(_SIMULATION_MODELS)
)
def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_PROJECTS_MODELS) | set(_SIMULATION_MODELS))
def __getattr__(name: str):
    if name in _PROJECTS_MODELS:
        from . import projects
        return getattr(projects, name)
    if name in _SIMULATION_MODELS:
        from . import simulation
        return getattr(simulation, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
