"""Workspace-scoped stdio MCP bridge for PVsystCLI 8.0/8.1 (SDK 2.x).

Set PVSYST_CLI and PVSYST_WORKSPACE in the client's MCP server environment.
Only supported CLI options discovered from the installed binary are used.
"""
from __future__ import annotations

import inspect
import math
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import StrictBool, StrictFloat

from pvsyst_cli import (PVsystCLI, VAR_GROUPS, build_monthly_weather_csv,
                        build_sfi, iter_result_csv, parse_batch_results, result_units, summarize_results)
from pvsyst_components import ComponentStore
from pvsyst_variants import VariantStore
from pvsyst_paths import workspace_file

mcp = MCPServer("pvsyst-cli")
_cli: PVsystCLI | None = None

TOOL_GROUPS = {
    "Setup & License": (
        "pvsyst_capabilities", "pvsyst_license_info", "pvsyst_license_activate",
        "pvsyst_license_deactivate", "pvsyst_license_sync", "pvsyst_export_logs",
    ),
    "Projects & Variants": (
        "pvsyst_list_projects", "pvsyst_list_variants", "pvsyst_inspect_project",
        "pvsyst_clone_project", "pvsyst_update_project_sources",
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
    ),
    "Simulation": ("pvsyst_build_sfi", "pvsyst_run_simulation"),
    "Results": ("pvsyst_read_results", "pvsyst_read_rows", "pvsyst_read_batch_results"),
}
TOOL_CATEGORY = {name: category for category, names in TOOL_GROUPS.items() for name in names}
if len(TOOL_CATEGORY) != sum(map(len, TOOL_GROUPS.values())):
    raise RuntimeError("Duplicate tool category assignment")


def categorized_tool():
    """Label MCP descriptions without changing established wire-level tool names."""
    def register(func):
        category = TOOL_CATEGORY[func.__name__]
        return mcp.tool(description=f"[{category}] {inspect.getdoc(func)}")(func)
    return register


