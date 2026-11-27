"""Strict wire request models for economics, reports, BESS sizing, and feasibility studies."""
from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from pvsyst_auto.api.schemas_scene import (
    ConstantNativeLoad,
    DailyNativeLoad,
    StrictNum,
    _StrictModel,
)
from pvsyst_auto.energy.p50_p90 import normalize_p50_spec


class P50P90Spec(_StrictModel):
    """Probabilistic P50/P90 uncertainty specification (snake_case or native PVsyst keys)."""
    enabled: StrictBool | None = None
    evaluation_years: Annotated[StrictInt, Field(ge=1, le=50)] | None = None
    meteo_kind: Literal["SpecYear", "TMY", "MonthAverage"] | None = None
    p90_custom: Annotated[StrictInt, Field(ge=51, le=98)] | None = None
    pxx_custom: Annotated[StrictInt, Field(ge=52, le=99)] | None = None
    pxx_report: StrictBool | None = None
    pxx_main_results: StrictBool | None = None
    plot_kind: Literal[1] | None = None
    year_dev_from_aver_pct: StrictNum | None = None
    meteo_annual_variab_pct: StrictNum | None = None
    pv_model_variab_pct: StrictNum | None = None
    inverter_variab_pct: StrictNum | None = None
    soiling_variab_pct: StrictNum | None = None
    degrad_variab_pct: StrictNum | None = None
    custom_variab_pct: StrictNum | None = None
    custom_var_label: StrictStr | None = None
    MeteoKind: Literal["SpecYear", "TMY", "MonthAverage"] | None = None
    P90Custom: Annotated[StrictInt, Field(ge=51, le=98)] | None = None
    PxxCustom: Annotated[StrictInt, Field(ge=52, le=99)] | None = None
    PxxReport: StrictBool | None = None
    PxxMainResults: StrictBool | None = None
    PlotKind: Literal[1] | None = None
    YearDevFromAver: StrictNum | None = None
    MeteoAnnualVariab: StrictNum | None = None
    PVModelVariab: StrictNum | None = None
    InverterVariab: StrictNum | None = None
    SoilingVariab: StrictNum | None = None
    DegradVariab: StrictNum | None = None
    CustomVariab: StrictNum | None = None
    CustomVarLabel: StrictStr | None = None

    @model_validator(mode="after")
    def _validate_p50_spec(self):
        normalize_p50_spec(self.model_dump(exclude_none=True))
        return self


class EconomicsReplacementEvent(_StrictModel):
    year: Annotated[StrictInt, Field(ge=1, le=50)]
    cost: StrictNum
    label: StrictStr | None = None
class EconomicsTouPeriod(_StrictModel):
    name: StrictStr
    hours: Annotated[list[StrictInt], Field(min_length=1, max_length=24)]
    import_rate: StrictNum | None = None
    export_rate: StrictNum | None = None
    months: list[StrictInt] | None = None
    day_type: Literal["all", "weekday", "weekend"] = "all"
class EconomicsDemandChargeConfig(_StrictModel):
    billing_mode: Literal["demand", "capacity", "compare"] = "demand"
    demand_rate_per_kw_month: StrictNum = 0.0
    capacity_rate_per_kva_month: StrictNum | None = None
    contract_capacity_kva: StrictNum | None = None
    power_factor: StrictNum = 0.95
    months: list[StrictInt] | None = None
class EconomicsSensitivityConfig(_StrictModel):
    enabled: StrictBool = True
    factors: list[Literal["tariff", "capex", "yield", "degradation"]] | None = None
    variation_pct: StrictNum = 15.0
class EconomicsTariffConfig(_StrictModel):
    mode: Literal["feed_in", "self_consumption_flat", "tou"] = "feed_in"
    feed_in_tariff: StrictNum = 0.0
    import_tariff: StrictNum = 0.0
    self_consumption_ratio: StrictNum | None = None
    tou_schedule: list[EconomicsTouPeriod] | None = None
    demand_charge: EconomicsDemandChargeConfig | None = None
class EconomicsVatConfig(_StrictModel):
    """Optional region-neutral value-added tax (VAT) and fixed-asset input VAT credit rules."""
    rate_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    tariff_includes_vat: StrictBool = True
    input_vat_credit: StrictNum | None = None
    deductible_capex_share: Annotated[StrictNum, Field(ge=0.0, le=1.0)] | None = None
    surtax_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    opex_input_vat_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
class EconomicsCitConfig(_StrictModel):
    """Optional region-neutral corporate income tax (CIT), depreciation, and tax-holiday rules."""
    rate_pct: Annotated[StrictNum, Field(ge=0.0, le=60.0)] = 0.0
    exempt_years: Annotated[StrictInt, Field(ge=0, le=30)] = 0
    half_rate_years: Annotated[StrictInt, Field(ge=0, le=30)] = 0
    depreciation_years: Annotated[StrictInt, Field(ge=1, le=50)] | None = None
    residual_value_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] = 0.0
    loss_carryforward_years: Annotated[StrictInt, Field(ge=0, le=50)] = 5
