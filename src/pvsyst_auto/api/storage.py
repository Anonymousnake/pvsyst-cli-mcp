"""Loads & Storage MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Literal
from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.energy.profiles import prepare_load_profile
from pvsyst_auto.energy.load import create_load_variant
from pvsyst_auto.energy.storage import create_grid_storage_variant
from pvsyst_auto.energy.system import create_grid_storage_system
from pvsyst_auto.native.load import create_native_load_variant
from pvsyst_auto.energy.peak import create_peak_shaving_variant
from pvsyst_auto.api.schemas import BatteryPackDesign, GridAvailabilityProfile, GridStorageDesign, GridStorageUpdates, NativeLoadRequest, PeakDischarge
from pvsyst_auto.api.context import categorized_tool, cli, components


@categorized_tool()
def pvsyst_prepare_load_profile(csv_name: StrictStr, year: StrictInt, output_name: StrictStr,
                                 utc_offset_hours: StrictInt,
                                 time_basis: Literal["local_standard_interval_start"],
                                 date_format: Literal["iso", "pvsyst_dot", "pvsyst_slash"] = "iso",
                                 date_column: StrictStr = "Date", load_column: StrictStr = "P Load",
                                 dry_run: StrictBool = True) -> dict:
    """Inspect or prepare a NEW native hourly load CSV from workspace/UserData.
    Require exactly the declared date/load columns plus explicit W or kW units,
    all 8760/8784 increasing interval-start hours of the requested year and an
    explicit fixed whole-hour standard UTC offset (no DST). iso dates mean
    YYYY-MM-DD HH:MM; pvsyst_dot/slash mean DD.MM.YYYY/DD/MM/YYYY HH:MM.
    dry_run=true writes nothing. Execution preserves the source, writes an ASCII
    Date;P Load / ;kW CSV to UserHourlyParams and an import record to Results.
    Missing/duplicate/non-hourly/negative/nonfinite rows stop; no gap filling,
    resampling, yearly renormalization, net-meter interpretation or time shift.
    Existing names are preserved. Write failure retains disclosed partial outputs.
    ready/prepared concern this CSV only, not native consumption: bind the CSV
    to a variant and verify hourly output. Native calendar lookup ignores year;
    the importer explicitly checks the requested year's leap day and time basis.
    input-param-file is an advanced parameters DAT, not a load CSV channel."""
    return prepare_load_profile(cli(), csv_name=csv_name, year=year, output_name=output_name,
                                utc_offset_hours=utc_offset_hours, time_basis=time_basis,
                                date_format=date_format, date_column=date_column,
                                load_column=load_column, dry_run=dry_run)

@categorized_tool()
def pvsyst_create_load_variant(project: StrictStr, source_variant: StrictStr, new_variant: StrictStr,
                                load_manifest_name: StrictStr, dry_run: StrictBool = True) -> dict:
    """Preview or create a NEW variant with an explicit embedded hourly load snapshot.
    Bind one prepared v1 load manifest in Results to current raw UserData CSV and
    normalized UserHourlyParams CSV; check full year/time basis/units and paired
    powers, never open a historical absolute path from the record. Replace the
    owning pvLoad with Energy/MJ signed-base62 DDMM hours, including leap day;
    return encoding precision. No annual renormalization, filling or DST shift.
    The source VC, PRJ, CSVs and other system/strategy/site/topology fields remain
    unchanged. A later CSV edit requires a new binding; reading-progress logs
    do not prove native consumption. This sets loads, not battery dispatch or
    hybrid ports. dry_run=true writes nothing; execution creates a new VC only,
    never overwrites. Verify E_User in a native run of matching year/timezone
    before treating configuration as consumed. Legacy binary/ambiguous VCs stop;
    non-ASCII load paths add a disclosed UTF-8 BOM to the NEW VC."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_load_variant(store, project=project, source_variant=source_variant,
                               new_variant=new_variant, load_manifest_name=load_manifest_name,
                               dry_run=dry_run)

