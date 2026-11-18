"""Projects & Variants MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Literal
from pydantic import StrictBool, StrictInt, StrictStr
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.project.tracking import create_tracking_variant
from pvsyst_auto.project.domes import create_dome_allocation_variant
from pvsyst_auto.scene.author import create_house_pv_scene
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.api.schemas import ACCablingUpdates, AgeingUpdates, AuxiliaryUpdates, DCCablingUpdates, GeneratorUpdates, GridLimitUpdates, HorizonUpdates, HouseSpec, IamUpdates, InverterTemperatureUpdates, MeshHouseSpec, ModuleGeometry, NamedPVTablesLayout, ObstacleSpec, P50P90Updates, PVTableLayout, PlacedPVTableLayout, PowerControlsUpdates, PowerFactorUpdates, SeasonalUpdates, ShedGeometryUpdates, SingleDesignRequest, SoilingUpdates, StrictNum, StringLayoutBinding, StringMismatchUpdates, SystemUnavailabilityUpdates, TrackingControls
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
                                       system_unavailability_updates: SystemUnavailabilityUpdates | None = None) -> dict:
    """Update variant parameters (subarray/orientation/main_array scalars, generator, power sharing,
    independent MPPT, display name, soiling, grid limit, auxiliaries, seasonal tilt, horizon,
    inverter temperature, power factor, power controls, P50/P90, string mismatch, ageing, IAM,
    or system unavailability periods/removal) with SHA-256 optimistic locking and rollback backup."""
    return variant_call("update_parameters", project, variant, expected_sha256,
                        subarray_id, orientation_id, subarray_updates, orientation_updates,
                        generator_updates.model_dump(exclude_none=True) if generator_updates is not None else None,
                        dry_run, power_sharing_enabled, independent_mppt_enabled, main_array_updates, variant_name, add_utf8_bom,
                        soiling_updates.model_dump(exclude_none=True) if soiling_updates is not None else None,
                        grid_limit_updates.model_dump(exclude_none=True) if grid_limit_updates is not None else None,
                        auxiliary_updates.model_dump(exclude_none=True) if auxiliary_updates is not None else None,
                        seasonal_updates.model_dump(exclude_none=True) if seasonal_updates is not None else None,
                        horizon_updates.model_dump(exclude_none=True) if horizon_updates is not None else None,
                        inverter_temperature_updates.model_dump(exclude_none=True) if inverter_temperature_updates is not None else None,
                        power_factor_updates.model_dump(exclude_none=True) if power_factor_updates is not None else None,
                        p50_p90_updates.model_dump() if p50_p90_updates is not None else None,
                        power_controls_updates.model_dump(exclude_none=True) if power_controls_updates is not None else None,
                        string_mismatch_updates.model_dump(exclude_none=True) if string_mismatch_updates is not None else None,
                        ageing_updates.model_dump(exclude_none=True) if ageing_updates is not None else None,
                        iam_updates.model_dump(exclude_none=True) if iam_updates is not None else None,
                        ageing_enabled,
                        system_unavailability_updates.model_dump() if system_unavailability_updates is not None else None)

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
    from pvsyst_auto.energy.ac import create_ac_cabling_variant
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
def pvsyst_create_mv_night_disconnect_variant(project: StrictStr, source_variant: StrictStr,
                                              new_variant: StrictStr, night_disconnect: StrictBool,
                                              dry_run: StrictBool = True,
                                              variant_name: StrictStr | None = None,
                                              add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant toggling the MV transformer NightDisconnect switch and main-array bit 29."""
    from pvsyst_auto.energy.transformer import create_mv_night_variant
    return create_mv_night_variant(variant_store(), project=project, source_variant=source_variant,
        new_variant=new_variant, night_disconnect=night_disconnect, dry_run=dry_run,
        variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_shed_geometry_variant(project: StrictStr, source_variant: StrictStr,
                                        new_variant: StrictStr, geometry: ShedGeometryUpdates,
                                        dry_run: StrictBool = True,
                                        variant_name: StrictStr | None = None,
                                        add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant updating Sheds or SunShields pitch, tilt, count, width,
    inactive bands, shading factor, or electrical shading flag."""
    from pvsyst_auto.project.sheds import create_shed_variant
    return create_shed_variant(variant_store(), project=project, source_variant=source_variant,
        new_variant=new_variant, geometry=geometry.model_dump(exclude_none=True),
        dry_run=dry_run, variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_dome_allocation_variant(project: StrictStr, source_variant: StrictStr,
                                          new_variant: StrictStr,
                                          first_orientation_strings: StrictInt,
                                          dry_run: StrictBool = True,
                                          variant_name: StrictStr | None = None,
                                          add_utf8_bom: StrictBool = False) -> dict:
    """Create a new variant rebalancing string counts between the two faces of a paired Dome template."""
    return create_dome_allocation_variant(variant_store(), project=project, source_variant=source_variant,
                                          new_variant=new_variant,
                                          first_orientation_strings=first_orientation_strings,
                                          dry_run=dry_run, variant_name=variant_name,
                                          add_utf8_bom=add_utf8_bom)

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
                                   method: Literal["gui", "native"] = "native") -> dict:
    """Import a .pvc2 house/PV scene into a new variant via the headless builder (default, method='native')
    or the legacy PVsyst GUI path (method='gui'), supporting linear and module_layout shading."""
    current = cli()
    store = variant_store()
    with store.lock:
        if not (method == "native" and shading_mode == "module_layout"):
            # A scene whose recipe carries an explicit binding must not be fed to the GUI/linear
            # importer, which would silently ignore per-cell wiring. Detect it on the recipe and
            # stop before any write; old scenes without a binding keep their exact legacy routing.
            recovered = read_house_pv_scene(store.components, scene_name)
            binding = (recovered.get("create_arguments") or {}).get("string_layout")
            if binding is not None:
                return {"status": "blocked", "stage": "preflight", "method": method,
                        "scene_name": scene_name, "dry_run": dry_run,
                        "native_consumption_verified": False,
                        "issues": ["This scene declares an explicit string_layout; it can only be "
                                   "imported with method='native' and shading_mode='module_layout'. "
                                   "The GUI/linear importer auto-allocates strings and will not drop "
                                   "declared per-cell wiring silently."]}
        if method == "native":
            from pvsyst_auto.native.headless import import_house_pv_scene_native
            return import_house_pv_scene_native(store, store.components, scene_name, variant_name,
                                                dry_run=dry_run, confirm=confirm, shading_mode=shading_mode)
        if method != "gui":
            raise ValueError("method must be gui or native")
        from pvsyst_auto.native.scene import import_house_pv_scene
        return import_house_pv_scene(store, store.components, current, scene_name, variant_name,
                                     dry_run=dry_run, confirm=confirm, shading_mode=shading_mode)

@categorized_tool()
def pvsyst_prepare_design_project(spec: SingleDesignRequest, dry_run: StrictBool = True) -> dict:
    """Run component lookup, weather preparation, and project configuration without launching an annual simulation."""
    return prepare_design_project(cli(), variant_store(), spec=spec.model_dump(exclude_none=True), dry_run=dry_run)
