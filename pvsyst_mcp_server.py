"""Workspace-scoped stdio MCP bridge for PVsystCLI 8.0/8.1 (SDK 2.x).

Set PVSYST_CLI and PVSYST_WORKSPACE in the client's MCP server environment.
Only supported CLI options discovered from the installed binary are used.
"""
from __future__ import annotations

import inspect
import math
import os
from collections.abc import Mapping
from threading import Lock
from typing import Annotated, Any, Literal
from functools import wraps
from pathlib import Path

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from pvsyst_cli import (PVsystCLI, PVsystError, VAR_GROUPS, build_monthly_weather_csv,
                        build_sfi, iter_result_csv, parse_batch_results, result_units, summarize_results)
from pvsyst_component_editor import objects
from pvsyst_components import ComponentStore, electrical_inventory
from pvsyst_component_builder import build_sourced_component
from pvsyst_variants import VariantStore
from pvsyst_design_flow import simulate_design
from pvsyst_paths import workspace_file
from pvsyst_results import compare_results
from pvsyst_simulation import simulate_template
from pvsyst_source_inputs import SCOPE as SOURCED_DESIGN_SCOPE, prepare_sourced_design
from pvsyst_load_profile import prepare_load_profile
from pvsyst_load_variant import create_load_variant
from pvsyst_grid_storage import create_grid_storage_variant
from pvsyst_grid_storage_system import create_grid_storage_system
from pvsyst_weather import inspect_weather_csv
from pvsyst_weather_acquisition import download_weather, search_locations
from pvsyst_weather_flow import prepare_weather
from pvsyst_weather_review import review_weather

# Strict numbers reject booleans and numeric strings, so a JSON true or "20" never
# becomes a design number. Defined before the first tool signature that uses it.
StrictNum = StrictInt | StrictFloat

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
        "pvsyst_compare_components", "pvsyst_project_components", "pvsyst_build_sourced_component",
    ),
    "Sites & Weather": (
        "pvsyst_build_monthly_weather", "pvsyst_create_site", "pvsyst_convert_meteo",
        "pvsyst_inspect_weather_csv", "pvsyst_prepare_weather", "pvsyst_resolve_location",
        "pvsyst_acquire_weather", "pvsyst_review_weather",
    ),
    "Loads & Storage": ("pvsyst_prepare_load_profile", "pvsyst_create_load_variant",
                        "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system"),
    "Simulation": ("pvsyst_build_sfi", "pvsyst_run_simulation", "pvsyst_simulate_template",
                   "pvsyst_simulate_design", "pvsyst_prepare_sourced_design"),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results"),
}
TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}
if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")

# MCP 2.2 middleware sees raw arguments before its default extra=ignore model.
# Limit strict top-level keys to these entry points and derive keys from signatures.
STRICT_ARGUMENT_TOOLS = ("pvsyst_prepare_weather", "pvsyst_resolve_location",
                         "pvsyst_acquire_weather", "pvsyst_review_weather",
                         "pvsyst_simulate_template", "pvsyst_simulate_design",
                         "pvsyst_prepare_sourced_design", "pvsyst_build_sourced_component",
                          "pvsyst_prepare_load_profile", "pvsyst_create_load_variant",
                          "pvsyst_create_grid_storage_variant", "pvsyst_create_grid_storage_system")
_strict_arguments: dict[str, frozenset[str]] = {}


async def reject_unknown_tool_arguments(ctx: ServerRequestContext[Any, Any],
                                        call_next: CallNext) -> HandlerResult:
    """Refuse unknown top-level arguments for the strict entry points only."""
    if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
        name = ctx.params.get("name")
        arguments = ctx.params.get("arguments")
        # A malformed name/arguments value is left to the SDK's own params validation.
        accepted = _strict_arguments.get(name) if isinstance(name, str) else None
        if accepted is not None and isinstance(arguments, Mapping):
            unknown = sorted(set(arguments) - accepted, key=str)
            if unknown:
                text = (f"Unknown argument(s) for {name}: {', '.join(map(str, unknown))}. "
                        f"Accepted arguments: {', '.join(sorted(accepted))}")
                return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
    return await call_next(ctx)


mcp.middleware.append(reject_unknown_tool_arguments)


def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        if func.__name__ in STRICT_ARGUMENT_TOOLS:
            # Typos are rejected against the published signature, so the whitelist cannot drift.
            _strict_arguments[func.__name__] = frozenset(inspect.signature(func).parameters)
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


