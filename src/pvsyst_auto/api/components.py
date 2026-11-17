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
    """List PAN, OND, BTR, GEN or REV loose files in workspace or installed DataRO.
    The encrypted built-in component database is not enumerable."""
    return component_call("list", component_type, library, query, offset, limit)

@categorized_tool()
def pvsyst_get_component(component_type: str, filename: str,
                         library: str = "workspace", offset: int = 0,
                         limit: int = 100, include_curve_csv: StrictBool = False) -> dict:
    """Read paged UTF-8 text, editable scalar allowlist, SHA-256, OND/BTR curves,
    commercial fields, and remarks for a PAN, OND, BTR, GEN, or REV component."""
    return component_call("inspect", component_type, filename, library, offset, limit, include_curve_csv)

@categorized_tool()
def pvsyst_validate_component(component_type: str, filename: str,
                              library: str = "workspace") -> dict:
    """Validate structure, required fields, and numeric bounds of a component file."""
    return component_call("validate", component_type, filename, library)

@categorized_tool()
def pvsyst_copy_component(component_type: str, source_name: str, new_name: str,
                          source_library: str = "workspace") -> dict:
    """Copy a PAN/OND/BTR/GEN/REV into the workspace without overwrite."""
    return component_call("copy", component_type, source_name, new_name, source_library)

@categorized_tool()
def pvsyst_create_component(component_type: str, filename: str, content: str) -> dict:
    """Create a new PAN, OND, BTR, GEN, or REV file in the workspace from UTF-8 PVObject_ text."""
    return component_call("create", component_type, filename, content)

@categorized_tool()
def pvsyst_clone_component(component_type: str, source_name: str, new_name: str,
                           updates: dict[str, str], source_library: str = "workspace") -> dict:
    """Clone an existing text component into the workspace with updated scalar fields."""
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
    """Update allowlisted scalar fields, commercial metadata, remarks (up to 5 lines),
    or Mode 1 OND/BTR curve tables (4..256 points via curve_updates or curve_csv)
    in a workspace component with SHA-256 optimistic locking and automatic backup."""
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
    """Restore a workspace component from a backup snapshot (requires confirm=true)."""
    return component_call("restore", component_type, filename, backup_name, confirm)

@categorized_tool()
def pvsyst_archive_component(component_type: str, filename: str,
                             confirm: bool = False) -> dict:
    """Move an unreferenced workspace component to .mcp-archive (requires confirm=true)."""
    return component_call("archive", component_type, filename, confirm)

@categorized_tool()
def pvsyst_restore_archived_component(component_type: str, filename: str,
                                      archive_name: str, confirm: bool = False) -> dict:
    """Restore an archived component to the workspace without overwriting existing files (requires confirm=true)."""
    return component_call("restore_archive", component_type, filename, archive_name, confirm)

@categorized_tool()
def pvsyst_compare_components(component_type: str, first: str, second: str,
                              second_library: str = "workspace") -> dict:
    """Compare two same-type components by text diff or binary SHA-256."""
    return component_call("compare", component_type, first, second, second_library)

@categorized_tool()
def pvsyst_project_components(project: str, variant: str) -> dict:
    """List PAN, OND, BTR, and GEN references used by a project variant."""
    return component_call("dependencies", project, variant)

@categorized_tool()
def pvsyst_build_sourced_component(
        component_type: Literal["PAN", "OND"], template_name: StrictStr, new_name: StrictStr,
        manufacturer: StrictStr, model: StrictStr,
        sources: Annotated[dict[str, SourceTextRequest], Field(min_length=1, max_length=16)],
        declarations: Annotated[list[SourceDeclarationRequest], Field(min_length=1, max_length=32)],
        source_library: Literal["workspace", "builtin"] = "workspace", dry_run: StrictBool = True,
        acknowledge_inherited_model: StrictBool = False) -> dict:
    """Derive a new user-defined PAN or OND from a template using cited datasheet
    values and units (requires acknowledge_inherited_model=true when dry_run=false)."""
    return build_sourced_component(
        components(), component_type=component_type, template_name=template_name, new_name=new_name,
        manufacturer=manufacturer, model=model,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        source_library=source_library, dry_run=dry_run,
        acknowledge_inherited_model=acknowledge_inherited_model)
