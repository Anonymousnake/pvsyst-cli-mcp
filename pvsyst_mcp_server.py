"""Workspace-scoped stdio MCP bridge for PVsystCLI 8.0/8.1 (SDK 2.x).

Set PVSYST_CLI and PVSYST_WORKSPACE in the client's MCP server environment.
Only supported CLI options discovered from the installed binary are used.
"""
from __future__ import annotations

import inspect
import math
import os
from threading import Lock
from functools import wraps
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr

from pvsyst_cli import (PVsystCLI, PVsystError, VAR_GROUPS, build_monthly_weather_csv,
                        build_sfi, iter_result_csv, parse_batch_results, result_units, summarize_results)
from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore
from pvsyst_paths import workspace_file
from pvsyst_results import compare_results
from pvsyst_simulation import simulate_template
from pvsyst_weather import inspect_weather_csv

mcp = MCPServer("pvsyst-cli")
_cli: PVsystCLI | None = None
_cli_init_lock = Lock()

TOOL_GROUPS = {
    "Setup & License": (
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_license_activate",
        "pvsyst_license_deactivate", "pvsyst_license_sync", "pvsyst_export_logs",
    ),
    "Projects & Variants": (
        "pvsyst_list_projects", "pvsyst_list_variants", "pvsyst_inspect_project",
        "pvsyst_create_project", "pvsyst_clone_project", "pvsyst_update_project_parameters", "pvsyst_update_project_sources",
        "pvsyst_restore_project_sources", "pvsyst_archive_project", "pvsyst_archive_variant",
        "pvsyst_restore_project_archive", "pvsyst_get_variant_components",
        "pvsyst_clone_variant", "pvsyst_update_variant_components",
        "pvsyst_get_variant_parameters", "pvsyst_update_variant_parameters",
        "pvsyst_validate_variant_structure", "pvsyst_clone_subarray",
        "pvsyst_remove_subarray", "pvsyst_restore_variant",
    ),
    "Components": (
        "pvsyst_list_components", "pvsyst_get_component", "pvsyst_validate_component",
        "pvsyst_create_component", "pvsyst_copy_component", "pvsyst_clone_component",
        "pvsyst_update_component", "pvsyst_backup_component", "pvsyst_restore_component",
        "pvsyst_archive_component", "pvsyst_restore_archived_component",
        "pvsyst_compare_components", "pvsyst_project_components",
    ),
    "Sites & Weather": (
        "pvsyst_build_monthly_weather", "pvsyst_create_site", "pvsyst_convert_meteo",
        "pvsyst_inspect_weather_csv",
    ),
    "Simulation": ("pvsyst_build_sfi", "pvsyst_run_simulation", "pvsyst_simulate_template"),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results"),
}
TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}
if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")


