"""Simulation MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.native.cli import VAR_GROUPS, build_sfi
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import simulate_design
from pvsyst_auto.workflows.simulation import simulate_template
from pvsyst_auto.workflows.sourced_design import SCOPE as SOURCED_DESIGN_SCOPE, prepare_sourced_design
from pvsyst_auto.api.schemas import CapacityRequest, HouseTaskRequest, LossRequest, SingleDesignRequest, SourceDeclarationRequest, SourceTextRequest, SourcedDesignGoals, SourcedHouseTaskRequest, StrictNum, TrackingControls
from pvsyst_auto.api.context import categorized_tool, cli, components, optional_path, simulation_csv_path, workspace_path
from pvsyst_auto.components.identity import sourced_component


@categorized_tool()
def pvsyst_build_sfi(filename: str, variables: str = "all_common") -> dict:
    """Create a NEW hourly export definition in workspace/Models.
    variables=grid_storage includes common PV outputs plus native load, SOC,
    battery/grid flows, EUnused and all three converter losses CL_Chrg, CL_InvB,
    CL_Over. Include CL_Over when reviewing native grid-storage energy balance;
    this exports PVsyst values, not a separate storage calculation.
    variables=soiling adds native GlobSlg/SlgLoss irradiance outputs to common PV
    outputs; native runs may omit these columns when soiling is disabled.
    variables=grid_limit adds PGridLim, EGrdLim, EUnused and EOutInv. PGridLim is
    the native limit, EGrdLim the grid-curtailment loss, not EUnused. EOutInv is
    not the bounded injection column. Disabled runs may omit limit columns.
    variables=shading adds GlobShd/ShdElec; native linear-mode runs may omit ShdElec,
    and a defined detailed layout can legitimately have zero electrical loss."""
    target = workspace_path("Models", filename, ".sfi")
    names = (VAR_GROUPS[variables] if variables in VAR_GROUPS
             else [part.strip() for part in variables.split(",")])
    build_sfi(target, list(names))
    return {"sfi": str(target), "variables": list(names)}

@categorized_tool()
def pvsyst_run_house_task(spec: HouseTaskRequest, dry_run: StrictBool = True,
                            confirm: StrictBool = False, resume: StrictBool = False,
                            retry_simulation: StrictBool = False) -> dict:
    """Deliver one AI-authored residential project through existing native entries.
    Source must be the existing bounded fixed PV-only northern-site template,
    with identical bound workspace SIT/MET in PRJ/VC. Hardware/counts/angles/losses,
    source engineering defaults and weather remain inherited and disclosed, never sized
    or guessed. AI supplies a complete mesh house and one/two placed PV regions in spec;
    all module counts/source-PAN metadata and native8.1.6CN/DPI96 boundaries remain.
    Two source orientations require one explicitly named region per orientation1/2,
    the same PAN/OND, anchor azimuth zero and distinct source planes. Native table
    properties assign each plane and place its declared coordinates; SHD exports,
    saved VC and actual GUI reload verify every mesh/region before the annual run.
    Optional spec.shading_mode=module_layout invokes the native detailed electrical
    layout path for two landscape zero-spacing, single-string groups and exports
    ShdElec. Omitting it preserves the original request/checkpoint representation.
    Optional spec.scene_import_method="native" routes the SAME supported two-fixed-table
    recipe through the existing headless native importer (no GUI/INI/clipboard) and records
    the new variant as STORED-only: stored_geometry_verified (and stored_module_layout_verified
    for module_layout) may be true, while imported/native_geometry_verified/native_reload_verified
    and native_consumption_verified stay false until the normal CLI simulation consumes that VC.
    Omitting it keeps the original GUI import route and request/checkpoint shape; unsupported
    recipes stop with a concrete issue before writing and there is no silent GUI fallback.
    Preview writes nothing/does not activate or launch GUI; cold execution requires
    dry_run=false and confirm=true, configured DataPath and NO preexisting native GUI.
    It exclusively creates a working PRJ/VC0, PVC2 and SFI, opens one owned GUI, imports/
    associates/checks/shading-tables/SavesAs one NEW native variant, then normally closes
    only its actual Popen. Unknown dialogs/unsaved-work questions are not dismissed.
    A NEW CLI process reloads the saved variant, recomputes native shading and generates
    the explicit full-year CSV/PDF. Completeness/context/finite columns/GlobShd and source
    preservation are checked using existing result validators; no replacement PV model.
    File creation, native save and simulation-consumption remain distinct checkpoints.
    Results/<new-project-stem>_house_task.json records the exact request and guarded files;
    existing artifacts are never overwritten/deleted. Resume requires the IDENTICAL spec
    and explicit resume=true; no checkpoint PID grants ownership. Partial unverified native
    saves need inspection, not blind reimport. A failed run keeps its outputs: explicit
    retry_simulation=true chooses NEW attempt names, without rebuilding the saved scene.
    A completed retry-free resume revalidates artifacts and runs no new GUI/simulation.
    Status/phase/issues matter, not just MCP is_error; any unexpected stop retains evidence.
    Process coordination is one server/workspace RLock, not a global desktop/task service."""
    from pvsyst_auto.workflows.house import run_house_task
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return run_house_task(store, component_store, cli(), request=spec.model_dump(exclude_none=True),
        dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation)

@categorized_tool()
def pvsyst_simulate_template(template_project: StrictStr = "", variant: StrictStr = "",
                             new_project: StrictStr = "", pan_name: StrictStr = "",
                             ond_name: StrictStr = "", site_name: StrictStr = "",
                             met_name: StrictStr = "", tilt: StrictNum | None = None,
                             azimuth: StrictNum | None = None,
                             year: StrictInt | None = None, dry_run: StrictBool = True,
                             capacity: CapacityRequest | None = None,
                             weather_manifest_name: StrictStr = "",
                             losses: LossRequest | None = None,
                             report_pages: list[StrictStr] | None = None,
                             tracking: TrackingControls | None = None,
                             tracking_variant: StrictStr = "",
                             nominal_dc_power_kw: StrictNum | None = None,
                             tracking_power_source: StrictStr = "") -> dict:
    """Preflight or execute one template-to-full-year grid simulation without GUI.
    Supply an existing valid PRJ/VC, loose text PAN/OND, workspace SIT/MET, tilt,
    year and a NEW project filename. Missing inputs return field-specific issues.
    Native MET root date declarations are checked before creating the working project;
    generic-1990, absent or unreadable dates remain disclosed, not inferred from filenames.
    Scope: completely checked single subarray/fixed plane, no active shading/layout,
    PRCorrTemp=False; retain template counts unless capacity is explicitly supplied.
    capacity plans integer series/strings/inverters against declared design voltages,
    currents, temperatures, IEC/UL module voltage rating and maximum DC:AC ratio.
    Require independent unshared 8.1.0/8.1.6, one repeatable inverter/MPPT/string branch,
    full hardware MPPT population and crystalline PAN at STC; mode/topology stays intact.
    Design envelope/source are required, not guessed from raw temperature coefficients.
    No native temperature-model reproduction, hardware selection or design certification.
    Optional weather_manifest_name binds prepare_weather provenance to the requested
    year/SIT/MET, explicitly loads native weather, and compares hourly GHI/DHI/Tamb;
    changed files stop before writes. Conversion/rounding concerns remain needs_review.
    dry_run=true (default) writes nothing and consumes no simulation execution.
    dry_run=false copies/configures the project, runs Jan 1 through Dec 31 hourly,
    validates native context/complete year, and returns energy, standard PR, IL_Pmax,
    warnings, CSV/PDF/SFI and a persisted JSON manifest with reverse recovery steps.
    Existing project/output files are never overwritten. Failure retains the original
    template and working files/backups/manifest; no automatic retry or destructive cleanup.
    status is blocked/ready/completed/needs_review/failed; inspect it, not just MCP is_error.
    Extreme IL_Pmax versus STC reference energy triggers a disclosed review heuristic,
    not a claim of native physical validation or an automatic model repair.
    Year is 1969..2068 due to two-digit native CSV dates. Restart host for this tool.
    azimuth overrides the template plane azimuth in -180..180 degrees (zero south, west
    positive); losses accepts only LIDLoss, PMismMPP and ModuleQualityLoss on the
    subarray plus UFactor on the whole main array. Template fields left unspecified keep
    their saved values, and the returned configuration_summary discloses the requested
    angles and losses; no loss is fitted, inferred or defaulted.
    Optional report_pages is a nonempty list of native page names (for example
    ["summary", "losses"]); omit it for the default complete report. Invalid pages
    stop in preflight, and the request/manifest records the selection.
    Explicit tracking + tracking_variant selects an existing verified native8.1.6
    UnlimTracking template flow instead: omit tilt/azimuth/capacity/losses; exact source
    PAN/OND database references and equipment counts stay inherited. This branch requires
    explicit positive nominal_dc_power_kw and printable tracking_power_source; these are
    declarations cross-checked against native PR, not guessed from names/cached sizing.
    Loose workspace/builtin model shadows stop; database internals are not inspected or
    snapshotted. Only CLI8.1.6rev49278, and completed consumption retains needs_review.
    Reuses create_tracking_variant to
    create a NEW VC in the isolated clone, then the same yearly native load/report/
    context/integrity checks plus finite actual columns and GlobShd. No type conversion,
    arbitrary geometry or other tracker families. native_consumption_verified becomes
    true only after validated completion, not engineering certification. Its recovery is
    review/manual archive of the entire new clone/VC/outputs; no stale per-file undo,
    automatic retry/resume, deletion or repeat on an existing project/output."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return simulate_template(current, store, template_project=template_project, variant=variant,
        new_project=new_project, pan_name=pan_name, ond_name=ond_name, site_name=site_name,
        met_name=met_name, tilt=tilt, azimuth=azimuth, year=year, dry_run=dry_run,
        capacity=capacity.model_dump(exclude_none=True) if capacity is not None else None,
        weather_manifest_name=weather_manifest_name,
        losses=losses.model_dump(exclude_none=True) if losses is not None else None,
        report_pages=report_pages, tracking=tracking.model_dump(exclude_none=True) if tracking is not None else None,
        tracking_variant=tracking_variant, nominal_dc_power_kw=nominal_dc_power_kw,
        tracking_power_source=tracking_power_source)

