"""Projects & Variants MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Literal
from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.project.tracking import create_tracking_variant
from pvsyst_auto.scene.author import create_house_pv_scene
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.api.schemas import ACCablingUpdates, AgeingUpdates, AuxiliaryUpdates, BifacialUpdates, DCCablingUpdates, GeneratorUpdates, GridLimitUpdates, HorizonUpdates, HouseSpec, HvTransformerSpec, IamUpdates, InverterTemperatureUpdates, MeshHouseSpec, ModuleGeometry, MvTransformerSpec, NamedPVTablesLayout, NativeEconomicsUpdates, ObstacleSpec, OrientationConvertParams, P50P90Updates, PVTableLayout, PlacedPVTableLayout, PowerControlsUpdates, PowerFactorUpdates, SeasonalUpdates, ShedGeometryUpdates, SingleDesignRequest, SoilingUpdates, SpectralUpdates, StrictNum, StringLayoutBinding, StringMismatchUpdates, SystemUnavailabilityUpdates, TrackingControls
from pvsyst_auto.api.context import categorized_tool, cli, components, variant_call, variant_store


@categorized_tool()
def pvsyst_list_projects() -> list[str]:
    """List PRJ filenames in the configured workspace."""
    return cli().list_projects()

@categorized_tool()
def pvsyst_list_variants(project: str) -> list[str]:
    """List variant IDs belonging to an existing project."""
    return cli().list_variants(project)

@categorized_tool()
def pvsyst_inspect_project(project: str) -> dict:
    """List PRJ/VC SHA-256 hashes, embedded SIT/MET references, missing workspace files, and source mismatches."""
    return variant_call("inspect_project", project)

@categorized_tool()
def pvsyst_create_project(project: str, project_content: str, variants: dict[str, str]) -> dict:
    """Create a new PRJ/VC set (1..127 variants) from complete UTF-8 text."""
    return variant_call("create_project", project, project_content, variants)

@categorized_tool()
def pvsyst_update_project_parameters(project: str, project_name: StrictStr,
                                     expected_files: dict[str, str] | None = None,
                                     dry_run: StrictBool = False,
                                     add_utf8_bom: StrictBool = False) -> dict:
    """Update a project's display title (Comment, 1..80 UTF-8 bytes) with backup and optional file hash checks."""
    return variant_call("update_project_parameters", project, expected_files, project_name, dry_run, add_utf8_bom)

@categorized_tool()
def pvsyst_update_project_sources(project: str, site_name: str, met_name: str,
                                  expected_files: dict[str, str] | None = None) -> dict:
    """Bind a workspace SIT/MET pair across PRJ and all its VC variants with backup and optional file hash checks."""
    return variant_call("update_project_sources", project, site_name, met_name, expected_files)

@categorized_tool()
def pvsyst_get_variant_components(project: str, variant: str) -> dict:
    """Read component references in a variant and its current SHA-256."""
    return variant_call("inspect", project, variant)

@categorized_tool()
def pvsyst_clone_variant(project: str, source_variant: str, new_variant: str,
                         updates: dict[str, str] | None = None, subarray_id: StrictInt = 1,
                         source_project: str | None = None) -> dict:
    """Clone a variant into a new VC (optionally from source_project) with optional component updates."""
    return variant_call("clone", project, source_variant, new_variant, updates, subarray_id, source_project)

@categorized_tool()
def pvsyst_update_variant_components(project: str, variant: str,
                                     updates: dict[str, str],
                                     expected_sha256: str | None = None,
                                     subarray_id: StrictInt = 1) -> dict:
    """Replace PAN/OND/BTR/GEN references in a variant with backup and optional SHA-256 check."""
    return variant_call("update", project, variant, updates, expected_sha256, subarray_id)

@categorized_tool()
def pvsyst_validate_variant_structure(project: str, variant: str) -> dict:
    """Validate orientation IDs, TOrientGroup counts, grid circuit references, and serialized inverter/string counts."""
    return variant_call("validate_structure", project, variant)

@categorized_tool()
def pvsyst_clone_subarray(project: str, variant: str, source_subarray_id: StrictInt,
                          expected_sha256: str | None = None,
                          clone_orientation: StrictBool = False,
                          share_inverter: StrictBool = False) -> dict:
    """Duplicate a grid subarray and its owned inverter groups (optionally cloning its fixed plane or sharing its inverter)."""
    return variant_call("clone_subarray", project, variant, source_subarray_id, expected_sha256, clone_orientation, share_inverter)