class EconomicsFinancingConfig(_StrictModel):
    """Optional project-finance debt & equity structure for Equity IRR and DSCR."""
    equity_ratio_pct: Annotated[StrictNum, Field(ge=1.0, le=100.0)] = 100.0
    loan_tenor_years: Annotated[StrictInt, Field(ge=1, le=50)] = 15
    loan_grace_years: Annotated[StrictInt, Field(ge=0, le=10)] = 0
    loan_interest_rate_pct: Annotated[StrictNum, Field(ge=0.0, le=30.0)] = 0.0
    repayment_method: Literal["equal_principal", "equal_payment"] = "equal_principal"
    equity_discount_rate_pct: Annotated[StrictNum, Field(ge=0.0, le=50.0)] | None = None
class EconomicsOpexTier(_StrictModel):
    """Optional year-band OPEX override or additive schedule (e.g. staged O&M or land lease)."""
    start_year: Annotated[StrictInt, Field(ge=1, le=50)]
    end_year: Annotated[StrictInt, Field(ge=1, le=50)]
    annual_opex: StrictNum | None = None
    opex_per_kw_year: StrictNum | None = None
    opex_pct_capex: StrictNum | None = None
    extra_annual_opex: StrictNum | None = None
    escalate: StrictBool = False
    label: StrictStr | None = None
class EconomicsFinancialConfig(_StrictModel):
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
    p50_p90: P50P90Spec | None = None
    uncertainty: P50P90Spec | None = None

    @model_validator(mode="after")
    def _validate_financial_p50(self):
        if self.p50_p90 is not None and self.uncertainty is not None:
            raise ValueError("Specify either p50_p90 or uncertainty, not both")
        return self
class EconomicsScenarioInput(_StrictModel):
    label: StrictStr
    csv_name: StrictStr | None = None
    csv_names: Annotated[list[StrictStr], Field(min_length=1, max_length=20)] | None = None
    csv_weights: list[StrictNum] | None = None
    annual_yield_kwh: StrictNum | None = None
    yearly_energy_kwh: dict[StrictStr | StrictInt, StrictNum] | None = None
    ageing_batch_csv: StrictStr | None = None
    installed_dc_kw: StrictNum | None = None
    battery_kwh: StrictNum | None = None
    capex_total: StrictNum | None = None
    annual_opex: StrictNum | None = None
    replacement_events: list[EconomicsReplacementEvent] | None = None
    p50_p90: P50P90Spec | None = None
    uncertainty: P50P90Spec | None = None

    @model_validator(mode="after")
    def _validate_scenario_p50(self):
        if self.p50_p90 is not None and self.uncertainty is not None:
            raise ValueError("Specify either p50_p90 or uncertainty, not both")
        return self
class ReportScenarioInput(_StrictModel):
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
    p50_p90: P50P90Spec | dict[StrictStr, Any] | None = None
    uncertainty: P50P90Spec | None = None

    @model_validator(mode="after")
    def _validate_report_scenario_p50(self):
        if self.p50_p90 is not None and self.uncertainty is not None:
            raise ValueError("Specify either p50_p90 or uncertainty, not both")
        if isinstance(self.p50_p90, dict) and not ("yields_kwh" in self.p50_p90 and "exceedance_curve" in self.p50_p90):
            normalize_p50_spec(self.p50_p90)
        return self
class BessSizingParams(_StrictModel):
    soc_min: StrictNum = 0.10
    soc_max: StrictNum = 0.95
    initial_soc: StrictNum = 0.20
    round_trip_efficiency: StrictNum = 0.88
    allow_grid_charging: StrictBool = True
    cycles_per_day: Annotated[StrictInt, Field(ge=1, le=2)] = 2
    min_spread: StrictNum = 0.05
    peak_shaving_limit_kw: StrictNum | None = None
class BessVerifyConfig(_StrictModel):
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
class FeasibilityInlineComponent(_StrictModel):
    component_type: Literal["PAN", "OND", "BTR"]
    new_name: StrictStr
    manufacturer: StrictStr
    model: StrictStr
    params: dict[StrictStr, Any]
class FeasibilityBessSizingConfig(_StrictModel):
    bess_kwh_values: Annotated[list[StrictNum], Field(min_length=1, max_length=20)]
    pcs_kw_values: Annotated[list[StrictNum], Field(min_length=1, max_length=15)]
    strategies: list[Literal["self_consumption", "tou_arbitrage", "peak_shaving"]] | None = None
    load_profile: ConstantNativeLoad | DailyNativeLoad | None = None
    bess_params: BessSizingParams | None = None
    pcs_capex_per_kw: StrictNum = 0.0
    rank_by: Literal["npv", "irr", "lcoe", "simple_payback_years", "discounted_payback_years"] = "npv"
    verify_top_k: Annotated[StrictInt, Field(ge=0, le=5)] = 0
    verify_config: BessVerifyConfig | None = None