def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        @wraps(func)
        def guarded(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except (ValueError, FileNotFoundError, FileExistsError, PVsystError) as exc:
                raise ToolError(str(exc)) from None
        mcp.tool(description=f"[{category}] {inspect.getdoc(func)}")(guarded)
        return func
    return register


def cli() -> PVsystCLI:
    global _cli
    with _cli_init_lock:
        if _cli is None:
            _cli = PVsystCLI(os.environ.get("PVSYST_CLI"), os.environ.get("PVSYST_WORKSPACE"))
        return _cli


def components() -> ComponentStore:
    current = cli()
    configured = os.environ.get("PVSYST_BUILTIN_COMPONENTS")
    if configured:
        builtin = Path(configured).resolve(strict=True)
        if not builtin.is_dir() or builtin.name != "ComposPV":
            raise ValueError("PVSYST_BUILTIN_COMPONENTS must be a ComposPV directory")
    else:
        data_ro = current.cli.parent / "DataRO"
        candidates = (sorted(path / "ComposPV" for path in data_ro.iterdir()
                             if path.is_dir() and (path / "ComposPV").is_dir())
                      if data_ro.is_dir() else [])
        if len(candidates) > 1:
            raise ValueError("Multiple DataRO component libraries; set PVSYST_BUILTIN_COMPONENTS")
        builtin = candidates[0] if candidates else None
    return ComponentStore(current.workspace, builtin, current._lock)


def component_call(method: str, *args, **kwargs) -> dict:
    try:
        return getattr(components(), method)(*args, **kwargs)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise ToolError(str(exc)) from None


def variant_call(method: str, *args, **kwargs) -> dict:
    try:
        store = components()
        return getattr(VariantStore(store.workspace, store, store.lock), method)(*args, **kwargs)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise ToolError(str(exc)) from None


def workspace_path(folder: str, filename: str, extension: str) -> Path:
    """Allow a filename only within a named directory of the configured workspace."""
    return workspace_file(cli().workspace, folder, filename, extension)


def optional_path(folder: str, filename: str, extension: str) -> Path | None:
    return workspace_path(folder, filename, extension) if filename else None


def simulation_csv_path(filename: str) -> Path | None:
    """Accept a bare single-run output name while preserving workspace checks."""
    if not filename:
        return None
    if filename not in (".", "..") and not filename.endswith(".") and not Path(filename).suffix:
        filename += ".csv"
    return workspace_path("Results", filename, ".csv")


def result_path(folder: str, csv_name: str) -> Path:
    if folder not in ("Results", "UserHourly"):
        raise ValueError("Results folder must be Results or UserHourly")
    return workspace_path(folder, csv_name, ".csv")


@categorized_tool()
def pvsyst_capabilities() -> dict:
    """List commands and option names actually available in the installed CLI."""
    return {"version": cli().version(), "commands": cli().capabilities()}


@categorized_tool()
def pvsyst_license_info() -> dict:
    """Read CLI license state and quota without returning keys or Host ID."""
    return cli().license_info()


@categorized_tool()
def pvsyst_license_activate(key: str, confirm: bool = False) -> dict:
    """Activate a CLI license using a supplied key. Requires confirm=true;
    MCP clients may retain tool arguments, so handle license keys accordingly."""
    return cli().license_activate(key, confirm=confirm)


@categorized_tool()
def pvsyst_license_deactivate(customer_id: str, confirm: bool = False) -> dict:
    """Deactivate this computer's CLI license. Requires confirm=true."""
    return cli().license_deactivate(customer_id, confirm=confirm)


@categorized_tool()
def pvsyst_license_sync(confirm: bool = False) -> dict:
    """Synchronize CLI license information with its server. Requires confirm=true."""
    return cli().license_sync(confirm=confirm)


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
    """Inspect orientations, owning subarray scalars including LIDLoss/PMismMPP
    percent, raw PQualityModU/I/P and Flags, variant_name from the header only (null if absent/
    duplicate there; this does not validate late-root name ambiguity),
    utf8_bom for the VC (raw UTF-8 names do not prove BOM-less native decoding),
    main_array.values UFactor (W/m2/K)/Flags/Flags2,
    and standalone generator settings/thresholds. Nested owning object fields
    are excluded."""
    return variant_call("inspect_parameters", project, variant)


class GeneratorUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    filename: str | None = None
    operating_power_kw: StrictFloat | None = None
    thresholds: dict[str, dict[str, StrictFloat]] | None = None


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
                                     add_utf8_bom: StrictBool = False) -> dict:
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
    No-op edits create no backup."""
    return variant_call("update_parameters", project, variant, expected_sha256,
                        subarray_id, orientation_id, subarray_updates, orientation_updates,
                        generator_updates.model_dump(exclude_none=True) if generator_updates is not None else None,
                        dry_run, power_sharing_enabled, independent_mppt_enabled, main_array_updates, variant_name, add_utf8_bom)


@categorized_tool()
def pvsyst_restore_variant(project: str, variant: str, backup_name: str,
                           expected_sha256: str, confirm: bool = False) -> dict:
    """Restore a variant snapshot after verifying its current hash; backs up the
    pre-restore state and requires confirm=true."""
    return variant_call("restore", project, variant, backup_name, expected_sha256, confirm)


@categorized_tool()
def pvsyst_list_components(component_type: str, library: str = "workspace",
                           query: str = "", offset: int = 0, limit: int = 100) -> dict:
    """List PAN, OND, BTR or GEN loose files in workspace or installed DataRO.
    The encrypted built-in component database is not enumerable."""
    return component_call("list", component_type, library, query, offset, limit)


@categorized_tool()
def pvsyst_get_component(component_type: str, filename: str,
                         library: str = "workspace", offset: int = 0,
                         limit: int = 100, include_curve_csv: StrictBool = False) -> dict:
    """Read paged UTF-8 text, scalar allowlist, SHA and OND/BTR curve paths/
    active points, plus commercial field types/units and remarks. Curves report
    mode or CLI tag recognition, supported edits and their
    preconditions; static inspection does not verify simulation behavior.
    include_curve_csv adds a path-to-CSV-text map for OND main/BTR capacity
    curves with explicit-unit headers, plus curve_csv_errors for omissions.
    CSV export does not expand edit support. PAN scalar_aliases lists supported
    ISC/Isc and MuISC/muISC spellings. PAN/OND electrical exposes owning-object
    reference-point values/units and missing/ambiguous/invalid declarations;
    serialized temperature coefficients and per-MPPT limits remain unverified.
    IDCMax=0 is not a positive declared ceiling. Legacy PAN is metadata only."""
    return component_call("inspect", component_type, filename, library, offset, limit, include_curve_csv)


@categorized_tool()
def pvsyst_validate_component(component_type: str, filename: str,
                              library: str = "workspace") -> dict:
    """Check format and known fields; only a simulation verifies PVsyst model behavior."""
    return component_call("validate", component_type, filename, library)


@categorized_tool()
def pvsyst_copy_component(component_type: str, source_name: str, new_name: str,
                          source_library: str = "workspace") -> dict:
    """Copy a PAN/OND/BTR/GEN into the workspace without overwrite. Legacy PAN
    bytes are preserved. Source library is workspace or read-only builtin."""
    return component_call("copy", component_type, source_name, new_name, source_library)


@categorized_tool()
def pvsyst_create_component(component_type: str, filename: str, content: str) -> dict:
    """Create a NEW PAN/OND/BTR/GEN from complete UTF-8 PVObject_ text in the
    workspace without a template. Checks structure, required fields and known
    hazardous numeric values; only a referencing simulation verifies physics.
    Legacy binary PAN must be imported with pvsyst_copy_component instead."""
    return component_call("create", component_type, filename, content)


@categorized_tool()
def pvsyst_clone_component(component_type: str, source_name: str, new_name: str,
                           updates: dict[str, str], source_library: str = "workspace") -> dict:
    """Create a new text component from a complete existing template. New
    Manufacturer and Model are required; only verified scalar fields are editable.
    Run a simulation with a variant referencing the new component to verify it."""
    return component_call("clone", component_type, source_name, new_name, updates, source_library)


@categorized_tool()
def pvsyst_update_component(component_type: str, filename: str,
                           updates: dict[str, str],
                           expected_sha256: str | None = None, dry_run: bool = False,
                           curve_updates: dict[str, list[list[StrictFloat]]] | None = None,
                           use_file_curve: StrictBool = False,
                           commercial_updates: dict[str, str] | None = None,
                           remarks: list[str] | None = None,
                           curve_csv: dict[str, str] | None = None) -> dict:
    """Edit allowlisted text fields in one workspace component with automatic
    backup. dry_run returns a diff and candidate SHA without writes. Pass the
    inspected expected_sha256 to reject stale edits. PAN ISC/Isc and MuISC/muISC
    are aliases for root physical fields; edits retain the file's spelling and
    reject duplicate aliases. curve_updates accepts
    OND Version=8.1.6 single-voltage Converter/ProfilPIO Mode=1 points in watts,
    or BTR Version=8.1.6 AGM/Gel CapaCourant/Capa_DischRate Mode=1 points as
    [discharge hours, capacity relative to C10]. BTR requires positive points,
    increasing X, nondecreasing Y, coverage of 100h and C100/C10 in [1.15,1.45].
    Supply the complete table of 4..256 points; X >= 1e-9 and X gaps >= 1e-8.
    Count changes require point_count_editable from inspection and synchronize
    NPtsMax/NPtsEff/Point_N rows. Curves require expected_sha256; pass updates={}
    for curve-only edits. Automatic curves require explicit use_file_curve=true
    alongside points to clear the root automatic-profile bit. For BTR, this
    opt-in renames the supplied Capa_DischRate block to CLI's CapaCourant; all
    other tags and Flags are preserved. Use the current path from inspection.
    Three-voltage OND, other BTR curves and legacy PAN are read-only.
    commercial_updates edits/adds the Version=8.1.6 commercial form fields
    returned by inspection, including dimensions in metres and weight in kg.
    PAN Width/Height also update LargApp/LongApp when present; absent legacy
    root dimensions are not added to GUI-format files. Identity changes
    synchronize a tab-separated root Comment. remarks replaces up to five
    lines ([] clears); PAN's fifth line may encode functional options.
    These operations require expected_sha256 and share the preview/backup.
    Seller prices, flags and model fitting are separate from this form.
    curve_csv alternatively accepts one path-to-CSV-text entry; never combine
    it with curve_updates. Header: input_watts,output_watts for OND or
    discharge_hours,relative_capacity for BTR. Use comma-separated decimal
    numbers with decimal dots, 4..256 point rows and at most 64 KiB of UTF-8
    text. The same curve guards, preview and backup apply. No file is read
    from a caller-supplied path; supply the CSV contents.
    Physical behavior requires simulation; derived scalars are not refitted.
    A no-op returns changed=false without creating a backup."""
    return component_call("update", component_type, filename, updates,
                          expected_sha256, dry_run, curve_updates, use_file_curve,
                          commercial_updates, remarks, curve_csv)


@categorized_tool()
def pvsyst_backup_component(component_type: str, filename: str) -> dict:
    """Save a byte-identical backup under workspace/ComposPV/.mcp-backups."""
    return component_call("backup", component_type, filename)


@categorized_tool()
def pvsyst_restore_component(component_type: str, filename: str,
                             backup_name: str, confirm: bool = False) -> dict:
    """Restore a matching backup to a workspace component; confirm=true required.
    Back up current bytes, verify new snapshot metadata, and restore original bytes.
    Model diagnostics are returned separately; legacy snapshots have unverified integrity."""
    return component_call("restore", component_type, filename, backup_name, confirm)


@categorized_tool()
def pvsyst_archive_component(component_type: str, filename: str,
                             confirm: bool = False) -> dict:
    """Reversibly remove an unreferenced workspace component to .mcp-archive.
    Requires confirm=true; rejects references from workspace PRJ/VC files."""
    return component_call("archive", component_type, filename, confirm)


@categorized_tool()
def pvsyst_restore_archived_component(component_type: str, filename: str,
                                      archive_name: str, confirm: bool = False) -> dict:
    """Restore an archived component as a NEW file, preserving the archived
    snapshot; requires confirm=true and never overwrites existing files.
    Verify new snapshot metadata; return model diagnostics separately from recovery."""
    return component_call("restore_archive", component_type, filename, archive_name, confirm)


@categorized_tool()
def pvsyst_compare_components(component_type: str, first: str, second: str,
                              second_library: str = "workspace") -> dict:
    """Compare two same-type components: bounded text diff or binary PAN hashes."""
    return component_call("compare", component_type, first, second, second_library)


@categorized_tool()
def pvsyst_project_components(project: str, variant: str) -> dict:
    """Inspect PRJ/VC references to PAN, OND, BTR and GEN. Unresolved loose files
    may still exist inside the encrypted built-in database."""
    return component_call("dependencies", project, variant)


@categorized_tool()
def pvsyst_build_sfi(filename: str, variables: str = "all_common") -> dict:
    """Create a NEW hourly export definition in workspace/Models."""
    target = workspace_path("Models", filename, ".sfi")
    names = (VAR_GROUPS[variables] if variables in VAR_GROUPS
             else [part.strip() for part in variables.split(",")])
    build_sfi(target, list(names))
    return {"sfi": str(target), "variables": list(names)}


@categorized_tool()
def pvsyst_inspect_weather_csv(csv_name: str, expected_year: StrictInt | None = None) -> dict:
    """Check a PVsyst standard hourly CSV in Meteo before preparing a full-year run.
    Read-only: reports metadata/units, gaps, duplicates, bad numbers and observed
    monthly irradiation/temperature with input SHA-256. GHI/Tamb required; optional
    DHI/DNI and weather columns supported. Raw timestamps must cover one whole year
    at 00:00..23:00 (including leap day); no silent filling or time conversion.
    Nonzero shifts, explicit time references and POA need separate review. valid
    covers this CSV profile only, not MEF/SIT matching or native MET correctness."""
    try:
        return inspect_weather_csv(workspace_path("Meteo", csv_name, ".csv"), expected_year)
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from None


@categorized_tool()
def pvsyst_build_monthly_weather(filename: str, global_h: list[float],
                                 temperature: list[float],
                                 diffuse_h: list[float] | None = None,
                                 wind_velocity: list[float] | None = None) -> dict:
    """Create the 14-row monthly weather CSV for create-site in workspace/Meteo."""
    target = workspace_path("Meteo", filename, ".csv")
    build_monthly_weather_csv(target, global_h, temperature, diffuse_h, wind_velocity)
    return {"weather_csv": str(target)}


@categorized_tool()
def pvsyst_create_site(site_name: str, latitude: float, longitude: float,
                       weather_csv_name: str, sit_name: str,
                       altitude: float | None = None, timezone: float | None = None,
                       country_code: str = "", region: str = "", source: str = "",
                       albedo: float | None = None, log_level: int | None = None) -> dict:
    """Use CLI create-site to create a NEW SIT in workspace/Sites (8.1+).
    Stage or generate monthly weather CSV in workspace/Meteo. If altitude,
    timezone or country are omitted, the CLI may query an external service."""
    weather = workspace_path("Meteo", weather_csv_name, ".csv")
    target = workspace_path("Sites", sit_name, ".sit")
    output = cli().create_site(site_name, latitude, longitude, weather, target,
                               altitude=altitude, timezone=timezone,
                               country_code=country_code, region=region, source=source,
                               albedo=albedo, log_level=log_level)
    return {"site": str(output)}


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


@categorized_tool()
def pvsyst_simulate_template(template_project: StrictStr = "", variant: StrictStr = "",
                             new_project: StrictStr = "", pan_name: StrictStr = "",
                             ond_name: StrictStr = "", site_name: StrictStr = "",
                             met_name: StrictStr = "", tilt: StrictInt | StrictFloat | None = None,
                             year: StrictInt | None = None, dry_run: StrictBool = True,
                             capacity: CapacityRequest | None = None) -> dict:
    """Preflight or execute one template-to-full-year grid simulation without GUI.
    Supply an existing valid PRJ/VC, loose text PAN/OND, workspace SIT/MET, tilt,
    year and a NEW project filename. Missing inputs return field-specific issues.
    Scope: completely checked single subarray/fixed plane, no active shading/layout,
    PRCorrTemp=False; retain template counts unless capacity is explicitly supplied.
    capacity plans integer series/strings/inverters against declared design voltages,
    currents, temperatures, IEC/UL module voltage rating and maximum DC:AC ratio.
    Require independent unshared 8.1.0/8.1.6, one repeatable inverter/MPPT/string branch,
    full hardware MPPT population and crystalline PAN at STC; mode/topology stays intact.
    Design envelope/source are required, not guessed from raw temperature coefficients.
    No native temperature-model reproduction, hardware selection or design certification.
    dry_run=true (default) writes nothing and consumes no simulation execution.
    dry_run=false copies/configures the project, runs Jan 1 through Dec 31 hourly,
    validates native context/complete year, and returns energy, standard PR, IL_Pmax,
    warnings, CSV/PDF/SFI and a persisted JSON manifest with reverse recovery steps.
    Existing project/output files are never overwritten. Failure retains the original
    template and working files/backups/manifest; no automatic retry or destructive cleanup.
    status is blocked/ready/completed/needs_review/failed; inspect it, not just MCP is_error.
    Extreme IL_Pmax versus STC reference energy triggers a disclosed review heuristic,
    not a claim of native physical validation or an automatic model repair.
    Year is 1969..2068 due to two-digit native CSV dates. Restart host for this tool."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return simulate_template(current, store, template_project=template_project, variant=variant,
        new_project=new_project, pan_name=pan_name, ond_name=ond_name, site_name=site_name,
        met_name=met_name, tilt=tilt, year=year, dry_run=dry_run,
        capacity=capacity.model_dump(exclude_none=True) if capacity is not None else None)


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
def pvsyst_convert_meteo(csv_name: str, mef_name: str, sit_name: str,
                         met_name: str, timeshift: int = 0,
                         log_level: int | None = None) -> dict:
    """Convert a staged CSV + MEF in Meteo and SIT in Sites to a NEW MET."""
    source = workspace_path("Meteo", csv_name, ".csv")
    mef = workspace_path("Meteo", mef_name, ".mef")
    sit = workspace_path("Sites", sit_name, ".sit")
    target = workspace_path("Meteo", met_name, ".met")
    return {"met": str(cli().convert_meteo(source, mef, sit, target, timeshift,
                                            log_level=log_level))}