@categorized_tool()
def pvsyst_create_peak_shaving_variant(project: StrictStr, source_variant: StrictStr,
                                       new_variant: StrictStr, discharge: PeakDischarge,
                                       dry_run: StrictBool = True,
                                       variant_name: StrictStr | None = None,
                                       add_utf8_bom: StrictBool = False) -> dict:
    """Preview or create a NEW variant that switches the native PeakShaving discharge strategy.
    Supported subset is the already-active native 8.1.6 PeakShaving profile: root
    Flags=$00080000 with no pvLoad, pvSystem Flags=$17000040/Grid plus the pinned battery
    hardware and temperature profile, and one directly owned pvGridStorage record holding
    Strategy=PeakShaving and ChargingStrategy=PVToGridMin. Stored SOC, AC power limits and
    efficiencies use the shared storage semantic validator, so valid scalar edits remain
    editable here instead of requiring the original sample's numeric constants. Pinned
    hardware/system limits remain unchanged; no sizing or dispatch model is offered.
    mode is the explicit discriminator: after_sunset writes DischStrategy=AfterSunset and
    drops HourBegDisch/HourEndDisch; hour_window writes DischStrategy=MinMaxHour and exactly
    two bare-integer hour endpoints that differ. Hour values are adapter-bounded to 0..23
    and are not measured native GUI limits; the source record is checked against the same
    domain, so a saved NaN, fractional or unknown value stops instead of being overwritten.
    Only those three fields change: strategy, charging strategy, SOC, power limits,
    efficiencies, grid limit, root/system flags, battery hardware, geometry, loads and the
    orientation records stay at their verified values, and every other owning field set is
    compared exactly so unknown, nested or duplicated fields stop. A source with a load,
    P50/P90, saved horizon, explicit 3D scene or module layout is blocked.
    dry_run=true writes nothing; execution creates one NEW VC only and never edits the source
    PRJ/VC or its inputs, reusing existing store locking, path checks, read-back verification
    and no-overwrite semantics. variant_name/add_utf8_bom follow the existing display-name
    and BOM rules. native_consumption_verified stays false: hours are native hour slots
    only, endpoint inclusivity is not inferred, AfterSunset does not mean discharging
    solely in hours without irradiance, and creating this variant does not prove that this
    run consumes it. This tool never changes the grid power limitation or power factor;
    the pinned PLimGrid/PGridLim 200 belongs to this native profile only, and the profile's
    grid power limitation is apparent power in the native report, not an active-power edit."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_peak_shaving_variant(store, project=project, source_variant=source_variant,
                                       new_variant=new_variant, discharge=discharge.model_dump(),
                                       dry_run=dry_run, variant_name=variant_name,
                                       add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_native_load_variant(project: StrictStr, source_variant: StrictStr,
                                      new_variant: StrictStr, load: NativeLoadRequest,
                                      dry_run: StrictBool = True,
                                      variant_name: StrictStr | None = None,
                                      add_utf8_bom: StrictBool = False) -> dict:
    """Preview or create a NEW variant with one native flat load block (constant, monthly or daily).
    Supported subset is a native 8.1.6 PV-only single-subarray Grid variant with
    exactly one ordinary fixed plane, free horizon, no shading scene or module layout,
    no P50/P90, and a pvSystem declaring only Flags=$06000000 and SystemType=Grid;
    main array Flags=$01900400/Flags2=$082A and subarray Flags=$019E1002 stay unchanged.
    mode is the explicit discriminator: constant{constant_kw} writes ConstantLoad with
    Energy Units=kW; monthly{monthly_energy_kwh} writes the declared twelve kWh values
    as ValMonth plus the observed three trailing 0.000 slots; daily{daily_power_kw}
    writes 24 hourly powers and a trailing slot equal to their sum, which reproduces
    the observed native layout only and is not a load or dispatch model.
    Numbers must be strict finite nonnegative 0.001-step values; constant_kw must be
    positive and profiles need at least one positive entry. The 1e6 cap is an adapter
    bound, not a native or engineering limit. Booleans, strings, empty or wrongly
    sized arrays, NaN/Inf and unknown fields are rejected, never silently dropped.
    Only root pvVCalcul bit19, the directly owned pvLoad block and an optional root
    Comment are written; root Flags must already be 0 or 0x80000 and agree with the
    presence of an existing pvLoad. An existing constant/monthly/daily block is
    replaced in place before the unique SimulPlots=Start anchor; the owning pvLoad
    Comment is normalized to a fixed MCP label inside this declared subset.
    HourlyFile, Probability, HouseHold, nested, duplicated-owner or unknown-field
    blocks stop.
    dry_run=true writes nothing. Execution creates one NEW VC and never edits the
    source PRJ/VC, inputs, weather, topology or other loads; existing store locking,
    path checks, read-back verification and no-overwrite semantics apply.
    add_utf8_bom follows the existing explicit marker habit: false keeps the source
    marker and a non-ASCII variant_name then needs an existing BOM, true writes a
    UTF-8 BOM to the NEW VC and is also accepted without a name; bom_added reports it.
    native_consumption_verified stays false: verify native E_User and the PDF for the
    matching year/timezone before treating a saved block as consumed."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_native_load_variant(store, project=project, source_variant=source_variant,
                                      new_variant=new_variant, load=load.model_dump(),
                                      dry_run=dry_run, variant_name=variant_name,
                                      add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_grid_storage_variant(project: StrictStr, source_variant: StrictStr,
                                       new_variant: StrictStr, updates: GridStorageUpdates,
                                       dry_run: StrictBool = True,
                                       availability: GridAvailabilityProfile | None = None,
                                       allow_grid_export: StrictBool | None = None) -> dict:
    """Preview or create a NEW VC from an existing native AC-coupled Grid storage template.
    Supported variant declarations are 8.1.0/8.1.6 with one complete flat
    pvGridStorage, Grid pvSystem, declared active storage/battery bits, an existing
    BTR reference and positive pack counts.
    SOC fields are fractions 0..1; require min < max, initial and reserve inside
    those bounds. PMaxCharge limits the charger's AC input in kW, PMaxDisch the
    battery inverter's AC output; neither is a hybrid DC-port rating. Explicit
    max/Euro efficiencies are percent, with Euro <= max. Scalar-only edits also retain
    the source PeakShaving/PVToGridMin pair with MinMaxHour or AfterSunset unchanged;
    source and candidate both validate SOC/efficiency/window semantics. Explicit strategy
    still uses SelfConsumption or WeakGrid with PVAvailable/Immediate. Public getter
    exposes grid_storage for read/re-edit; absent storage is null. WeakGrid requires a fresh
    availability declaration: full-year UserData Date;Grid Available / ;ratio CSV,
    ISO interval-start hours, exact 0/1 values, fixed standard offset and at least
    one outage. Bind native pvUnavail/HourlyVal GrdUnav Factor/Aver day snapshots,
    match the load snapshot year and require the root self-consumption flag.
    reserve_soc is SOCDischSelf: WeakGrid preserves it with grid available and
    permits discharge toward SOCDischDecl during outages. It is unused as backup
    in SelfConsumption; never a standalone generator voltage threshold.
    Preview writes nothing. Execution creates a NEW VC, retains source PRJ/VC,
    battery filename/counts, PV/load/site/temperature/SOW/topology. Optional
    allow_grid_export toggles only native system bit27: false prevents grid
    injection, true allows it subject to existing limits, omitted preserves it.
    This is not a charge-from-grid switch and not a hybrid DC-port setting.
    This does not construct hybrid ports, add battery hardware, schedule grid
    charging, infer efficiencies or certify physical sizing. Verify native SOC
    and energy flows before treating saved fields as consumed."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_grid_storage_variant(store, project=project, source_variant=source_variant,
                                       new_variant=new_variant,
                                       updates=updates.model_dump(exclude_none=True), dry_run=dry_run,
                                       availability=availability.model_dump() if availability else None,
                                       allow_grid_export=allow_grid_export)

@categorized_tool()
def pvsyst_create_grid_storage_system(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, battery: BatteryPackDesign,
                                     storage: GridStorageDesign, load_manifest_name: StrictStr,
                                     dry_run: StrictBool = True,
                                     availability: GridAvailabilityProfile | None = None,
                                     allow_grid_export: StrictBool | None = None) -> dict:
    """Preview or create a NEW native AC-coupled storage VC from an existing PV-only Grid design.
    Require explicit workspace loose BTR, series/parallel counts, twelve monthly
    battery temperatures, cycle/static initial SOW, complete SOC fractions,
    charger AC-input/battery inverter AC-output kW limits and percent efficiencies.
    No hardware or climate defaults are inferred. Bind a prepared v1 hourly load
    record and both current source/normalized CSVs into an explicit MJ snapshot.
    Enable root self-consumption bit19 and existing native Grid/battery system bits;
    retain other flags and PV/component/site/weather/geometry fields. Optional
    allow_grid_export toggles only system no-injection bit27; omission preserves
    the source setting. This controls export, not grid charging or DC coupling.
    Creates only one new VC, never edits the source or replaces files. SelfConsumption and
    WeakGrid use PVAvailable/Immediate. WeakGrid requires a complete explicit
    availability CSV declaration matching the prepared load year and standard
    UTC offset, with at least one outage. Bind native pvUnavail/HourlyVal ratio
    snapshots; SOCDischSelf preserves reserve with grid available and allows
    discharge toward SOCDischDecl during outages. It is unused as a backup
    reserve in SelfConsumption. Backend is pvsyst_native_ac_coupled, not hybrid
    DC battery-port modelling, a new BMS model or a grid-charge tariff controller.
    Creation does not simulate or certify sizing; verify native E_User, SOC and
    energy in the matching year/timezone before treating configuration as consumed.
    Non-ASCII new load/component paths add a disclosed BOM to the NEW VC only."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_grid_storage_system(store, project=project, source_variant=source_variant,
                                      new_variant=new_variant, battery=battery.model_dump(),
                                      storage=storage.model_dump(), load_manifest_name=load_manifest_name,
                                      dry_run=dry_run,
                                      availability=availability.model_dump() if availability else None,
                                      allow_grid_export=allow_grid_export)