@categorized_tool()
def pvsyst_run_simulation(project: str, variant: str,
                          sfi_name: str = "", csv_name: str = "", pdf_name: str = "",
                          met_name: str = "", rvt_name: str = "",
                          params_name: str = "", batch_params_name: str = "",
                          batch_rvt_name: str = "", site_name: str = "",
                          synthetic_weather: bool = False,
                          time_step: str = "", start_date: str = "", end_date: str = "",
                          report_language: str = "", report_pages: str = "",
                          recompute_shading: bool | None = None,
                          log_level: int | None = None) -> dict:
    """Run an existing project/variant. Regular CSV/PDF go to Results.
    For a single run, csv_name accepts a bare filename (e.g. run01) or a .csv
    filename (run01.csv); both create Results/run01.csv. Existing outputs are
    never overwritten. For batch_params_name, leave csv_name/pdf_name empty:
    the CLI generates UserBatch/UserHourly outputs automatically.
    Optional inputs live in Models (SFI,RVT), Meteo (MET), UserData (DAT),
    UserBatch (batch CSV/RVT), or Sites (SIT). Unsupported options fail early.
    Each run can consume a license execution."""
    license_state = cli().license_info()
    if license_state.get("status") == "TRIAL" and license_state.get("remaining_executions") == 0:
        raise ValueError("PVsystCLI reports no remaining trial executions")
    return cli().run_simulation(
        project, variant,
        sfi=optional_path("Models", sfi_name, ".sfi"),
        out_csv=simulation_csv_path(csv_name),
        report_pdf=optional_path("Results", pdf_name, ".pdf"),
        met=optional_path("Meteo", met_name, ".met"),
        rvt=optional_path("Models", rvt_name, ".rvt"),
        params=optional_path("UserData", params_name, ".dat"),
        batch_params=optional_path("UserBatch", batch_params_name, ".csv"),
        batch_rvt=optional_path("UserBatch", batch_rvt_name, ".rvt"),
        site=optional_path("Sites", site_name, ".sit"),
        synthetic_weather=synthetic_weather, time_step=time_step or None,
        start_date=start_date or None, end_date=end_date or None,
        report_language=report_language or None,
        report_pages=[p.strip() for p in report_pages.split(",")] if report_pages else None,
        recompute_shading=recompute_shading, log_level=log_level,
    )

