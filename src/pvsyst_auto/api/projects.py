"""Projects & Variants MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Literal
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr
from pvsyst_auto.project.variants import VariantStore
from pvsyst_auto.workflows.design import prepare_design_project
from pvsyst_auto.project.tracking import create_tracking_variant
from pvsyst_auto.project.domes import create_dome_allocation_variant
from pvsyst_auto.scene.author import create_house_pv_scene
from pvsyst_auto.scene.reader import read_house_pv_scene
from pvsyst_auto.api.schemas import ACCablingUpdates, AgeingUpdates, AuxiliaryUpdates, GeneratorUpdates, GridLimitUpdates, HorizonUpdates, HouseSpec, IamUpdates, InverterTemperatureUpdates, MeshHouseSpec, ModuleGeometry, NamedPVTablesLayout, NativeDCUpdates, P50P90Updates, PVTableLayout, PlacedPVTableLayout, PowerControlsUpdates, PowerFactorUpdates, SeasonalUpdates, ShedGeometryUpdates, SingleDesignRequest, SoilingUpdates, StringLayoutBinding, StringMismatchUpdates, SystemUnavailabilityUpdates, TrackingControls
from pvsyst_auto.api.context import categorized_tool, cli, components, variant_call


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
    """List PRJ/VC hashes and embedded SIT/MET references; flag missing workspace
    files and differences between project and variant sources. project_name reads
    only the UTF-8 root header (null if absent/duplicate there), not effective
    native decoding of a BOM-less non-ASCII file. utf8_bom reports the PRJ marker."""
    return variant_call("inspect_project", project)

@categorized_tool()
def pvsyst_archive_project(project: str, expected_files: dict[str, str],
                           confirm: bool = False) -> dict:
    """Archive up to 128 PRJ/VC members after matching every hash; confirm=true."""
    return variant_call("archive_project", project, expected_files, confirm)

@categorized_tool()
def pvsyst_archive_variant(project: str, variant: str, expected_sha256: str,
                           confirm: bool = False) -> dict:
    """Archive a VC only if another variant remains, with SHA guard; confirm=true."""
    return variant_call("archive_variant", project, variant, expected_sha256, confirm)

@categorized_tool()
def pvsyst_restore_project_archive(project: str, archive_name: str,
                                   confirm: bool = False) -> dict:
    """Restore a complete archived project or one archived variant; confirm=true."""
    return variant_call("restore_archive", project, archive_name, confirm)

@categorized_tool()
def pvsyst_create_project(project: str, project_content: str, variants: dict[str, str]) -> dict:
    """Create a NEW PRJ/VC set from complete UTF-8 text without an existing project.
    variants maps VC identifiers to their full text; 1..127 variants. Preserves
    supplied BOM/newlines and references, rejects existing same-prefix members
    and duplicate normalized IDs; handled write errors remove only created files.
    Checks envelopes and subarray sections, not full schema or physical validity.
    Site/meteo/components remain external; inspect, validate and simulate the new
    project. No invented blank design/defaults. Returns the member hash map."""
    return variant_call("create_project", project, project_content, variants)

@categorized_tool()
def pvsyst_clone_project(source_project: str, new_project: str) -> dict:
    """Create a NEW project by copying an existing PRJ and all its VC variants.
    Embedded site/meteo snapshots and external component references are preserved."""
    return variant_call("clone_project", source_project, new_project)

@categorized_tool()
def pvsyst_update_project_parameters(project: str, expected_files: dict[str, str],
                                     project_name: StrictStr, dry_run: StrictBool = False,
                                     add_utf8_bom: StrictBool = False) -> dict:
    """Edit an existing project's display title only, preserving its PRJ/VC filenames.
    project_name requires a strict string of 1..80 printable UTF-8 bytes without
    controls/surrounding whitespace. Requires one existing owning header Comment,
    complete recognized PRJ object syntax and the full hash map from inspect_project.
    No blank name field/default is invented. dry_run returns the full candidate
    file map and diff without backups/writes; repeated names are no-ops.
    Non-ASCII names require UTF-8 BOM: existing BOM is preserved, adding one needs
    explicit add_utf8_bom=true. This changes UTF-8 decoding for the whole PRJ,
    not merely the title; bom_added is explicit even in a BOM-only transaction.
    Only changed PRJ bytes are replaced; unchanged VC bytes/timestamps/file IDs
    stay intact. Full-project snapshots and final file-set guards are reused.
    Restore using restore_project_sources with the returned backups/files map.
    Does not rename IDs, change sources, nested names, physical settings or VC names.
    This is the GUI project-name field, not its filename-based window caption.
    Close a loaded GUI project before editing its file; inspect again after GUI saves.
    The 1..80-byte limit is the verified adapter contract, not a native GUI limit.
    BOM-less UTF-8 inspection reads raw text, not effective GUI/CLI decoding.
    This is optimistic conflict checking, not cross-process locking/crash atomicity."""
    return variant_call("update_project_parameters", project, expected_files, project_name, dry_run, add_utf8_bom)

@categorized_tool()
def pvsyst_update_project_sources(project: str, site_name: str, met_name: str,
                                  expected_files: dict[str, str]) -> dict:
    """Persist a workspace SIT/MET pair across PRJ and all VC snapshots with
    full-project SHA preconditions and byte-identical backups. Existing MET
    origin metadata in each variant remains historical; verify via CLI output."""
    return variant_call("update_project_sources", project, site_name, met_name, expected_files)

@categorized_tool()
def pvsyst_restore_project_sources(project: str, backups: dict[str, str],
                                   expected_files: dict[str, str], confirm: bool = False) -> dict:
    """Restore every PRJ/VC file from one source/name project transaction, after
    matching current hashes, a complete transaction manifest and backup hashes.
    Requires confirm=true and backs up current bytes; legacy sets need manual recovery."""
    return variant_call("restore_project_sources", project, backups, expected_files, confirm)

@categorized_tool()
def pvsyst_get_variant_components(project: str, variant: str) -> dict:
    """Read component references in a variant and its current SHA-256 for guarded edits."""
    return variant_call("inspect", project, variant)

@categorized_tool()
def pvsyst_clone_variant(project: str, source_variant: str, new_variant: str,
                         updates: dict[str, str] | None = None, subarray_id: StrictInt = 1,
                         source_project: str | None = None) -> dict:
    """Copy a variant as a NEW VC in project. source_project defaults to project;
    supply another workspace PRJ to import its variant under the target prefix.
    Embedded site/meteo and external references are preserved, not synchronized
    with the destination PRJ; inspect_project reports source differences.
    Optional PAN/OND edits target one SubArrayId; BTR/GEN target pvSystem only.
    subarray_id requires a strict JSON integer, not a boolean, string or float."""
    return variant_call("clone", project, source_variant, new_variant, updates, subarray_id, source_project)

@categorized_tool()
def pvsyst_update_variant_components(project: str, variant: str,
                                     updates: dict[str, str], expected_sha256: str,
                                     subarray_id: StrictInt = 1) -> dict:
    """Replace existing PAN/OND/BTR/GEN references only, with backup and SHA guard.
    Use pvsyst_get_variant_components to obtain the current expected_sha256.
    subarray_id requires a strict JSON integer, not a boolean, string or float."""
    return variant_call("update", project, variant, updates, expected_sha256, subarray_id)

@categorized_tool()
def pvsyst_validate_variant_structure(project: str, variant: str) -> dict:
    """Check orientation IDs/references and counted TOrientGroup lists (including
    planes without an assigned subarray), grid circuit
    references and serialized inverter/string counts. Dome/unknown list quantities
    are reported as unchecked. grid_circuit lists grouped branches and counts;
    multiple inverter groups can belong to one subarray. Explicit MPPT-node counts
    do not establish independent-MPPT simulation mode. Verified Version=8.1.0/8.1.6
    subarrays with $0400 compare explicit inputs against NInvMPPT directly;
    diagnostics leave this comparison unset for other modes/implicit layouts.
    Bounded shared parents return ordered subarray_ids and power_sharing_id;
    member shared_inverter_lines mark overlapping references, and totals count
    each parent once. Requires explicit owned MPPT/string children, matching
    Version=8.1.0/8.1.6 independent members with matching declared versions and
    inverter references, and common group 0 or all -1.
    Other shared/unknown allocation reports incomplete checks.
    valid is false for issues, null for incomplete checks,
    true when scoped checks pass. This is not voltage/current or physical validation."""
    return variant_call("validate_structure", project, variant)

@categorized_tool()
def pvsyst_clone_subarray(project: str, variant: str, source_subarray_id: StrictInt,
                          expected_sha256: str, clone_orientation: StrictBool = False,
                          share_inverter: StrictBool = False) -> dict:
    """Duplicate a grid subarray and all its exclusively owned inverter groups.
    source_subarray_id requires a strict JSON integer, not a boolean, string or float.
    By default keep the shared fixed-plane orientation and serialized MPPT layout.
    Complete exclusive MPPT branches are preserved, not switched to independent
    mode. Empty shading Flags=$00/$00020000/$00080000 still require geometry/table
    exclusion. clone_orientation=true appends an independent fixed plane and updates its
    NoOrientation reference/list count: requires complete grid counts, all strings
    in one orientation, a flat Flags=$00 non-averaged source, and sequential
    ordered TOrientGroup IDs. Supports ordinary branches without explicit MPPT,
    or verified Version=8.1.0/8.1.6 independent $0400 layouts with disabled sharing -1,
    exclusive direct MPPT children and complete declared input counts. It preserves
    mode, input/leaf multiplicities and copied loss/model fields; no MPPT pairing,
    shared parent construction or electrical fitting. Returns orientation_id
    and orientation_cloned. Independent plane copying rejects circuit-level
    orientation references instead of retaining stale allocation.
    Multiple groups require fully checked counts. Shared power, external target-ID
    references and shading geometry/tables are not supported. Requires the current
    variant hash; saves a rollback snapshot. share_inverter=true instead copies
    the one owned MPPT subtree under the same inverter, creating a bounded
    shared parent with aggregate name and owner 0. Requires Version=8.1.0/8.1.6,
    independent $0400, explicit disabled sharing -1, complete counts, one
    inverter/input (multiplicity 1), and all strings in the existing plane.
    New member keeps disabled sharing and its copied parameters; topology
    grouping is separate from enabling power sharing and hardware sizing.
    Do not combine with clone_orientation. Returns shared_parent_created."""
    return variant_call("clone_subarray", project, variant, source_subarray_id, expected_sha256, clone_orientation, share_inverter)

@categorized_tool()
def pvsyst_remove_subarray(project: str, variant: str, subarray_id: StrictInt,
                           expected_sha256: str, remove_orientation: StrictBool = False,
                           shared_member: StrictBool = False) -> dict:
    """Remove a grid subarray and all its exclusively owned inverter groups.
    subarray_id requires a strict JSON integer, not a boolean, string or float.
    By default another subarray must retain the orientation. remove_orientation=true
    also removes its now-unused fixed plane in a sequential TOrientGroup list,
    leaving another orientation. Non-final removal renumbers higher plane IDs and
    all owning NoOrientation references; requires all planes flat Flags=$00 and
    non-averaged, single-orientation subarrays with completely owned branches,
    ordinary NInvMPPT=1 without explicit MPPT or verified Version=8.1.0/8.1.6 independent
    $0400/direct-MPPT allocations with disabled sharing -1 and consistent counts.
    Every orientation reference must be an owned plane/root field; external or
    circuit-level references stop before writes. Returns removed_orientation_id (null by
    default) and orientation_id_map for changed surviving IDs. This opt-in
    and multiple groups require complete consistent grid counts. Shared power
    and shading geometry/tables are not supported. Requires the current hash
    and creates a rollback snapshot. shared_member=true instead deletes one
    member/owned MPPT subtree from a complete disabled two-member parent with
    explicit owner 0, one inverter/input per member and a common unshaded fixed
    plane. Preserves other member fields, restores its exclusive parent owner/name
    and reports remaining_member_id. All power sharing must be disabled; do not
    combine with remove_orientation. This is a bounded construction inverse,
    not general merge/split, active-group reallocation or hardware refitting.
    An active bounded group uses an explicit prior power_sharing_enabled=false
    parameter transaction; direct active deletion still stops. Restore deletion
    then the toggle snapshot to recover the active parent. These are separate
    guarded transactions, not an atomic implicit-disable operation."""
    return variant_call("remove_subarray", project, variant, subarray_id, expected_sha256, remove_orientation, shared_member)

@categorized_tool()
def pvsyst_get_variant_parameters(project: str, variant: str) -> dict:
    """Inspect orientations, including native SummerTilt/WinterTilt (degrees),
    WinterMonths (raw month bitmask) and UsesTiltAver. Seasonal FieldTilt=0 is not
    the effective seasonal angle; these fields are read-only here, not a mode editor.
    Inspect owning subarray LIDLoss/PMismMPP percent, raw PQualityModU/I/P and
    Flags, variant_name from the header only (null if absent/duplicate there;
    late-root name ambiguity is not validated), utf8_bom (raw UTF-8 names do
    not prove BOM-less native decoding), main_array UFactor (W/m2/K)/Flags/Flags2,
    main_array.soiling, native grid_limit, auxiliaries, horizon, inverter_temperature
    and power_factor state, power_controls (verified PF/grid combinations),
    p50_p90 (existing native result-template parameters), and standalone
    generator settings/thresholds. Unsupported system forms report an issue;
    system_parameters exposes unique owning SystemType/Flags/TempShift/TempFixed/
    TanPhi/PLimGrid/LimGrSeparLoss as raw text; missing or duplicate fields are omitted.
    Native temperature fields use degC; TanPhi is not a computed PF, and PLimGrid
    units depend on the native mode (kW or kVA). Nested owning object fields are
    excluded; this does not widen write support.
    Read-only transformers, system_unavailability, peak_shaving and shading_scene expose
    stored native layers, outage periods, strategy and scene declarations. Active
    shading_scene.pv_bindings additionally exposes one or more native8.1.6 PVCollada
    PV-table declarations for one or more ordinary fixed source planes: raw automatic
    labels/creation counters, stored orientation IDs, module
    counts and native DOrigine metres (NOT house/PVCollada coordinates). Cross-checks
    ordinary fixed-plane references, three angle fields and per-plane saved module totals.
    stored_consistent means these declarations agree, not source geometry/identity,
    circuit topology or native consumption verified. Unsupported/malformed records retain
    the scene's active declaration and report an issue, never an empty/verified scene.
    Generic meshes are not decoded. These are not computed electrical quantities or
    dispatch. Ageing reports
    its stored template fields; IAM includes native FresnelSmooth/UserProfile point tables
    without evaluating them. A broader readable P50 root state does not widen writing."""
    return variant_call("inspect_parameters", project, variant)

@categorized_tool()
def pvsyst_review_project_inputs(project: StrictStr, variant: StrictStr,
                                  preparation_manifest_name: StrictStr) -> dict:
    """Compare producer-declared source inputs with saved project/variant fields, read-only.
    preparation_manifest_name is a completed Results/<source>_prepare_<year>.json from
    pvsyst_prepare_design_project, optionally carrying cited-source provenance. It is a
    selected declaration baseline, not authenticated engineering evidence or a job resume.
    Reads existing project/parameter/reference APIs; one Grid subarray and assigned fixed
    non-seasonal orientation only. Compare PAN/OND and project/variant SIT/MET filenames,
    tilt/azimuth at existing editor three-decimal serialization, accepted-plan string/
    inverter counts, and explicitly requested loss scalar fields. ModuleQualityLoss is
    compared to owning PQualityModU/I exactly as the existing editor writes it.
    Only stored values are compared, not actual runtime effectiveness. Raw default/manual
    flags are disclosed but not interpreted here, so requested losses retain needs_review.
    Electrical envelope temperatures/coefficients/current limits and project saved design
    temperatures are shown separately, not equated by name/unit; year is an execution
    option, target capacity is a goal rather than an exact field. Source review remains.
    status=reviewed means no difference in compared saved fields, not design certification;
    needs_review includes differences, unassessed requested-loss modes or source concerns;
    blocked means incomplete/ambiguous/out-of-scope baseline or target. All calls report
    readonly=true/native_consumption_verified=false/runtime_state_observed=false.
    Never writes records/files, activates GUI, repairs/invalidates geometry, queries a
    licence, runs annual simulation or reads arbitrary baseline-supplied filesystem paths.
    Existing production input snapshots reject mixed-state reads. Unsaved user work and
    native effective solar/weather/physics are not inspected or approved."""
    from pvsyst_auto.engineering.review import review_project_inputs
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return review_project_inputs(store, project=project, variant=variant,
                                 preparation_manifest_name=preparation_manifest_name)

@categorized_tool()
def pvsyst_update_variant_parameters(project: str, variant: str,
                                     expected_sha256: str, subarray_id: StrictInt = 1,
                                     orientation_id: StrictInt = 1,
                                     subarray_updates: dict[str, StrictFloat] | None = None,
                                     orientation_updates: dict[str, StrictFloat] | None = None,
                                     generator_updates: GeneratorUpdates | None = None,
                                     dry_run: StrictBool = False,
                                     power_sharing_enabled: StrictBool | None = None,
                                     independent_mppt_enabled: StrictBool | None = None,
                                     main_array_updates: dict[str, StrictFloat] | None = None,
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
    """Edit existing validated parameters or display name with SHA guard and rollback backup.
    variant_name changes only one owning header Comment, not the VC identifier,
    filename, project or nested names. Requires printable single-line text of
    1..80 UTF-8 bytes, no surrounding whitespace; bool/numeric values stop at SDK.
    Missing/duplicate/late root names stop rather than inventing a field. Returns
    name_change before/after. Existing preview/no-op/restore apply; display-only
    names need no shading remapping and may accompany ordinary scalar updates.
    Mode/sharing toggles stay separate. This is not file/variant-ID renaming.
    Non-ASCII names require an existing UTF-8 BOM or explicit add_utf8_bom=true;
    that option requires variant_name and changes decoding of the entire VC.
    bom_added is returned for name transactions, including a marker-only change
    with unchanged name/text diff. Existing BOM/newline style and formal recovery
    are retained; no implicit marker insertion or ANSI transcoding. Strict JSON
    booleans only, not strings/numbers. Raw inspected names do not prove correct
    native decoding of a BOM-less file. Ordinary non-name edits preserve encoding.
    dry_run returns a candidate hash/diff without writing. Subarray/orientation/
    main-array dictionaries require strict numbers, not booleans or numeric strings.
    main_array_updates supports existing owning UFactor (constant Uc, W/m2/K)
    in adapter range [15,100]. Requires one Grid main array with Flags=$00100400,
    Flags2=$082A, all subarrays declared 8.1.0/8.1.6, no active geometry/layout.
    This changes the whole main array, not the selected subarray. Flags, wind
    terms, absorptance and efficiency fields are preserved; no fitting, inferred
    defaults or transient/wind model edits. Low-value behavior is unverified.
    Target subarray_id/orientation_id require strict JSON integers, not booleans,
    strings or floats; numeric dictionary values retain their separate rules.
    subarray_updates
    also supports existing root LIDLoss in percent [0,100], text 8.1.0/8.1.6
    Grid subarrays without active geometry/layout. It does not infer module
    technology, change flags, recompute defaults or fit losses. Existing root
    PMismMPP in percent [0,100] is supported for declared 8.1.0/8.1.6 Grid subarrays
    without active geometry/layout and with one hexadecimal owning Flags.
    Setting PMismMPP also clears only default-mismatch bit 0x00040000 so the
    saved value is consumed; other flags/losses stay intact. Returns mismatch
    default_before/default_after. Restore the snapshot to recover default mode;
    this does not fit losses or recalculate the native default.
    ModuleQualityLoss is a semantic subarray_updates key in adapter percent range
    [-5,20], negative means gain. Requires verified declared 8.1.0/8.1.6 Grid,
    no active geometry/layout, unique existing owning PQualityModU/I and hexadecimal
    Flags. It synchronizes both U/I saved values and clears only quality default
    bit 0x00100000; PQualityModP, other flags/losses/fields remain unchanged.
    Raw U/I/P are read-only inspection fields, not independent quality setters.
    Returns module_quality default_before/default_after; restore recovers default
    mode. No native-default calculation, tolerance inference or model fitting;
    this bounded paired mapping is verified on an unshaded generic mono grid case.
    PNomAC_InMPPT is a separate subarray_updates key: the stored per-MPPT nominal AC
    power in kW, written only when that subarray already declares the field. It must
    be a positive finite number (zero, negative, NaN and infinity stop before any
    write); there is no guessed hardware ceiling and no derivation from the OND
    PNomConv or NbMPPT. This edits the existing declaration only: it does not add
    fields, resize MPPT inputs or allocate strings, and it is not an inverter power
    calculation.
    generator_updates
    supports text 8.1.0/8.1.6 SystemType=Battery: enabled (only system Flags bit 0x20),
    filename (existing GEN), operating_power_kw (>0), and thresholds mapping
    string subarray IDs to VBkUpEncl_syst/VBkUpDecl_syst values in [0,1].
    These are native normalized voltage thresholds, not SOC. Missing generator
    fields can be added. Enabling requires a valid GEN, positive power and both
    thresholds in every subarray. Disabling preserves its saved configuration.
    Nominal power/fuel consumption remain component fields. NModSerie edits
    integer series-module count directly, preserving parallel strings, input
    grouping and mode. Native model derives module count/operating voltage;
    hardware voltage/current sizing, shading remapping and loss fitting are
    not performed. Series edits, angle edits and structural/count/mode edits
    stop before writes for active scenes/tables or any module-layout block;
    cached module/string mapping is not remapped. Angle edits additionally
    require a flat non-averaged FixedPlane, explicit Flags=$00 if present, and
    absent/unique False UsesTiltAver/UsesAzimAver/UsesSlopeAver. Legacy angle
    scalars may omit Flags; topology edits still require explicit Flags=$00. subarray_updates NInverter (integer 1..10000) scales ordinary grid inverter groups; NStringCh
    (positive 32-bit integer) proportionally scales parallel StringNode counts.
    They may be combined. NInverter alone preserves strings per inverter;
    NStringCh alone preserves inverter count. All group/leaf allocations must
    remain integral; declarations and NElements are synchronized together.
    Requires consistent unshaded fixed-plane counts,
    NInvMPPT=1 without the independent $0400 subarray flag, no shared power and
    no explicit MPPT branches. NInvMPPT (integer 1..10000) separately scales
    existing explicit MPPT groups for verified Version=8.1.0/8.1.6 independent $0400
    subarrays with PowerSharingInverterId=-1 and complete exclusive allocation.
    It preserves inverter/leaf counts and mode, synchronizes total input/string
    declarations, and requires integral groups and explicit edited NElements.
    NInverter alone also scales verified Version=8.1.0/8.1.6 independent exclusive inverter groups,
    synchronizing NInverter, total NInvMPPT and strings while preserving MPPT/leaf
    multiplicities and mode. Changed inverter NElements must be explicit; all
    resulting group/input/string counts must be integral. Do not combine this
    independent-mode operation with NInvMPPT/NStringCh; shared parents stay closed.
    NStringCh alone also scales explicit parallel leaves in verified
    Version=8.1.0/8.1.6 unshared independent mode while preserving MPPT/inverter
    multiplicities; integral explicit leaf counts are required. NStringCh alone also supports a member
    of one bounded Version=8.1.0/8.1.6 shared parent (matching member versions,
    common fixed plane, complete allocation, group 0/-1); only its owned
    leaves/root strings change, not peer members,
    inputs, sharing or nominal tags. Group 0 must have no outside references.
    Returns capacity.shared_parent_line; native automatic budgets may change.
    Do not combine NInvMPPT with
    NInverter/NStringCh/NoOrientation. No hardware limits
    or loss/nominal-power refitting is performed. Other system bits
    and field type are preserved. NoOrientation (positive integer) reassigns the
    selected subarray to an existing ordinary fixed plane: sequential counted
    TOrientGroup IDs, flat Flags=$00 non-averaged source/target, fully checked
    exclusive ordinary branches without explicit MPPT, or verified Version=8.1.0/8.1.6
    independent $0400/direct-MPPT branches with disabled sharing -1 and complete
    declared input counts, and all strings in one orientation are required.
    Circuit-level orientation references remain unsupported.
    Every defined plane must retain an assigned subarray. Leaves planes/list
    counts and circuit quantities unchanged; no automatic pruning or shading
    remap. Returns orientation_assignment before/after.
    power_sharing_enabled separately toggles all members of the existing
    bounded shared parent containing subarray_id. Requires complete counts,
    Version=8.1.0/8.1.6 independent $0400 members with matching declared versions,
    the same inverter and fixed
    plane, all member strings in that plane, and exclusive use of group 0.
    Synchronizes member PowerSharingInverterId=0/-1 and only Flags bit $0100;
    preserves topology, quantities, mode, nominal tags and loss parameters.
    No topology creation, arbitrary group IDs or manual allocation. Do not
    combine with other parameter updates. Returns power_sharing before/after.
    independent_mppt_enabled separately switches verified Version=8.1.0/8.1.6
    exclusive explicit MPPT layouts with PowerSharingInverterId=-1, complete
    unshaded fixed-plane counts and all strings in one orientation. It changes
    only $0400 and NInvMPPT (explicit input total when enabled, 1 when disabled).
    Requires all owned inverter children explicit MPPT; preserves circuit and
    other flags/counts, components/nominal/loss tags. Model power interpretation
    changes; hardware allocations are not redesigned. Returns mppt_mode.
    soiling_updates configures the native whole-array Soiling block in a separate
    transaction (variant_name may accompany it). mode=none removes that block;
    constant requires yearly_loss_pct; monthly requires twelve monthly_loss_pct.
    Percent values are 0..100 in 0.1 percentage-point increments, matching GUI save
    precision. Verified scope: 8.1.6 PV-only Grid, one unshaded fixed plane/subarray,
    main Flags=$00100400/$01900400, Flags2=$082A. Flags and unrelated fields stay
    unchanged; no irradiance/yield calculation, legacy or file-based soiling edits.
    grid_limit_updates is a separate transaction (variant_name may accompany it).
    mode=constant requires positive limit_kw in 0.1 kW increments: native active
    power at the injection point, with separate grid-curtailment loss. mode=none
    clears the active-limit bit and removes its scalars, preserving the inactive
    location bit. Verified 8.1.6 PV-only single-subarray/fixed-plane/unshaded scope;
    PF, loads/storage, file-based limits and other system modes stop before writes.
    This is not bit27 export permission, kVA, storage peak shaving or external
    output clipping. Original native calculations and existing rollback apply.
    auxiliary_updates is separate (variant_name may accompany it). constant requires
    explicit operating_kw and night_kw >=0 in 0.01 kW increments for the whole system;
    native operating power applies above the verified zero inverter-output threshold,
    not a clock schedule. none clears native bit18 and removes auxiliary powers.
    Verified 8.1.6 PV-only fixed/unshaded scope; PF, grid limits, loads/storage,
    proportional/threshold fields stop before writes. Native negative E_Grid is
    retained, not clipped; no external auxiliary load or energy calculation.
    seasonal_updates is separate and configures native SeasonalTilt from a verified
    FixedPlane or SeasonalTilt: explicit summer_tilt/winter_tilt in 0..90 degrees,
    0.1-degree steps, and 1..11 distinct winter_months integers in 1..12.
    Optional orientation_name changes the owning plane label; azimuth is preserved.
    Require one 8.1.6 PV-only Grid subarray/plane, complete circuit allocation,
    no geometry/layout, free horizon and plain system Flags. Averaged planes,
    trackers, loads/storage and coupled system modes stop before writes.
    Zero fixed BaseSlope is removed as native GUI does. This is not a fixed-mode
    switch; restore the snapshot to recover the source. No angle or season model
    is implemented outside PVsyst. horizon_updates is separate: none removes the
    native far-horizon block; profile requires profile_name and 4..100 increasing
    [azimuth, elevation] points in native degrees, 0.1-degree steps, x=-180..180,
    y=0..90. All-zero profiles use none. Fixed-plane plain PV-only Grid scope;
    no trackers, seasonal modes, loads/storage, PF/aux/grid-limit coupling or
    near-shading/layout edits. Native FracAlbedo=1 stays inherited. New or changed
    points use the GUI profile template without stale derived factors; name-only
    updates preserve them. PVsyst computes interpolation, diffuse and losses.
    inverter_temperature_updates is separate and requires an ordinary single free-horizon
    PV-only Grid plane, with no load/storage/PF/aux/grid-limit coupling or geometry.
    mode ambient removes both native temperature fields; offset/fixed require both
    offset_c and fixed_c explicitly, including the inactive saved value, with no default.
    Both values are degC in adapter range [-100,100], 0.1-degree steps: this is an
    adapter support envelope, not a native GUI or manufacturer limit. Only system
    temperature mode bits and TempShift/TempFixed change; module Uc and OND curves
    stay unchanged. PVsyst alone calculates inverter temperature and derating.
    power_factor_updates is separate (variant_name may accompany it), for native
    8.1.6 PV-only single fixed-plane/free-horizon Grid systems without geometry,
    loads/storage, temperature, auxiliary or grid-limit coupling. mode none removes
    PF fields; constant requires tan_phi, monthly requires twelve monthly_tan_phi
    in January-to-December order. Require explicit pnom_limit native/active/apparent;
    only none requires native; constant/monthly accept all three. Values are signed dimensionless
    native tangent values in adapter range [-1,1], 0.001 steps, not cosine values
    or manufacturer limits. GUI leading uses negative, lagging positive; native
    PVsyst alone computes power and derating. Legacy active/apparent select inverter
    PNom interpretation, not injection limits, and do not change OND hardware data.
    p50_p90_updates edits an existing GUI-produced P50 result template, separately
    from physical changes (variant_name may accompany it). Supply all fourteen native
    keys: SpecYear/TMY kind, two ordered integer percentiles 51..99, report booleans,
    PlotKind=1, explicit year deviation and six variability percentages, and a custom
    ASCII label. YearDevFromAver is percent in [-100,100], variability fields percent
    in [0,100], all using 0.001 steps; these are adapter domains, not scientific bounds.
    CustomVarLabel requires 1..80 printable trimmed ASCII bytes. Require native 8.1.6
    plain PV-only single fixed/free-horizon Grid, root Flags=$01800001 and one existing
    flat P50_P90=TP50_P90 block; no creation/deletion, unknown ClimateChange, cumulative
    plot or coupled physical changes. Native root flags and cached results stay unchanged.
    Unchecked GUI boxes retain manual values; checked boxes restore native defaults,
    not include/exclude terms. Native PVsyst alone computes probabilities and reports.
    power_controls_updates is a separate combined PF/grid transaction (variant_name
    may accompany it); old power_factor_updates/grid_limit_updates stay separate.
    Supply power_factor using its existing shape and grid_limit with explicit mode,
    quantity active/apparent and point injection/inverter. Constant also requires
    positive limit in 0.1 native units (kW active, kVA apparent), separate_loss bool;
    none supplies neither. No conversion from tangent or independent power model.
    Only observed native flag families are accepted: monthly/Legacy with an enabled
    limit require injection/separate loss. Monthly plus Legacy with an enabled limit
    supports active quantity only. An enabled inverter-point limit requires
    constant/native PNom/separate loss; combined loss requires constant/native
    PNom/injection. Enabled NoPF requires active injection/separate loss.
    Monthly PNom without a grid limit uses explicit active quantity/inverter point.
    No temperature, P50, storage/load or geometry coupling. Inactive type/location
    bits are explicit, not silently reset.
    The shared 8.1.6 PV-only single fixed/free-horizon/count gates remain in force.
    string_mismatch_updates is separate (variant_name may accompany it), for native
    8.1.6 PV-only single fixed-plane/free-horizon Grid subarrays: mode default clears
    the manual value and sets the native default bit, constant writes one manual
    loss_percent (adapter envelope 0.01..100, 0.01 steps; zero is mode default).
    auxiliary_updates profile has two verified families -- constant power with its
    threshold, or proportional W/kW with its threshold -- and requires all five
    values explicitly with night_kw = 0; the old constant/none shapes are unchanged.
    ageing_updates is separate (variant_name may accompany it) and applies only to an
    existing activated ageing template: it writes simulation_year and
    annual_module_degradation_percent, optional keep_mc_values and a paired
    modules_rms_year_isc_percent/modules_rms_year_voc_percent (adapter 0..100 percent, 0.01 steps). Stored factors
    and warranty lists are preserved; no degradation or stochastic sampling is computed.
    The retention flag is a native input; sample-array persistence is not verified.
    ageing_enabled is a separate strict boolean transaction for an existing fully
    validated template, toggling only main-array Flags2 bit20; it never creates a block
    or activates an unsupported template. Disabled templates remain readable but must
    be enabled before model editing. Zero rate is a value, not the disabled state.
    iam_updates is separate too and covers only the component IAM bit or
    the ASHRAE model with an optional b0; PAN files and curve data are never written.
    system_unavailability_updates={"mode":"none"} separately removes one existing native
    Periods SystemUnavail child and its owning system tag. It is limited to the verified
    8.1.6 PV-only single fixed/free-horizon template, with no P50, loads or storage.
    It does not generate periods or edit grid availability. Restore the returned transaction
    backup to recover the original native schedule; clearing a flag alone is not sufficient.
    No-op edits create no backup."""
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
def pvsyst_restore_variant(project: str, variant: str, backup_name: str,
                           expected_sha256: str, confirm: bool = False) -> dict:
    """Restore a variant snapshot after verifying its current hash; backs up the
    pre-restore state and requires confirm=true."""
    return variant_call("restore", project, variant, backup_name, expected_sha256, confirm)

@categorized_tool()
def pvsyst_create_tracking_variant(project: StrictStr, source_variant: StrictStr,
                                   new_variant: StrictStr, tracking: TrackingControls,
                                   dry_run: StrictBool = True,
                                   variant_name: StrictStr | None = None,
                                   add_utf8_bom: StrictBool = False) -> dict:
    """Preview or create a NEW variant editing the source native tracking record only.
    Supported 8.1.6 FieldTypes are UnlimTracking, TrackHorizNSAxis, TrackTiltedAxis,
    TrackVertAxis, TrackSunShield, TrackHorizAxis, TrackTwoAxis, TrackFrameNS and TrackFrameEW.
    No conversion: each source must already have its exact recorded field shape, one
    counted orientation1, one PV-only Grid subarray and complete consistent circuit counts.
    Root/system/main/subarray flags and geometry/layout/load/storage/P50/horizon gates
    remain in force. Only controls that belong to that FieldType are accepted.
    Supply one or more controls; unspecified values stay inherited. Existing five-control
    Unlim calls remain valid. All present min/max pairs use source values for the side
    not changed, and both source and candidate must keep their order. AxisAzim/MinPhi/MaxPhi
    retain adapter domains -20..20/-60..-45/45..60. Other numeric controls have finite 0.1
    adapter precision, not measured GUI bounds. Lengths are meters, angles degrees and
    WindStowSpeed is meters/second. Booleans are strict.
    electrical_effect is writable only in an existing UnlimTracking record owning that
    field. Unlim geometry, BTNoTrackers, averaging and every other unrequested field stay
    inherited; no tracking, backtracking, irradiance or shading model is computed.
    Default preview creates nothing; execution exclusively creates one NEW VC, preserves
    the source, and reuses lock/path/snapshot/candidate readback. Optional variant_name and
    add_utf8_bom follow existing display-name rules. Public getter exposes the stored
    controls for read/edit closure. Native consumption is never inferred from saving:
    native_consumption_verified stays false until a separate native run is reviewed."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_tracking_variant(store, project=project, source_variant=source_variant,
                                   new_variant=new_variant, tracking=tracking.model_dump(exclude_none=True),
                                   dry_run=dry_run, variant_name=variant_name,
                                   add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_ac_cabling_variant(project: StrictStr, source_variant: StrictStr,
                                     new_variant: StrictStr, ac: ACCablingUpdates,
                                     dry_run: StrictBool = True, variant_name: StrictStr | None = None,
                                     add_utf8_bom: StrictBool = False) -> dict:
    """Preview or create a NEW VC with native uniform per-inverter AC cables.
    Only recorded8.1.6 PV-only single fixed plane/subarray, complete counts and native
    main/circuit/system flag families; no MV/HV/load/storage/P50/horizon/active geometry.
    Source and actual candidate validate all coupled main, subarray and inverter cable
    owners. Custom resistance, mixed/partial/inconsistent source cables are blocked.
    length_m is positive, <=100000 and exactly0.1m representable, not rounded; adapter
    bounds are not measured GUI or engineering limits. Active sources inherit omitted
    metal/section; inactive sources use the recorded Copper/ISO60228_10 template values.
    PVsyst computes electrical quantities from native CableType. StringNode DC cables,
    hardware and every unrequested record remain unchanged; no resistance/loss formula.
    Getter ac_cabling exposes stored state, represented groups and physical inverter
    count for read/re-edit. Preview writes nothing; execution exclusively creates one
    new VC with existing snapshot/path/name/BOM guards. Saving is not native consumption."""
    from pvsyst_auto.energy.ac import create_ac_cabling_variant
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_ac_cabling_variant(store, project=project, source_variant=source_variant,
        new_variant=new_variant, ac=ac.model_dump(exclude_none=True), dry_run=dry_run,
        variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_native_dc_cabling_variant(project: StrictStr, source_variant: StrictStr,
                                            dc: NativeDCUpdates, variant_name: StrictStr,
                                            dry_run: StrictBool = True, confirm: StrictBool = False) -> dict:
    """Set native DC resistance(mOhm) or STC loss(%) through the ALREADY OPEN GUI.
    Chinese PVsyst8.1.6rev49278/DPI96 and recorded window metrics only; one ordinary
    fixed-plane/subarray PV-only Grid, complete counts, no AC/MV/HV/load/storage/
    geometry/horizon/P50. Both native DC mode flag families are explicit source gates;
    all other editors retain their existing domains. value is finite, nonnegative,
    exact0.01 precision, <=20 characters; percent<=100. These are adapter constraints,
    not manufacturer limits. PVsyst computes the coupled subarray and StringNode values.
    dry_run only reads source/GUI state and does NOT activate/select/type/click. Actual
    input requires dry_run=false AND confirm=true; leave the desktop alone. The operator
    must have the matching project open and DataPath equal to the MCP workspace. No
    application start/exit, INI edit, clipboard, external DC formula or private VC patch.
    Native SaveAs chooses the new VC id; variant_name is its display title. Saved candidate
    owners/precision and all non-DC physical records are verified, sources preserved;
    BOM/display comments may normalize. Failure can leave a new file/current modal for
    inspection, and its path is disclosed. Saving is NOT simulation consumption proof."""
    from pvsyst_auto.native.dc import create_native_dc_cabling_variant
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_native_dc_cabling_variant(store, cli(), project=project, source_variant=source_variant,
        dc=dc.model_dump(), variant_name=variant_name, dry_run=dry_run, confirm=confirm)

@categorized_tool()
def pvsyst_create_mv_night_disconnect_variant(project: StrictStr, source_variant: StrictStr,
                                              new_variant: StrictStr, night_disconnect: StrictBool,
                                              dry_run: StrictBool = True,
                                              variant_name: StrictStr | None = None,
                                              add_utf8_bom: StrictBool = False) -> dict:
    """Preview or exclusively create a NEW VC changing a recorded MV night switch.
    Source and candidate both require native8.1.6 PV-only single fixed plane/subarray,
    valid circuit counts and the recorded single-MV20kV profile. Native field ownership,
    MVNbTransfos=1 and NightDisconnect/main-array bit29 consistency are checked first.
    Only that bool and dependent bit change (plus optional name/BOM). Reference power,
    iron/copper losses, AC/DC cables and all unrelated inputs remain inherited; no
    transformer formula or loss conversion. HV/multiple transformers, custom loss tables,
    loads/storage/P50/horizon/active geometry and unknown flag families are blocked.
    Configuration never claims native consumption; verify separate native outputs.
    Preview writes nothing; execution retains source/snapshot/no-overwrite protections."""
    from pvsyst_auto.energy.transformer import create_mv_night_variant
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_mv_night_variant(store, project=project, source_variant=source_variant,
        new_variant=new_variant, night_disconnect=night_disconnect, dry_run=dry_run,
        variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_shed_geometry_variant(project: StrictStr, source_variant: StrictStr,
                                        new_variant: StrictStr, geometry: ShedGeometryUpdates,
                                        dry_run: StrictBool = True,
                                        variant_name: StrictStr | None = None,
                                        add_utf8_bom: StrictBool = False) -> dict:
    """Preview or exclusively create a NEW native Sheds/SunShields variant.
    Use an existing recorded type with one orientation/subarray and complete grid counts,
    native8.1.6 root/system/main/subarray flags, no load/storage/P50/horizon or active
    geometry/layout. Shared tracking-template scope gates apply; no type conversion.
    Declare one or more type-owned geometry fields. Angles use0.1 degrees (tilt0..90,
    azimuth-180..180); lengths/factor use0.001. Pitch/collector/cell width must be positive,
    inactive bands nonnegative and their sum below collector width, factor0..1;
    counts strict positive32-bit and booleans strict. These are adapter domains, not
    measured GUI or manufacturer limits. Source and candidate share exact validation,
    with no rounding of unsupported precision or invention of unknown native fields.
    Preserve all unrequested data, hardware, topology and source bytes; getter exposes
    stored fields for create/read/create closure. Native alone computes shading and
    electrical grouping. Saving never sets native_consumption_verified to true.
    Preview creates nothing. Optional name/BOM rules and snapshot/no-overwrite guards
    are inherited; failures disclose partial artifacts without automatic cleanup."""
    from pvsyst_auto.project.sheds import create_shed_variant
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_shed_variant(store, project=project, source_variant=source_variant,
        new_variant=new_variant, geometry=geometry.model_dump(exclude_none=True),
        dry_run=dry_run, variant_name=variant_name, add_utf8_bom=add_utf8_bom)

@categorized_tool()
def pvsyst_create_dome_allocation_variant(project: StrictStr, source_variant: StrictStr,
                                          new_variant: StrictStr,
                                          first_orientation_strings: StrictInt,
                                          dry_run: StrictBool = True,
                                          variant_name: StrictStr | None = None,
                                          add_utf8_bom: StrictBool = False) -> dict:
    """Preview or create a NEW variant that rebalances an existing two-face Dome allocation.
    Supported subset is the already-allocated native 8.1.6 Dome template: one subarray
    sharing both faces, NStringCh=40 with NoOrientation=1 and NoOrientation2=2, plus the
    pinned module/inverter/series/inverter/MPPT declarations and the two paired
    pvDomeOrient records (main FieldType=Dome at tilt 10 / azimuth 90, back face
    FieldType=DomeBackfaces at tilt 10 / azimuth -90, NoPairOrient 2/1, IsMainOrient
    True/False, three averaging switches False, BTNoTrackers=-1,-1, both Version=8.1.6
    and Flags=$00). Record Comments are kept verbatim and are never used to detect the
    face type; unknown, duplicated, missing or wrongly paired records stop.
    Only NStrOrient1 is written, plus an optional root Comment; no inverter, string,
    module, subarray or orientation record is created or copied, NoOrientation2 is not
    changed, and a source without an existing allocation is refused rather than completed.
    The remaining face-two strings are not written as NStrOrient2 because the native
    template does not store that field, so NStringCh=40 stays the only string total.
    first_orientation_strings is adapter-bounded to 1..39; that bound is not a measured
    native GUI or engineering limit, and the source record is validated against the same
    domain. Circuit node NElements values are inherited unchanged and their semantics are
    not inferred; no mismatch, string-current or allocation model is computed here.
    dry_run=true writes nothing; execution creates one NEW VC only and never edits the
    source PRJ/VC, weather, topology or hardware, reusing existing store locking, path
    checks, read-back verification and no-overwrite semantics. variant_name/add_utf8_bom
    follow the existing display-name and BOM rules. native_consumption_verified stays
    false: verify native hourly results and reports before treating the allocation as
    consumed."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_dome_allocation_variant(store, project=project, source_variant=source_variant,
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
                                 string_layout: StringLayoutBinding | None = None) -> dict:
    """Write a complete AI-authored house/PV scene in one call; prefer house.kind='mesh'.
    AI supplies all named architectural meshes (vertices in metres, explicit zero-based
    triangle indices): walls, multiple roof faces, eaves, chimney, window/door geometry,
    porch and other parts of the same house. The serializer does not reconstruct the
    house from flat/gable templates, invent dimensions, or require a rectangular footprint.
    Each name is unique ignoring case; total ceilings are 128 parts, 50000 vertices and
    100000 triangles (adapter ceilings, not PVsyst limits). No external assets or code run.
    For a mesh house use layout.origin_m=[x,y,z] for the PV table's low local corner and
    layout.roof_mesh for its named planar, upward-facing support surface. That surface's
    normal must match the source fixed PV plane. Checks cover four corner projections,
    not full solid topology, holes, panel intersections or architectural certification.
    Alternatively layout={tables:[{name,...placed-layout fields},...]} defines 1..64 named
    PV regions on the same source orientation, with independent rows/columns and origins.
    Their SUM of module_rows*module_columns must equal the source module total. All use
    the same source PAN. Table names must be unique ignoring case. Coplanar overlapping
    PV rectangles stop; no automatic packing or physical shading approximation is used.
    Candidate-only grouped authoring accepts two or more ordinary fixed-plane source
    subarrays with complete exclusive8.1.0/8.1.6 branches, the same PAN/OND and distinct
    positive source orientations. Use one or more named tables per source group, each
    table's strict orientation_id set to a real declared source orientation (not a fixed
    {1,2} whitelist). Grouping is complete: every source subarray must own at least one
    region, no omitted or invented group is allowed, each group's summed module_rows x
    module_columns must equal that source subarray's NModSerie x NStringCh, and every
    table's total must still equal the whole source module count. House vertices and every
    origin_m stay in the lowest source orientation id's local frame; only each PV node gets
    its source-relative standard Z rotation. Roof normal/corner checks use that frame.
    Grouped XY bounding footprints must be disjoint (adapter restriction, not a collision/
    engineering limit). Writes version2 metadata; adapter-authored grouped tables and
    native auto-import stay candidate-only until original-software consumption bridges the
    per-table identity and geometric verification.
    Structured diagnostics locate invalid parts, vertices, triangles or table entries
    with repair hints. Fix those inputs and preview again; authored geometry is not patched.
    module_rows/columns, orientation and row/column_spacing_m remain explicit. Legacy
    house={length_m,width_m,eave_height_m,roof} plus the original margin/clearance layout
    stays compatible; do not mix its layout with mesh mode. House parts share the source
    anchor azimuth rotation. The tool only serializes official COLLADA1.5/PVCollada2.0
    geometry and PV semantics; native project, variant and PAN bytes are never modified.
    Except for the explicit grouped candidate above, source requirements stay one Grid
    pvSystem/subarray/ordinary fixed plane; all modes exclude shading scene/module layout,
    load, storage, P50/P90
    or saved horizon, complete grid counts and a northern-hemisphere site record. Module
    width and height come from a readable loose PAN in metres when available; matching
    Commercial/root aliases must agree and only XML pv:module uses millimetres. For a
    built-in database reference without a readable loose PAN, provide module_geometry
    explicitly; its provenance is caller-provided, awaiting PVsyst's import consistency
    check. A square module is allowed. geometry_depth_m is explicit geometric metadata,
    not an inferred vendor specification. All provided metadata must agree with a readable
    source PAN; no PAN, project or variant is generated or rewritten.
    Total table modules must equal the source module count (NModSerie x NStringCh),
    so the scene cannot be drawn smaller than the array.
    Tilt and azimuth are inherited from the source orientation record and only disclosed in
    the response, so the caller cannot pick engineering angles. The COLLADA azimuth uses
    north = 0 clockwise, converted as (180 + FieldAzim) % 360; that mapping is only verified
    for a northern-hemisphere site, so a non-positive latitude stops.
    House/table translation uses the same local frame; only vertex, face and transform data
    are generated. No physical, irradiance, shading or optimizer model is computed.
    AI-authored parts and legacy roofs use the same serializer. PVC2 support is still beta, so
    creating a file does not prove that PVsyst imports it as a shading scene or consumes it.
    dry_run=true writes nothing and creates no directory. Execution writes only
    UserData/<output_name>.pvc2 with no-overwrite semantics and re-checks both the source
    variant and PAN bytes. native_consumption_verified and imported stay false.
    Optional string_layout is an explicit per-cell wiring block stored verbatim as a sibling
    of layout in the .pvc2 recipe. Drafts (any global_string=0 or a partial table) are accepted
    and written here; only native module_layout import requires a complete binding, and this
    tool never imports or consumes the scene."""
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return create_house_pv_scene(store, component_store, project=project, variant=variant,
                                 output_name=output_name, house=house.model_dump(),
                                 layout=layout.model_dump(exclude_none=True), dry_run=dry_run,
                                 module_geometry=(module_geometry.model_dump()
                                                  if module_geometry is not None else None),
                                 string_layout=(string_layout.model_dump(exclude_none=True)
                                                if string_layout is not None else None))

@categorized_tool()
def pvsyst_read_house_pv_scene(scene_name: StrictStr) -> dict:
    """Read a generated UserData/*.pvc2 back into complete house/PV create arguments.
    Parses actual XML mesh vertices/triangles and module dimensions, then rebuilds the
    supported dialect and compares canonical XML so unknown content is never dropped.
    Named tables, single placed tables and legacy parametric inputs emitted with the
    versioned standard asset metadata round-trip. Use returned create_arguments, change
    the intended parts/layout, supply a NEW output_name and preview/create with the
    existing pvsyst_create_house_pv_scene tool; the original file is never overwritten.
    required_inputs always includes output_name. Old R44 AI scenes without provenance
    return needs_context with project/variant/roof association missing, never guessed.
    Source declarations are file metadata, not proof the native project still matches;
    creation checks the current project/PAN again. No project or native scene is edited,
    imported/native_consumption_verified stay false, and no geometry is auto-repaired.
    Reads only bounded UTF-8 XML; DTD/entity declarations and unsupported extensions stop.
    This is not a general COLLADA converter or a native VC scene exporter."""
    return read_house_pv_scene(components(), scene_name)

@categorized_tool()
def pvsyst_open_native_project(project: StrictStr, dry_run: StrictBool = True,
                                 confirm: StrictBool = False) -> dict:
    """Open a workspace PRJ through the recorded Chinese8.1.6rev49278/DPI96 native GUI.
    Requires native INI DataPath already equal to this MCP workspace. Reads/validates
    the requested project and native process inventory, including instances without a
    visible form. Preview never starts/activates/selects/types; a cold launch requires
    dry_run=false AND confirm=true. Only a newly launched process receives native Grid/
    Load/filter/confirm actions; the final project basename and unchanged PRJ are checked.
    A preexisting matching ready project is returned already_open with owned=false and
    existing_session_read_only=true: no activation, variant selection, reload or close.
    Any other/ambiguous/busy existing session stops untouched, preserving unobservable
    unsaved work. This is not a native dirty-state detector or consent to edit an existing
    window. No INI, clipboard or VC patch; no application is closed/force-terminated.
    A failed cold launch discloses its owned PID/current stage for inspection. Successful
    opened state is not house import, geometry validity or simulation certification."""
    from pvsyst_auto.native.project import open_native_project
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return open_native_project(store, cli(), project=project, dry_run=dry_run, confirm=confirm)

@categorized_tool()
def pvsyst_import_house_pv_scene(scene_name: StrictStr, variant_name: StrictStr,
                                   dry_run: StrictBool = True, confirm: StrictBool = False,
                                   shading_mode: Literal["linear", "module_layout"] = "linear",
                                   method: Literal["gui", "native"] = "gui") -> dict:
    """Import a generated house/PV scene using the already open native PVsyst GUI.
    This bounded Windows path targets Chinese PVsyst 8.1.6 rev49278 with native
    logical DPI96 and the recorded physical window metrics, with
    the matching workspace and recipe project already open on its project page.
    Supports one or two PV regions; creation/standard export retains its 64-region
    limit. Unknown GUI metrics, dialogs, source/PAN mismatches and unsupported scene
    dialects stop rather than patching private VC data or guessing control positions.
    dry_run reads state only: no activation, key/mouse input or clipboard access.
    Execute with dry_run=false AND confirm=true, then leave the native desktop alone:
    selects the clean source variant, imports PVC2 and assigns its regions to the
    declared source orientation(s). The bounded two-orientation path explicitly places
    each table and verifies native SHD/saved VC geometry and a real GUI reload. It then
    runs PVsyst's scene-validity check and shading table, and saves a NEW native variant
    with the explicit display title variant_name (1..80 printable UTF-8 bytes).
    Native warnings beyond the expected beta notice stop; invalid geometry is returned
    as the software's own text. Any failure leaves the current window for inspection;
    an already-created variant path is disclosed even if postchecks fail. No automatic
    retry, source overwrite, application start/exit or INI modification is performed.
    Native validity text replaces the clipboard only after this confirmed operation;
    only the newly copied native text is read, never the earlier clipboard.
    imported/native_geometry_verified become true only after native persistence checks;
    native_consumption_verified stays false until a separate native simulation.
    shading_mode=module_layout additionally uses native placement and automatic string
    allocation, then selects detailed electrical shading. This opt-in requires two
    fixed source groups, one landscape zero-spacing table and one independent string
    (1..100 modules, one inverter/MPPT) per source array1/2. The saved module cells and
    native table/array links are checked; the selected mode is checked after GUI reload.
    Use pvsyst_run_simulation on the returned variant for energy/shading consumption.
    method="native" (opt-in) builds a NEW native input variant from the same supported
    recipe without any GUI, INI, clipboard, licence or engine action. It accepts two or more
    GROUPED homogeneous fixed orientations (same PAN/OND, distinct positive source
    orientations, one exclusive subarray each) on landscape zero-spacing tables carrying an
    explicit per-cell string_layout, and keeps the legacy single-orientation / one-and-two
    implicit shapes; unsupported recipes stop before writing. For the grouped N>=3 case this
    is build/verify-preview only: dry_run builds and verifies the native records in memory.
    Execution requires dry_run=false AND confirm=true and writes one new variant; the
    status is native_variant_created; stored_geometry_verified/stored_module_layout_verified
    reflect the STORED record only, while native_geometry_verified/native_module_layout_verified,
    native_consumption_verified and imported stay false because PVsyst has not loaded or consumed
    it; run an explicit simulation to verify consumption. dry_run writes nothing and keeps both
    stored flags false but still reports the passed precheck. The source variant/PAN are never modified.
    A scene recipe carrying an explicit string_layout is rejected here for method='gui' or
    shading_mode='linear' instead of silently ignoring the wiring; such a scene requires
    method='native' with shading_mode='module_layout', and the binding is read from the recipe
    (no extra tool argument)."""
    if not (method == "native" and shading_mode == "module_layout"):
        # A scene whose recipe carries an explicit binding must not be fed to the GUI/linear
        # importer, which would silently ignore per-cell wiring. Detect it on the recipe and
        # stop before any write; old scenes without a binding keep their exact legacy routing.
        recovered = read_house_pv_scene(components(), scene_name)
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
        component_store = components()
        store = VariantStore(component_store.workspace, component_store, component_store.lock)
        return import_house_pv_scene_native(store, component_store, scene_name, variant_name,
                                            dry_run=dry_run, confirm=confirm, shading_mode=shading_mode)
    if method != "gui":
        raise ValueError("method must be gui or native")
    from pvsyst_auto.native.scene import import_house_pv_scene
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return import_house_pv_scene(store, component_store, cli(), scene_name, variant_name,
                                 dry_run=dry_run, confirm=confirm, shading_mode=shading_mode)

@categorized_tool()
def pvsyst_prepare_design_project(spec: SingleDesignRequest, dry_run: StrictBool = True) -> dict:
    """Prepare one new design project without GUI or annual simulation, using existing flow gates.
    Same strict SingleDesignRequest/source/weather/fixed_unshaded_grid boundaries as
    pvsyst_simulate_design: exact PAN/OND and template, explicit counts or sourced design
    envelope, declared angles/losses and bound existing/csv/open_meteo weather. Missing
    design evidence stops with concrete issues; no manufacturer/site value is guessed.
    dry_run writes nothing. Future weather/seed inputs return ready_for_weather, never a
    falsely completed native preflight. Execution reuses guarded clone/source/component/
    capacity/parameter edits and structural readback, then stops BEFORE SFI/CSV/PDF/run.
    A zero remaining simulation quota does not itself block non-simulation preparation;
    required weather commands/version/licence are still queried, not bypassed.
    status=prepared or needs_review with prepared=true means configured files exist;
    simulated/native_consumption_verified stay false and metrics stays null. Review flags
    are preserved, including provider/weather review, never upgraded to energy acceptance.
    Results/<stem>_prepare_<year>.json retains declared request/envelope/selected sources,
    inherited configuration and existing guarded recovery transactions. Existing files
    are not overwritten, original template/component/weather assets remain protected.
    prepared_project gives exact project/variant/year/SIT/MET for a subsequent house task
    or explicit native run; report_pages is validated but no report is generated here.
    The old simulation entry still runs its annual path by default. No new sizing/PV model."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return prepare_design_project(current, store, spec=spec.model_dump(exclude_none=True), dry_run=dry_run)