def cli() -> PVsystCLI:
    global _cli
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
    files and differences between project and variant sources."""
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
def pvsyst_clone_project(source_project: str, new_project: str) -> dict:
    """Create a NEW project by copying an existing PRJ and all its VC variants.
    Embedded site/meteo snapshots and external component references are preserved."""
    return variant_call("clone_project", source_project, new_project)


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
    """Restore every PRJ/VC file from one source-update transaction, after
    matching current hashes, a complete transaction manifest and backup hashes.
    Requires confirm=true and backs up current bytes; legacy sets need manual recovery."""
    return variant_call("restore_project_sources", project, backups, expected_files, confirm)


@categorized_tool()
def pvsyst_get_variant_components(project: str, variant: str) -> dict:
    """Read component references in a variant and its current SHA-256 for guarded edits."""
    return variant_call("inspect", project, variant)


@categorized_tool()
def pvsyst_clone_variant(project: str, source_variant: str, new_variant: str,
                         updates: dict[str, str] | None = None, subarray_id: int = 1) -> dict:
    """Copy an existing variant as a NEW VC file. Optional PAN/OND edits target one
    SubArrayId; BTR/GEN edits target the existing pvSystem reference only."""
    return variant_call("clone", project, source_variant, new_variant, updates, subarray_id)


@categorized_tool()
def pvsyst_update_variant_components(project: str, variant: str,
                                     updates: dict[str, str], expected_sha256: str,
                                     subarray_id: int = 1) -> dict:
    """Replace existing PAN/OND/BTR/GEN references only, with backup and SHA guard.
    Use pvsyst_get_variant_components to obtain the current expected_sha256."""
    return variant_call("update", project, variant, updates, expected_sha256, subarray_id)


@categorized_tool()
def pvsyst_validate_variant_structure(project: str, variant: str) -> dict:
    """Check orientation and supported grid circuit references; disclose unchecked scope.
    valid is false for issues, null for incomplete checks, true when scoped checks pass."""
    return variant_call("validate_structure", project, variant)


@categorized_tool()
def pvsyst_clone_subarray(project: str, variant: str, source_subarray_id: int,
                          expected_sha256: str) -> dict:
    """Duplicate a self-contained inverter branch in an unshaded grid variant.
    Requires the current variant hash; saves a rollback snapshot."""
    return variant_call("clone_subarray", project, variant, source_subarray_id, expected_sha256)


@categorized_tool()
def pvsyst_remove_subarray(project: str, variant: str, subarray_id: int,
                           expected_sha256: str) -> dict:
    """Remove one isolated unshaded grid inverter branch, retaining its shared orientation.
    Requires the current variant hash and creates a rollback snapshot."""
    return variant_call("remove_subarray", project, variant, subarray_id, expected_sha256)


@categorized_tool()
def pvsyst_get_variant_parameters(project: str, variant: str) -> dict:
    """Inspect fixed-plane orientations and subarray sizing/backup thresholds."""
    return variant_call("inspect_parameters", project, variant)


@categorized_tool()
def pvsyst_update_variant_parameters(project: str, variant: str,
                                     expected_sha256: str, subarray_id: int = 1,
                                     orientation_id: int = 1,
                                     subarray_updates: dict[str, float] | None = None,
                                     orientation_updates: dict[str, float] | None = None) -> dict:
    """Edit existing validated scalar parameters with SHA guard and rollback backup.
    Circuit tree, field type, and system flags are preserved."""
    return variant_call("update_parameters", project, variant, expected_sha256,
                        subarray_id, orientation_id, subarray_updates, orientation_updates)


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
                         limit: int = 100) -> dict:
    """Read paged UTF-8 text, scalar allowlist, SHA and OND/BTR curve paths/
    active points, plus commercial field types/units and remarks. Curves report
    mode or CLI tag recognition, supported edits and their
    preconditions; static inspection does not verify simulation behavior.
    Legacy binary PAN is metadata only."""
    return component_call("inspect", component_type, filename, library, offset, limit)


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
                           remarks: list[str] | None = None) -> dict:
    """Edit allowlisted text fields in one workspace component with automatic
    backup. dry_run returns a diff and candidate SHA without writes. Pass the
    inspected expected_sha256 to reject stale edits. curve_updates accepts
    OND Version=8.1.6 single-voltage Converter/ProfilPIO Mode=1 points in watts,
    or BTR Version=8.1.6 AGM/Gel CapaCourant/Capa_DischRate Mode=1 points as
    [discharge hours, capacity relative to C10]. BTR requires positive points,
    increasing X, nondecreasing Y, coverage of 100h and C100/C10 in [1.15,1.45].
    Both preserve the active count. Curves require expected_sha256; pass updates={}
    for curve-only edits. Automatic curves require explicit use_file_curve=true
    alongside points to clear the root automatic-profile bit. For BTR, this
    opt-in renames the supplied Capa_DischRate block to CLI's CapaCourant; all
    other tags and Flags are preserved. Use the current path from inspection.
    Three-voltage OND, other BTR curves and legacy PAN are read-only.
    commercial_updates edits/adds the Version=8.1.6 commercial form fields
    returned by inspection, including dimensions in metres and weight in kg.
    PAN Width/Height also update existing LargApp/LongApp; identity changes
    synchronize a tab-separated root Comment. remarks replaces up to five
    lines ([] clears); PAN's fifth line may encode functional options.
    These operations require expected_sha256 and share the preview/backup.
    Seller prices, flags and model fitting are separate from this form.
    Physical behavior requires simulation; derived scalars are not refitted.
    A no-op returns changed=false without creating a backup."""
    return component_call("update", component_type, filename, updates,
                          expected_sha256, dry_run, curve_updates, use_file_curve,
                          commercial_updates, remarks)


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
                        folder: str = "Results") -> dict:
    """Summarize hourly or subhour SFI rows in Results or UserHourly.
    Complete energy requires finite samples and regular cadence; observed energy excludes missing samples."""
    path = result_path(folder, csv_name)
    requested = tuple(column.strip() for column in columns.split(",") if column.strip())
    return summarize_results(path, requested)


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
