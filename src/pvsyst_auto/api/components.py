"""Components MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictFloat, StrictStr
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.api.schemas import SourceDeclarationRequest, SourceTextRequest
from pvsyst_auto.api.context import categorized_tool, component_call, components


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
    Successful preview/creation remain needs_review, not physical certification. A failed creation
    removes the source record it wrote and never overwrites existing files, so the same name can
    be retried. Use a referencing native run to verify behavior;
    datasheet authenticity, page layout and source/model applicability are not certified here."""
    return build_sourced_component(
        components(), component_type=component_type, template_name=template_name, new_name=new_name,
        manufacturer=manufacturer, model=model,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        source_library=source_library, dry_run=dry_run,
        acknowledge_inherited_model=acknowledge_inherited_model)