class SoilingUpdates(BaseModel):
    """Explicit native soiling mode and GUI-precision percentage values."""
    model_config = ConfigDict(extra="forbid")
    mode: Literal["none", "constant", "monthly"]
    yearly_loss_pct: StrictNum | None = None
    monthly_loss_pct: list[StrictNum] | None = None


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
                                     soiling_updates: SoilingUpdates | None = None) -> dict:
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
    soiling_updates configures the native whole-array Soiling block in a separate
    transaction (variant_name may accompany it). mode=none removes that block;
    constant requires yearly_loss_pct; monthly requires twelve monthly_loss_pct.
    Percent values are 0..100 in 0.1 percentage-point increments, matching GUI save
    precision. Verified scope: 8.1.6 PV-only Grid, one unshaded fixed plane/subarray,
    main Flags=$00100400/$01900400, Flags2=$082A. Flags and unrelated fields stay
    unchanged; no irradiance/yield calculation, legacy or file-based soiling edits.
    No-op edits create no backup."""
    return variant_call("update_parameters", project, variant, expected_sha256,
                        subarray_id, orientation_id, subarray_updates, orientation_updates,
                        generator_updates.model_dump(exclude_none=True) if generator_updates is not None else None,
                        dry_run, power_sharing_enabled, independent_mppt_enabled, main_array_updates, variant_name, add_utf8_bom,
                        soiling_updates.model_dump(exclude_none=True) if soiling_updates is not None else None)


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
    """Create a NEW hourly export definition in workspace/Models.
    variables=grid_storage includes common PV outputs plus native load, SOC,
    battery/grid flows, EUnused and all three converter losses CL_Chrg, CL_InvB,
    CL_Over. Include CL_Over when reviewing native grid-storage energy balance;
    this exports PVsyst values, not a separate storage calculation.
    variables=soiling adds native GlobSlg/SlgLoss irradiance outputs to common PV
    outputs; native runs may omit these columns when soiling is disabled."""
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
def pvsyst_prepare_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                            sit_name: StrictStr = "", met_name: StrictStr = "",
                            country_code: StrictStr = "", time_basis: StrictStr = "",
                            dry_run: StrictBool = True) -> dict:
    """Prepare NEW native SIT/MET and a provenance manifest from one full hourly CSV.
    GHI, DHI, Tamb only; explicit uppercase ISO country code and time_basis
    local_standard_interval_start are required (fixed winter timezone, no DST).
    Coordinates, altitude, timezone, site and source come from verified CSV metadata.
    dry_run=true writes nothing. Execution preserves raw bytes, normalizes only
    columns/units, creates monthly CSV/MEF, runs create-site and convert-meteo.
    Conversion warnings are retained as needs_review; prepared is not native
    MET certification. Pass artifacts.manifest filename as weather_manifest_name
    to simulate_template for explicit SIT/MET loading and hourly weather comparison.
    No UTC/DST shifts, missing-data filling, ignored columns or binary parsing.
    Existing outputs are never overwritten; failure artifacts/manifest are retained."""
    return prepare_weather(cli(), csv_name=csv_name, year=year, sit_name=sit_name,
                           met_name=met_name, country_code=country_code,
                           time_basis=time_basis, dry_run=dry_run)


@categorized_tool()
def pvsyst_resolve_location(query: StrictStr, country_code: StrictStr = "") -> dict:
    """Resolve a place name into provider geocoding candidates without selecting one.
    Read-only geocoding lookup: returns every candidate with its integer location_id,
    coordinates, ISO country code and IANA zone, and sets selection_required unless
    exactly one candidate matched. query must be 2..100 characters of a place name;
    country_code, when given, must be an uppercase ISO alpha-2 code. No candidate is
    chosen automatically, nothing is inferred from a single hit and no weather is
    downloaded or written."""
    return search_locations(query, country_code)


@categorized_tool()
def pvsyst_acquire_weather(csv_name: StrictStr = "", year: StrictInt | None = None,
                           location_id: StrictInt | None = None,
                           latitude: StrictNum | None = None, longitude: StrictNum | None = None,
                           site_name: StrictStr = "", country_code: StrictStr = "",
                           altitude: StrictNum | None = None, timezone_name: StrictStr = "",
                           temperature_method: StrictStr = "endpoint_mean",
                           dry_run: StrictBool = True) -> dict:
    """Plan or execute one Open-Meteo ERA5 year as a PVsyst standard hourly weather CSV.
    Give either location_id from pvsyst_resolve_location, or explicit latitude/longitude
    together with site_name, country_code and timezone_name; mixing both is blocked.
    year is an integer in 1940..2068 and the requested year must already be fully
    published by the provider. temperature_method=endpoint_mean is the only verified
    method: the unweighted mean of the two UTC instants bounding each local standard
    hour, disclosed as a linear approximation. dry_run=true (default) downloads nothing
    and writes nothing; an execution run writes the raw provider response, the standard
    CSV and a provenance manifest, refuses to overwrite existing files and never fills,
    zeroes or interpolates a null hour. Half-hour IANA offsets and standard offsets that
    change inside the requested year are blocked instead of being rebinned. Native MET
    conversion and pyranometer quality are outside this scope."""
    return download_weather(cli().workspace, csv_name=csv_name, year=year,
                            location_id=location_id, latitude=latitude, longitude=longitude,
                            site_name=site_name, country_code=country_code, altitude=altitude,
                            timezone_name=timezone_name, temperature_method=temperature_method,
                            dry_run=dry_run)


@categorized_tool()
def pvsyst_review_weather(csv_name: StrictStr, native_csv_name: StrictStr, year: StrictInt,
                          acquisition_manifest_name: StrictStr = "",
                          weather_manifest_name: StrictStr = "") -> dict:
    """Review one prepared weather CSV against one native result, read-only.
    csv_name names the prepared standard CSV in workspace/Meteo and native_csv_name the
    exported hourly result in workspace/Results; year is the strict integer year under
    review. The CSV is re-inspected with the hash-free value checker, so a historical
    manifest report is never trusted; the optional acquisition_manifest_name and
    weather_manifest_name add the stored acquisition provenance and the declared time
    basis. Provider coverage and native hourly values are compared as measured
    quantities, and native .MET/.SIT declared fields are read from their file headers
    only. This review does not certify the effective sun-position time shift inside the
    native run, does not claim that the endpoint_mean ambient temperature is a measured
    hourly mean, and does not certify climate extremes. Nothing is written, hashed or
    simulated. status is blocked, needs_review or completed: review items are listed in
    the result instead of being dropped, and blocked means the evidence could not be
    read at all."""
    return review_weather(cli().workspace, csv_name=csv_name, native_csv_name=native_csv_name,
                          year=year, acquisition_manifest_name=acquisition_manifest_name,
                          weather_manifest_name=weather_manifest_name)


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
    max/Euro efficiencies are percent, with Euro <= max. SelfConsumption or
    WeakGrid with PVAvailable/Immediate are supported. WeakGrid requires a fresh
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


@categorized_tool()
def pvsyst_simulate_template(template_project: StrictStr = "", variant: StrictStr = "",
                             new_project: StrictStr = "", pan_name: StrictStr = "",
                             ond_name: StrictStr = "", site_name: StrictStr = "",
                             met_name: StrictStr = "", tilt: StrictNum | None = None,
                             azimuth: StrictNum | None = None,
                             year: StrictInt | None = None, dry_run: StrictBool = True,
                             capacity: CapacityRequest | None = None,
                             weather_manifest_name: StrictStr = "",
                             losses: LossRequest | None = None) -> dict:
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
    angles and losses; no loss is fitted, inferred or defaulted."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return simulate_template(current, store, template_project=template_project, variant=variant,
        new_project=new_project, pan_name=pan_name, ond_name=ond_name, site_name=site_name,
        met_name=met_name, tilt=tilt, azimuth=azimuth, year=year, dry_run=dry_run,
        capacity=capacity.model_dump(exclude_none=True) if capacity is not None else None,
        weather_manifest_name=weather_manifest_name,
        losses=losses.model_dump(exclude_none=True) if losses is not None else None)


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
    was not changed."""
    current = cli()
    component_store = components()
    store = VariantStore(component_store.workspace, component_store, component_store.lock)
    return simulate_design(current, store, spec=spec.model_dump(exclude_none=True), dry_run=dry_run)


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