@categorized_tool()
def pvsyst_export_logs() -> dict:
    """Export CLI diagnostic logs to a ZIP under workspace/Results.
    The archive may contain private data; only its path is returned."""
    root = cli().workspace.resolve(strict=True)
    folder = (root / "Results").resolve()
    if root not in folder.parents:
        raise ValueError("Results folder resolves outside the workspace")
    return {"zip": str(cli().export_logs(folder))}


@categorized_tool()
def pvsyst_read_results(csv_name: str, columns: str = "E_Grid,PR",
                        folder: str = "Results", compare_to: str = "",
                        compare_folder: str | None = None) -> dict:
    """Summarize hourly or subhour SFI rows in Results or UserHourly.
    Complete energy requires finite samples and regular cadence; observed energy excludes missing samples.
    With compare_to, return target (csv_name) minus baseline (compare_to), using
    1..64 distinct columns, identical explicit units and identical increasing
    timestamps. compare_folder defaults to folder. No resampling/conversion.
    Statistics use finite pairs only; missing pairs and coverage are explicit.
    Power differences in W/kW integrate to kWh only at a regular cadence.
    Comparison verifies both file hashes before/after reading; it runs no simulation."""
    requested = tuple(column.strip() for column in columns.split(",") if column.strip())
    if compare_to:
        baseline_folder = folder if compare_folder is None else compare_folder
        try:
            result = compare_results(result_path(baseline_folder, compare_to),
                                     result_path(folder, csv_name), requested)
        except (ValueError, OSError) as exc:
            raise ToolError(str(exc)) from None
        result["baseline"]["folder"] = baseline_folder
        result["target"]["folder"] = folder
        return result
    if compare_folder is not None:
        raise ValueError("compare_folder requires compare_to")
    return summarize_results(result_path(folder, csv_name), requested)