@categorized_tool()
def pvsyst_remove_subarray(project: str, variant: str, subarray_id: StrictInt,
                           expected_sha256: str | None = None,
                           remove_orientation: StrictBool = False,
                           shared_member: StrictBool = False) -> dict:
    """Remove a grid subarray and its owned inverter groups (optionally pruning an unused fixed plane or shared MPPT member)."""
    return variant_call("remove_subarray", project, variant, subarray_id, expected_sha256, remove_orientation, shared_member)

@categorized_tool()
def pvsyst_get_variant_parameters(project: str, variant: str,
                                  include_saved_results: StrictBool = False,
                                  include_economics: StrictBool = False,
                                  include_topology: StrictBool = False) -> dict:
    """Inspect stored variant state (orientations, subarrays, inverter_state, main_array, losses,
    energy management, transformers, cabling, storage, and shading_scene), optionally including
    saved_results, economics, and raw pvCircuit topology."""
    return variant_call("inspect_parameters", project, variant,
                        include_saved_results=include_saved_results,
                        include_economics=include_economics,
                        include_topology=include_topology)

@categorized_tool()
def pvsyst_review_project_inputs(project: StrictStr, variant: StrictStr,
                                  preparation_manifest_name: StrictStr) -> dict:
    """Compare a preparation manifest from pvsyst_prepare_design_project against saved project/variant fields."""
    from pvsyst_auto.engineering.review import review_project_inputs
    return review_project_inputs(variant_store(), project=project, variant=variant,
                                 preparation_manifest_name=preparation_manifest_name)

@categorized_tool()
def pvsyst_update_variant_parameters(project: str, variant: str,
                                     expected_sha256: str | None = None, subarray_id: StrictInt = 1,
                                     orientation_id: StrictInt = 1,
                                     subarray_updates: dict[str, StrictNum] | None = None,
                                     orientation_updates: dict[str, StrictNum] | None = None,
                                     generator_updates: GeneratorUpdates | None = None,
                                     dry_run: StrictBool = False,
                                     power_sharing_enabled: StrictBool | None = None,
                                     independent_mppt_enabled: StrictBool | None = None,
                                     main_array_updates: dict[str, StrictNum] | None = None,
                                     variant_name: StrictStr | None = None,
                                     add_utf8_bom: StrictBool = False,
                                     soiling_updates: SoilingUpdates | None = None,
                                     grid_limit_updates: GridLimitUpdates | None = None,
                                     auxiliary_updates: AuxiliaryUpdates | None = None,
                                     seasonal_updates: SeasonalUpdates | None = None,
                                     horizon_updates: HorizonUpdates | None = None,
                                     inverter_temperature_updates: InverterTemperatureUpdates | None = None,
                                     power_factor_updates: PowerFactorUpdates | None = None,
                                     p50_p90_updates: P50P90Updates | None = None,
                                     power_controls_updates: PowerControlsUpdates | None = None,
                                     string_mismatch_updates: StringMismatchUpdates | None = None,
                                     ageing_updates: AgeingUpdates | None = None,
                                      iam_updates: IamUpdates | None = None,
                                      ageing_enabled: StrictBool | None = None,
                                      system_unavailability_updates: SystemUnavailabilityUpdates | None = None,
                                      bifacial_updates: BifacialUpdates | None = None,
                                      spectral_updates: SpectralUpdates | None = None,
                                      native_economics_updates: NativeEconomicsUpdates | None = None) -> dict:
    """Update variant parameters (subarray/orientation/main_array scalars, generator, power sharing,
    independent MPPT, display name, soiling, grid limit, auxiliaries, seasonal tilt, horizon,
    inverter temperature, power factor, power controls, P50/P90, string mismatch, ageing, IAM,
    system unavailability periods/random/removal, bifacial pvBiFacial, spectral correction, or
    native TEcoEvaluation for PDF financial pages) with SHA-256 optimistic locking and rollback backup."""
    def _dump(model, *, exclude_none: bool = True):
        return model.model_dump(exclude_none=exclude_none) if model is not None else None

    return variant_call("update_parameters", project, variant, expected_sha256,
                        subarray_id, orientation_id, subarray_updates, orientation_updates,
                        _dump(generator_updates),
                        dry_run, power_sharing_enabled, independent_mppt_enabled, main_array_updates, variant_name, add_utf8_bom,
                        _dump(soiling_updates), _dump(grid_limit_updates), _dump(auxiliary_updates),
                        _dump(seasonal_updates), _dump(horizon_updates), _dump(inverter_temperature_updates),
                        _dump(power_factor_updates), _dump(p50_p90_updates, exclude_none=False),
                        _dump(power_controls_updates), _dump(string_mismatch_updates),
                        _dump(ageing_updates), _dump(iam_updates), ageing_enabled,
                        _dump(system_unavailability_updates), _dump(bifacial_updates),
                        _dump(spectral_updates), _dump(native_economics_updates))