@categorized_tool()
def pvsyst_build_sourced_component(
        component_type: Literal["PAN", "OND"], template_name: StrictStr, new_name: StrictStr,
        manufacturer: StrictStr, model: StrictStr,
        sources: Annotated[dict[str, SourceTextRequest], Field(min_length=1, max_length=16)],
        declarations: Annotated[list[SourceDeclarationRequest], Field(min_length=1, max_length=32)],
        source_library: Literal["workspace", "builtin"] = "workspace", dry_run: StrictBool = True,
        acknowledge_inherited_model: StrictBool = False) -> dict:
    """Preview or create a NEW user-defined PAN/OND from a complete exact template and cited values.
    Reuses the source-text/numeric/unit binding and component editors. PAN accepts PNom (W),
    ISC/Imp (A), Voc/Vmp (V); OND accepts PNomConv/PMaxOUT (kW), VMppMin/VMPPMax/VAbsMax (V),
    EfficMax/EfficEuro (%). Width/Height/Depth (m or explicitly converted mm) and Weight (kg)
    require the verified 8.1.6 commercial form. Unspecified fields, hardware counts and
    efficiency/IAM/model curves stay inherited, not refitted or scaled. Temperature coefficient
    units are not guessed. A dry run returns citation provenance, inherited parameters and a
    diff without writes. Creation requires acknowledge_inherited_model=true, writes a new
    component and a candidate source record in Results, and never overwrites existing paths.
    Successful preview/creation remain needs_review, not physical certification. Failed writes
    retain disclosed artifacts for inspection. Use a referencing native run to verify behavior;
    datasheet authenticity, page layout and source/model applicability are not certified here."""
    return build_sourced_component(
        components(), component_type=component_type, template_name=template_name, new_name=new_name,
        manufacturer=manufacturer, model=model,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        source_library=source_library, dry_run=dry_run,
        acknowledge_inherited_model=acknowledge_inherited_model)