@categorized_tool()
def pvsyst_read_rows(csv_name: str, columns: str = "E_Grid,PR",
                     offset: int = 0, limit: int = 100,
                     folder: str = "Results") -> dict:
    """Read a bounded page from Results or batch UserHourly CSVs."""
    if offset < 0 or not 1 <= limit <= 500:
        raise ValueError("Offset must be >= 0 and limit between 1 and 500")
    selected = [s.strip() for s in columns.split(",") if s.strip()]
    indices = None
    total = 0
    page: list[list] = []
    path = result_path(folder, csv_name)
    for headers, row in iter_result_csv(path):
        if indices is None:
            if not selected or any(name not in headers[1:] for name in selected):
                raise ValueError(f"Choose columns from {headers[1:]}")
            indices = [headers.index(name) for name in selected]
        if offset <= total < offset + limit:
            page.append([str(row[0]), *(row[i] if math.isfinite(row[i]) else None
                                        for i in indices)])
        total += 1
    units = result_units(path)
    return {"total": total, "offset": offset, "columns": ["date", *selected],
            "units": {name: units.get(name, "") for name in selected},
            "rows": page}


@categorized_tool()
def pvsyst_read_batch_results(summary_name: str,
                              columns: str = "E_Grid,PR") -> dict:
    """Read SIM_* scenario values from a UserBatch/*Results.CSV summary."""
    if not summary_name.lower().endswith("results.csv"):
        raise ValueError("Expected a batch Results.CSV filename")
    target = workspace_path("UserBatch", summary_name, ".csv")
    requested = tuple(s.strip() for s in columns.split(",") if s.strip())
    if not requested:
        raise ValueError("Select at least one batch result column")
    return parse_batch_results(target, requested)


if __name__ == "__main__":
    mcp.run(transport="stdio")