@categorized_tool()
def pvsyst_convert_orientation(
    project: StrictStr,
    source_variant: StrictStr,
    new_variant: StrictStr,
    target_type: Literal["fixed", "seasonal", "sheds", "tracker_ns", "tracker_2axis"],
    params: OrientationConvertParams | None = None,
    dry_run: StrictBool = True,
    variant_name: StrictStr | None = None,
    add_utf8_bom: StrictBool = False,
) -> dict:
    """Create a new variant converting an existing variant's orientation type in-place
    (fixed <-> seasonal <-> sheds <-> tracker_ns <-> tracker_2axis), automatically stripping
    incompatible 3D shading and stale simulation caches. Defaults to dry_run=true."""
    from pvsyst_auto.project.tracking import convert_orientation
    return convert_orientation(
        variant_store(),
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        target_type=target_type,
        params=params.model_dump(exclude_none=True) if params is not None else None,
        dry_run=dry_run,
        variant_name=variant_name,
        add_utf8_bom=add_utf8_bom,
    )

@categorized_tool()
def pvsyst_create_tracking_variant(project: StrictStr, source_variant: StrictStr,
                                   new_variant: StrictStr, tracking: TrackingControls,
                                   dry_run: StrictBool = True,
                                   variant_name: StrictStr | None = None,
                                   add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant updating tracking parameters (UnlimTracking, TrackHorizNSAxis,
    TrackTiltedAxis, TrackVertAxis, TrackSunShield, TrackHorizAxis, TrackTwoAxis, TrackFrameNS,
    or TrackFrameEW) on an existing tracking template."""
    return create_tracking_variant(variant_store(), project=project, source_variant=source_variant,
                                   new_variant=new_variant, tracking=tracking.model_dump(exclude_none=True),
                                   dry_run=dry_run, variant_name=variant_name,
                                   add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_ac_cabling_variant(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, ac: ACCablingUpdates,
                                     dry_run: StrictBool = True, variant_name: StrictStr | None = None,
                                     add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant configuring uniform per-inverter AC cable length (0.1 m precision)
    and optional conductor/section."""
    from pvsyst_auto.energy.system import create_ac_cabling_variant
    return create_ac_cabling_variant(variant_store(), project=project, source_variant=source_variant,
        new_variant=new_variant, ac=ac.model_dump(exclude_none=True), dry_run=dry_run,
        variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_dc_cabling_variant(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, dc: DCCablingUpdates,
                                     dry_run: StrictBool = True,
                                     variant_name: StrictStr | None = None,
                                     add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant configuring DC cabling resistance (mOhm) or STC loss (%) headlessly;
    built-in encrypted .PAN modules support mode='resistance' and reject mode='percent'."""
    from pvsyst_auto.energy.dc import create_dc_cabling_variant
    return create_dc_cabling_variant(variant_store(), project=project, source_variant=source_variant,
        new_variant=new_variant, dc=dc.model_dump(), dry_run=dry_run,
        variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_configure_transformers(
    project: StrictStr,
    source_variant: StrictStr,
    new_variant: StrictStr,
    mv: MvTransformerSpec | None = None,
    hv: HvTransformerSpec | None = None,
    remove_transformers: StrictBool = False,
    dry_run: StrictBool = True,
    variant_name: StrictStr | None = None,
    add_utf8_bom: StrictBool = False,
) -> dict:
    """Create a new variant configuring single/parallel MV transformers, optional cascaded HV substation
    transformer, custom iron/copper losses, MV/HV AC collection cables, and night disconnect (or strip all
    transformers with remove_transformers=true). Enforces PVsyst 8.1.7 native rating (80%-200% PNomInvAC),
    voltage ordering, and loss bounds (<=1% iron, <=5% copper). Defaults to dry_run=true."""
    from pvsyst_auto.energy.transformers import configure_transformers
    mv_payload = (
        {"night_disconnect": mv.night_disconnect}
        if (mv is not None and mv.model_fields_set == {"night_disconnect"})
        else (mv.model_dump(exclude_none=True) if mv is not None else None)
    )
    return configure_transformers(
        variant_store(),
        project=project,
        source_variant=source_variant,
        new_variant=new_variant,
        mv=mv_payload,
        hv=hv.model_dump(exclude_none=True) if hv is not None else None,
        remove_transformers=remove_transformers,
        dry_run=dry_run,
        variant_name=variant_name,
        add_utf8_bom=add_utf8_bom,
    )

@categorized_tool()
def pvsyst_create_shed_geometry_variant(project: StrictStr, source_variant: StrictStr,
                                        new_variant: StrictStr, geometry: ShedGeometryUpdates,
                                        dry_run: StrictBool = True,
                                        variant_name: StrictStr | None = None,
                                        add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant updating Sheds or SunShields pitch, tilt, count, width,
    inactive bands, shading factor, or electrical shading flag."""
    from pvsyst_auto.project.bifacial import create_shed_variant
    return create_shed_variant(variant_store(), project=project, source_variant=source_variant,
        new_variant=new_variant, geometry=geometry.model_dump(exclude_none=True),
        dry_run=dry_run, variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_house_pv_scene(project: StrictStr, variant: StrictStr, output_name: StrictStr,
                                 house: MeshHouseSpec | HouseSpec,
                                 layout: NamedPVTablesLayout | PlacedPVTableLayout | PVTableLayout,
                                 dry_run: StrictBool = True,
                                 module_geometry: ModuleGeometry | None = None,
                                 string_layout: StringLayoutBinding | None = None,
                                 obstacles: list[ObstacleSpec] | None = None) -> dict:
    """Serialize named architectural meshes, rooftop PV tables, optional per-cell string_layout,
    and optional environment obstacles (building, tree, wall, mesh) into a PVCollada 2.0 (.pvc2)
    scene in workspace/UserData."""
    store = variant_store()
    return create_house_pv_scene(store, store.components, project=project, variant=variant,
                                 output_name=output_name, house=house.model_dump(),
                                 layout=layout.model_dump(exclude_none=True), dry_run=dry_run,
                                 module_geometry=(module_geometry.model_dump()
                                                  if module_geometry is not None else None),
                                 string_layout=(string_layout.model_dump(exclude_none=True)
                                                if string_layout is not None else None),
                                 obstacles=([obs.model_dump(exclude_none=True) for obs in obstacles]
                                            if obstacles is not None else None))

@categorized_tool()
def pvsyst_read_house_pv_scene(scene_name: StrictStr) -> dict:
    """Parse a UserData/*.pvc2 XML scene back into structured house, layout, and string_layout arguments."""
    return read_house_pv_scene(components(), scene_name)

@categorized_tool()
def pvsyst_import_house_pv_scene(scene_name: StrictStr, variant_name: StrictStr,
                                   dry_run: StrictBool = True, confirm: StrictBool = False,
                                   shading_mode: Literal["linear", "module_layout"] = "linear",
                                   method: Literal["native"] = "native") -> dict:
    """Import a .pvc2 house/PV scene into a new variant via the headless native builder,
    supporting linear and module_layout shading."""
    if method != "native":
        raise ValueError("Legacy Win32 GUI scene import ('gui') was removed in v0.6.0; use method='native'")
    store = variant_store()
    with store.lock:
        if shading_mode != "module_layout":
            # A scene whose recipe carries an explicit binding must not be fed to the linear
            # importer, which would silently ignore per-cell wiring. Detect it on the recipe and
            # stop before any write.
            recovered = read_house_pv_scene(store.components, scene_name)
            binding = (recovered.get("create_arguments") or {}).get("string_layout")
            if binding is not None:
                return {"status": "blocked", "stage": "preflight", "method": method,
                        "scene_name": scene_name, "dry_run": dry_run,
                        "native_consumption_verified": False,
                        "issues": ["This scene declares an explicit string_layout; it can only be "
                                   "imported with method='native' and shading_mode='module_layout'. "
                                   "The linear importer auto-allocates strings and will not drop "
                                   "declared per-cell wiring silently."]}
        from pvsyst_auto.native.headless import import_house_pv_scene_native
        return import_house_pv_scene_native(store, store.components, scene_name, variant_name,
                                            dry_run=dry_run, confirm=confirm, shading_mode=shading_mode)

@categorized_tool()
def pvsyst_prepare_design_project(spec: SingleDesignRequest, dry_run: StrictBool = True,
                                  verbose: StrictBool = False) -> dict:
    """Run component lookup, weather preparation, and project configuration (fixed_unshaded_grid,
    ground_sheds, or ground_tracker with optional ground_layout/bifacial) without launching an annual simulation.
    Set verbose=true to expand per-template discovery rejection details."""
    return prepare_design_project(cli(), variant_store(), spec=spec.model_dump(exclude_none=True),
                                  dry_run=dry_run, verbose=verbose)