class SourcedDesignGoals(BaseModel):
    """Explicit engineering goals only; no envelope, coefficient or limit is injected here."""
    model_config = ConfigDict(extra="forbid")
    target_dc_kw: StrictNum | None = None
    max_dc_ac_ratio: StrictNum | None = None
    capacity_tolerance_pct: StrictNum | None = None
    voltage_standard: Literal["IEC", "UL"] | None = None
    design_basis_extra: StrictStr | None = None


def sourced_component(store: ComponentStore, kind: str, filename: str) -> tuple[str, str, dict | None]:
    """Read one loose UTF-8 component by exact filename and bind its declared identity.

    Reuses the store's own path/format checks and the object parser: an explicit filename is
    never replaced by a manufacturer search, nothing is hashed and no model is invented. The
    root object is required and a PVObject_Commercial block is optional; when declared, the
    Commercial identity wins over the root, while a disagreement between the two blocks the
    flow. Returns (field, message, payload); payload is None when the component blocks.
    """
    path = None
    for library in ("workspace", "builtin"):
        try:
            candidate = store._path(kind, filename, library)
        except ValueError as exc:
            return f"{kind}.name", str(exc), None
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        return (f"{kind}.name",
                f"{filename} is not a loose {kind} file in the workspace or the installed library", None)
    try:
        text = store._text(kind, store._read(path))[1]
    except (ValueError, OSError) as exc:
        return f"{kind}.name", str(exc), None
    if text is None:
        return (f"{kind}.name",
                "Legacy binary PAN carries no readable declared identity; use a UTF-8 text component", None)
    blocks = objects(text)
    roots = [block for block in blocks if not block.path]
    commercial = [block for block in blocks if block.path == "PVObject_Commercial"]
    if len(roots) != 1 or len(commercial) > 1:
        return (f"{kind}.name",
                "Expected exactly one root object and at most one PVObject_Commercial block", None)
    owners = [("Commercial", commercial[0].fields)] if commercial else []
    owners.append(("root", roots[0].fields))
    identity = {}
    for name, key in (("Manufacturer", "manufacturer"), ("Model", "model")):
        declared = {}
        for label, fields in owners:
            values = [value.strip() for _, value in fields.get(name, []) if value.strip()]
            # Occurrence count, not a de-duplicated set: a repeated identical field is ambiguous.
            if len(values) > 1:
                return (f"{kind}.{key}", f"{name} occurs {len(values)} times in its {label} object; "
                                         "exactly one occurrence is bound", None)
            if values:
                declared[label] = values[0]
        if len({*declared.values()}) > 1:
            return (f"{kind}.{key}",
                    f"{name} is declared differently in the root object and the Commercial block", None)
        if not declared:
            return f"{kind}.{key}", f"{name} is missing in its owning object", None
        identity[key] = next(iter(declared.values()))
    return "", "", {"manufacturer": identity["manufacturer"], "model": identity["model"],
                    "electrical": electrical_inventory(kind, text)}


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


if set(_strict_arguments) != set(STRICT_ARGUMENT_TOOLS):
    # A renamed entry point must not silently lose its unknown-argument contract.
    raise RuntimeError(f"Unregistered strict entry points: {sorted(set(STRICT_ARGUMENT_TOOLS) - set(_strict_arguments))}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
