"""Components MCP operations; native and domain behavior stays in the existing modules."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, StrictBool, StrictInt, StrictStr
from pvsyst_auto.components.builder import build_sourced_component
from pvsyst_auto.api.schemas import SourceDeclarationRequest, SourceTextRequest, StrictNum
from pvsyst_auto.api.context import categorized_tool, component_call, components


@categorized_tool()
def pvsyst_list_components(component_type: str, library: str = "workspace",
                           query: str = "", offset: StrictInt = 0, limit: StrictInt = 100,
                           min_power_w: StrictNum | None = None,
                           max_power_w: StrictNum | None = None,
                           bifacial: StrictBool | None = None,
                           min_system_voltage_v: StrictNum | None = None,
                           min_mppt: StrictInt | None = None,
                           manufacturer: StrictStr | None = None) -> dict:
    """List PAN, OND, BTR, GEN or REV loose files in workspace or installed DataRO with
    optional electrical filtering (power range, bifaciality, system voltage, MPPT count,
    manufacturer) backed by an mtime-keyed metadata cache."""
    return components().list(
        component_type, library, query, offset, limit,
        min_power_w=min_power_w, max_power_w=max_power_w,
        bifacial=bifacial, min_system_voltage_v=min_system_voltage_v,
        min_mppt=min_mppt, manufacturer=manufacturer,
    )

@categorized_tool()
def pvsyst_get_component(component_type: str, filename: str,
                         library: str = "workspace", offset: StrictInt = 0,
                         limit: StrictInt = 100, include_curve_csv: StrictBool = False) -> dict:
    """Read paged UTF-8 text, editable scalar allowlist, SHA-256, OND/BTR curves,
    commercial fields, and remarks for a PAN, OND, BTR, GEN, or REV component."""
    return component_call("inspect", component_type, filename, library, offset, limit, include_curve_csv)

@categorized_tool()
def pvsyst_validate_component(component_type: str, filename: str,
                              library: str = "workspace") -> dict:
    """Validate structure, required fields, and numeric bounds of a component file."""
    return component_call("validate", component_type, filename, library)

@categorized_tool()
def pvsyst_create_component(component_type: str, filename: str, content: str) -> dict:
    """Create a new PAN, OND, BTR, GEN, or REV file in the workspace from UTF-8 PVObject_ text."""
    return component_call("create", component_type, filename, content)

@categorized_tool()
def pvsyst_update_component(component_type: str, filename: str,
                           updates: dict[str, str],
                           expected_sha256: str | None = None, dry_run: StrictBool = False,
                           curve_updates: dict[str, list[list[StrictNum]]] | None = None,
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
        component_type: Literal["PAN", "OND"], new_name: StrictStr,
        manufacturer: StrictStr, model: StrictStr,
        sources: Annotated[dict[str, SourceTextRequest], Field(min_length=1, max_length=16)],
        declarations: Annotated[list[SourceDeclarationRequest], Field(min_length=1, max_length=32)],
        template_name: StrictStr | None = None,
        source_library: Literal["workspace", "builtin"] = "workspace", dry_run: StrictBool = True,
        acknowledge_inherited_model: StrictBool = False) -> dict:
    """Build a new user-defined PAN or OND from cited datasheet values: either derived from
    a template (template_name set) or synthesized zero-template with single-diode / Mode-1
    efficiency derivation (template_name omitted). Requires acknowledge_inherited_model=true
    when dry_run=false."""
    return build_sourced_component(
        components(), component_type=component_type, template_name=template_name, new_name=new_name,
        manufacturer=manufacturer, model=model,
        sources={key: entry.model_dump(exclude_none=True) for key, entry in sources.items()},
        declarations=[entry.model_dump(exclude_none=True) for entry in declarations],
        source_library=source_library, dry_run=dry_run,
        acknowledge_inherited_model=acknowledge_inherited_model)