@categorized_tool()
def pvsyst_simulate_design(spec: SingleDesignRequest, dry_run: StrictBool = True) -> dict:
    """Preflight or execute one bounded design flow from declared inputs without the GUI.
    spec.new_project names the NEW working project and is required. scope is fixed to
    fixed_unshaded_grid: one subarray, one ordinary fixed plane, a Grid system,
    PRCorrTemp=False and no active geometry, layout or shading scene. The flow locates
    exact loose PAN/OND components and an eligible workspace template. If no template
    or variant is pinned and no candidate is eligible, it can derive a NEW template from the installed 8.1
    residential seed, preserving its source fields and declaring the target MPPT counts.
    It builds the linear design envelope from declared inputs, plans integer capacity,
    configures the working project and, with dry_run=false, runs the full year.
    The verified 8.1 workspace root must be named PVsyst8.1_Data. Read-only preflight
    reports missing standard folders and blocks file conflicts or escaping paths;
    execution fills missing empty folders without renaming the root or replacing files.
    weather.mode=existing uses the named workspace SIT/MET pair, weather.mode=csv names a
    staged standard hourly CSV, and weather.mode=open_meteo acquires one public ERA5 year.
    Dry-run with open_meteo or a not-yet-created standard template returns
    ready_for_weather with execution_preflight_complete=false: this is an input/template
    plan, not a runnable native design. electrical_design requires declared coefficients
    with units and sources, and source-declared current limits. Voltage/current field
    names imply V/A when units are omitted; explicitly contradictory units are blocked.
    Missing required data returns concrete issues; an empty object never becomes ready.
    losses overrides LIDLoss, PMismMPP and ModuleQualityLoss on the subarray and UFactor
    on the main array. Every field of spec that is not supplied keeps the template value;
    no loss, coefficient, temperature or azimuth is fitted, inferred or defaulted, and no
    design is certified here. status is blocked/ready/ready_for_weather/completed/
    needs_review/failed; inspect it, not just MCP is_error. Year is 1969..2068. The
    returned issues, warnings, transactions and recovery steps state exactly what was and
    was not changed. Optional spec.report_pages selects native PDF page names; omitted
    means the complete default report, and invalid lists stop before discovery/weather."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return simulate_design(current, store, spec=spec.model_dump(exclude_none=True), dry_run=dry_run)

@categorized_tool()
def pvsyst_prepare_sourced_design(pan_name: StrictStr, ond_name: StrictStr,
                                  sources: dict[str, SourceTextRequest],
                                  declarations: list[SourceDeclarationRequest],
                                  design: SourcedDesignGoals | None = None) -> dict:
    """Bind caller-declared excerpts to source text and build the design-envelope request.
    pan_name and ond_name are exact loose component filenames (workspace first, then the
    installed library): a manufacturer-only search is never substituted and no model is
    inferred. The identity label is composed from the owning objects as "PAN manufacturer
    PAN model / OND manufacturer OND model" and cannot be overridden by the caller. A
    root-only text component and an 8.1.6 GUI component are both accepted: the root object
    is required, a PVObject_Commercial block is optional and its identity wins, while a
    disagreement between the two, a repeated declared field or a legacy binary or non-UTF-8
    file returns blocked with the concrete field. sources maps short ids to declared source
    text whose kind is manufacturer, site or assumption; declarations claims one value per
    field, each tied to a source id, an excerpt and the exact value/unit tokens it must
    agree with, and the declared source text itself is never returned. design carries
    explicit engineering goals only, so no envelope, coefficient or limit can be injected
    without a citation. status is ready, needs_review or blocked; the assembled
    electrical_design draft, the reused envelope result and the citation provenance come
    back, and the declared excerpts are a source-consistency check between the claimed
    value and the quoted loose component identity, not a manufacturer certification."""
    selected = {}
    store = components()
    with store.lock:  # one lock for both reads: a concurrent workspace update cannot split them
        for kind, filename in (("PAN", pan_name), ("OND", ond_name)):
            field, message, payload = sourced_component(store, kind, filename)
            if payload is None:
                return {"status": "blocked", "stage": "components", "scope": SOURCED_DESIGN_SCOPE,
                        "issues": [{"field": field, "message": message}], "warnings": [],
                        "electrical_design": None, "capacity": None, "envelope": None,
                        "missing_fields": [], "needs_review": False, "provenance": []}
            selected[kind] = payload
    label = (f"{selected['PAN']['manufacturer']} {selected['PAN']['model']} / "
             f"{selected['OND']['manufacturer']} {selected['OND']['model']}")
    return prepare_sourced_design(
        selected["PAN"]["electrical"], selected["OND"]["electrical"], manufacturer_model=label,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        design=design.model_dump(exclude_none=True) if design is not None else None)

@categorized_tool()
def pvsyst_run_sourced_house_task(spec: SourcedHouseTaskRequest, dry_run: StrictBool = True,
        confirm: StrictBool = False, resume: StrictBool = False, retry_simulation: StrictBool = False,
        accept_source_review: StrictBool = False) -> dict:
    """Bind cited inputs, prepare a source project without an annual run, then deliver one house.
    design reuses SingleDesignRequest, but module/inverter require exact loose file_name and
    capacity/electrical_design must be absent: the existing cited-source helper builds the
    electrical draft. sources/declarations/goals reuse pvsyst_prepare_sourced_design semantics;
    an explicit design.target_dc_kw is also an engineering goal, never a guessed rating.
    Missing/mismatched citations return needs_context and the concrete draft without writes.
    design.new_project is the NEW source; spec.new_project is a DISTINCT final house project.
    Complete house/layout/module geometry reuse the existing native house input contract.
    Dry-run on a fresh source is ready_for_preparation or ready_for_weather, not full house
    preflight; it writes nothing and launches no GUI. Execution requires dry_run=false AND
    confirm=true. Source review additionally requires accept_source_review=true, and remains
    needs_review in the final result; confirmation never certifies source authenticity.
    Prepared source and house manifests own recovery: resume rechecks request/provenance,
    canonical inputs and prepared files, then resumes the existing child checkpoint without
    another preparation/annual baseline. Partial source preparation remains inspectable via
    its existing recovery transactions, not automatically repeated or silently overwritten.
    Final annual execution is only the existing native house task. GUI/version/DPI/topology
    bounds are unchanged. Existing user windows are not taken over, INI is not edited here.
    Weather assets/source review are checked; the house path does not add hourly comparison
    against the source weather CSV. It reports weather_native_comparison_performed=false.
    Raw source text is not persisted or echoed; accepted citation provenance is preserved.
    No new PV/sizing/dispatch model or general job framework."""
    from pvsyst_auto.workflows.sourced_house import run_sourced_house_task
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    with store.lock:
        goals = spec.goals.model_dump(exclude_none=True) if spec.goals is not None else {}
        if (spec.design.target_dc_kw is not None and "target_dc_kw" not in goals
                and not any(entry.field == "target_dc_kw" for entry in spec.declarations)):
            goals["target_dc_kw"] = spec.design.target_dc_kw
        binding = pvsyst_prepare_sourced_design(
            pan_name=spec.design.module.file_name or "", ond_name=spec.design.inverter.file_name or "",
            sources=spec.sources, declarations=spec.declarations, design=SourcedDesignGoals(**goals))
        request = spec.model_dump(exclude_none=True, exclude={"sources", "declarations", "goals"})
        return run_sourced_house_task(store, component_store, current, request=request, binding=binding,
            dry_run=dry_run, confirm=confirm, resume=resume, retry_simulation=retry_simulation,
            accept_source_review=accept_source_review)
