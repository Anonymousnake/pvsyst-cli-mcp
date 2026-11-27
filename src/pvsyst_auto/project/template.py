"""Workspace template derivation, ground-array shading, and zero-template utility project synthesis."""
from __future__ import annotations

import math
from pathlib import Path
import re

from pvsyst_auto.components.editor import pan_validation_fields, root_block
from pvsyst_auto.engineering.sizing import plan_capacity
from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory, positive_integer, resize_capacity, set_independent_mppt
from pvsyst_auto.project.paths import project_path
from pvsyst_auto.project.synthesize import (
    SYNTH_VERSION, _clean_text, _fmt_angle, _pos_int, synthesize_project_and_variant,
)
from pvsyst_auto.scene.reader import pv_shading_header
from pvsyst_auto.validation import append_issue as _issue, finite_number as _finite
from pvsyst_auto.validation import NATIVE_TEXT_OR_SEED, SEED_LABEL

SEED_STEM, SEED_VARIANT, SEED_SUBARRAY = "_DEMO_Residential", "VC0", 1
SEED_VERSIONS = NATIVE_TEXT_OR_SEED
DATA_RO_FOLDER, CLI_VERSION = re.compile(r"PVsyst8\.1(?:\.\d+)?_Data", re.I), re.compile(r"\b8\.1(?:\.\d+)?\b")
MAX_MPPT_INPUTS, INDEPENDENT_MPPT_BIT = 64, 0x400
RETAINED_FIELDS = ("NModSerie", "LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI", "PQualityModP", "PMismUFixe", "PNomAC_InMPPT")


def _workspace_entries(store) -> list:
    try:
        return list(store._root().iterdir())
    except OSError:
        return []


def _cli_profile(cli, *, allow_window_creation=None) -> dict:
    """Require a readable 8.1 command profile; the installed 8.1 seed is never replaced."""
    capabilities = cli.capabilities(allow_window_creation=allow_window_creation)
    if not isinstance(capabilities, dict) or not capabilities:
        raise ValueError("The installed CLI reported no readable command profile")
    version = str(cli.version(allow_window_creation=allow_window_creation)).splitlines()[0].strip()
    if not CLI_VERSION.search(version):
        raise ValueError(f"The installed CLI is not an 8.1 release ({version!r}); the installed 8.1 seed is not replaced by another release")
    return {"version": version, "commands": sorted(capabilities)}


def _locate_seed(cli) -> tuple[Path, Path]:
    """The only accepted seed source: the unique 8.1 DataRO library next to the CLI."""
    install = Path(cli.cli)
    if not install.is_file():
        raise ValueError("The configured PVsystCLI path is not an existing file")
    if not (data_ro := install.parent / "DataRO").is_dir():
        raise ValueError("The installed CLI has no DataRO library next to it")
    libraries = sorted((p for p in data_ro.iterdir() if p.is_dir() and DATA_RO_FOLDER.fullmatch(p.name)), key=lambda p: p.name.casefold())
    if not libraries:
        installed = sorted(p.name for p in data_ro.iterdir() if p.is_dir() and re.fullmatch(r"PVsyst[0-9.]+_Data", p.name, re.I))
        raise ValueError("No installed PVsyst8.1 DataRO library was found" + (f" (installed: {', '.join(installed)})" if installed else "") + "; another release's seed is not substituted")
    if len(libraries) > 1:
        raise ValueError("Multiple installed PVsyst8.1 DataRO libraries; the seed source is ambiguous")
    projects = libraries[0] / "Projects"
    project, variant = projects / f"{SEED_STEM}.PRJ", projects / f"{SEED_STEM}.{SEED_VARIANT}"
    if not project.is_file() or not variant.is_file():
        raise ValueError(f"The installed 8.1 library has no complete {SEED_STEM}.PRJ/{SEED_STEM}.{SEED_VARIANT} seed")
    return project, variant


def _mppt_inputs(ond_electrical) -> int:
    """The target inverter's declared hardware MPPT count, 1..64; never inferred."""
    if not isinstance(ond_electrical, dict):
        raise ValueError("Provide the resolved target OND electrical inventory")
    item = (ond_electrical.get("fields") or {}).get("NbMPPT")
    if not isinstance(item, dict) or item.get("status") != "declared":
        raise ValueError("The target OND must declare NbMPPT; no hardware input count is inferred")
    if isinstance(val := item.get("value"), bool) or not isinstance(val, int) or not 1 <= val <= MAX_MPPT_INPUTS:
        raise ValueError(f"The target OND must declare an integer NbMPPT in 1..{MAX_MPPT_INPUTS}")
    return val


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _field_value(store, lines, bounds, key) -> str:
    if len(indices := store._field_indices(lines, bounds, key)) != 1:
        raise ValueError(f"The seed subarray must declare one {key}")
    return lines[indices[0]].split("=", 1)[1].strip()


def _explicit_line(lines: list[str], node) -> str:
    start = lines[node.line - 1]
    indent = re.match(r"[ \t]*", start).group(0)
    ending = start[len(start.rstrip("\r\n")):] or "\n"
    return f"{indent}  NElements=1{ending}"


def _branch_nodes(lines, array_id: int):
    return [n for top in circuit_nodes(lines) for n in _walk(top) if n.kind == "InverterNode" and n.one("SubArrayId") == str(array_id)]


def _declare_explicit_counts(lines: list[str], array_id: int) -> list[dict]:
    """Declare the implicit NElements=1 of the one exclusive inverter/string branch."""
    if len(groups := _branch_nodes(lines, array_id)) != 1:
        raise ValueError("The seed must have one exclusive inverter branch for the selected subarray")
    group = groups[0]
    if not group.children or any(child.kind != "MPPTNode" for child in group.children):
        raise ValueError("The seed must have direct explicit MPPT children only")
    leaves = [leaf for port in group.children for leaf in port.children]
    if any(p.one("SubArrayId") != str(array_id) for p in group.children) or any(leaf.one("SubArrayId") != str(array_id) for leaf in leaves):
        raise ValueError("The seed inverter/MPPT branch must belong to the selected subarray only")
    if any(leaf.kind != "StringNode" or leaf.children for leaf in leaves):
        raise ValueError("The seed must have one empty parallel-string leaf per MPPT input")
    pending = []
    for node in (group, *leaves):
        if (declared := node.fields.get("NElements")) is None:
            pending.append(node)
        elif declared != ["1"]:
            raise ValueError(f"Circuit line {node.line}: {node.kind} already declares NElements={declared[0]}; implicit single counts are not rewritten")
    added = []
    for node in sorted(pending, key=lambda item: item.line, reverse=True):
        lines.insert(node.line, _explicit_line(lines, node))
        above = sum(other.line < node.line for other in pending)
        added.append({"object": "pvCircuit", "node": node.kind, "field": "NElements", "value": "1",
                      "line": node.line + 1 + above, "reason": "Declare the single serialized branch explicitly for guarded resizing"})
    if added:
        if len(checked := _branch_nodes(lines, array_id)) != 1:
            raise ValueError("Declaring explicit counts changed the exclusive inverter branch count")
        grp = checked[0]
        for node in (grp, *[leaf for port in grp.children for leaf in port.children]):
            if node.fields.get("NElements") != ["1"]:
                raise ValueError(f"Circuit line {node.line}: {node.kind} must declare NElements=1 after the insertion")
    return added


def _component_path(store, kind: str, name: str) -> Path:
    path = store.components._path(kind, name)
    if not path.is_file() and store.components.builtin:
        path = store.components._path(kind, name, "builtin")
    if not path.is_file():
        raise FileNotFoundError(f"Component {name} must exist as a loose file")
    return path


def _verify_and_plan_seed_variant(store, final_lines: list[str], *, pan_name: str, pan_fields: dict | None,
                                  ond_electrical: dict, capacity: dict | None, orientation_id: int, count_error_prefix: str):
    store._require_no_geometry(final_lines, require_shading_section=True)
    final_arrays, _ = store._sections(final_lines)
    if (rechecked := grid_inventory(final_lines, final_arrays))["counts_valid"] is not True:
        raise ValueError(count_error_prefix + "; ".join(rechecked["issues"] + rechecked["unchecked_checks"]))
    store._ordinary_fixed_plane(final_lines, store._orientation(final_lines, orientation_id))
    block = root_block(final_lines, final_arrays[SEED_SUBARRAY])
    plan = None
    if capacity is not None:
        if pan_fields is None:
            pan_fields = pan_validation_fields(store.components._read(_component_path(store, "PAN", pan_name)).decode("utf-8-sig"))
        plan = plan_capacity(store, final_lines, SEED_SUBARRAY, pan_fields, ond_electrical, capacity)
    return block, plan


def standard_seed_texts(cli, store, *, ond_electrical, pan_name, ond_name,
                        capacity=None, pan_fields=None, allow_window_creation=None) -> dict:
    """Build the derived PRJ/VC0 text in memory; nothing is written and the seed is untouched."""
    profile = _cli_profile(cli, allow_window_creation=allow_window_creation)
    seed_project, seed_variant = _locate_seed(cli)
    store._project_text(project_data := store._read(seed_project))
    variant_data = store._read(seed_variant)
    project_bom, variant_bom = project_data.startswith(b"\xef\xbb\xbf"), variant_data.startswith(b"\xef\xbb\xbf")
    _, lines = store._text(variant_data)
    arrays, system = store._sections(lines)
    if sorted(arrays) != [SEED_SUBARRAY]:
        raise ValueError(f"The seed variant must declare exactly subarray {SEED_SUBARRAY}")
    if system is None:
        raise ValueError("The seed variant must declare one system section")
    types = store._field_indices(lines, system, "SystemType")
    if len(types) != 1 or lines[types[0]].split("=", 1)[1].strip() != "Grid":
        raise ValueError("The seed variant must declare SystemType=Grid")
    store._require_no_geometry(lines, require_shading_section=True)
    if (child := next((i for i in range(1, len(lines)) if lines[i].strip().startswith("PVObject_")), None)) is None:
        raise ValueError("The seed variant has no child PVObject block")
    if len(corrected := store._field_indices(lines, (0, child), "PRCorrTemp")) != 1 or lines[corrected[0]].split("=", 1)[1].strip() != "False":
        raise ValueError("The seed variant must explicitly declare PRCorrTemp=False")
    bounds = arrays[SEED_SUBARRAY]
    version, flags_before = _field_value(store, lines, bounds, "Version"), _field_value(store, lines, bounds, "Flags")
    strings_before, mppt_before = _field_value(store, lines, bounds, "NStringCh"), _field_value(store, lines, bounds, "NInvMPPT")
    module_before, inverter_before = _field_value(store, lines, bounds, "PVModule"), _field_value(store, lines, bounds, "GInverter")
    if _field_value(store, lines, bounds, "SubArrayId") != str(SEED_SUBARRAY):
        raise ValueError("The seed subarray identifier does not match its section")
    if version not in SEED_VERSIONS:
        raise ValueError(f"The seed subarray must declare Version={SEED_LABEL}")
    if not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags_before) or int(flags_before[1:], 16) & INDEPENDENT_MPPT_BIT:
        raise ValueError("The seed subarray must be in ordinary NInvMPPT=1 mode with explicit Flags")
    if mppt_before != "1":
        raise ValueError("The seed subarray must declare ordinary NInvMPPT=1")
    if _field_value(store, lines, bounds, "PowerSharingInverterId") != "-1":
        raise ValueError("The seed subarray must declare unshared PowerSharingInverterId=-1")
    block = root_block(lines, bounds)
    if _field_value(store, lines, bounds, "NStrOrient1") != strings_before or any(
        k.startswith("NStrOrient") and k != "NStrOrient1" and block.one(k)[1] != "0" for k in block.fields
    ):
        raise ValueError("The seed subarray must keep all strings in one orientation")
    store._ordinary_fixed_plane(lines, store._orientation(lines, orientation := positive_integer(block.one("NoOrientation")[1], "NoOrientation")))
    mppt_inputs = _mppt_inputs(ond_electrical)

    changes = _declare_explicit_counts(lines, SEED_SUBARRAY)
    mode = set_independent_mppt(lines, store._sections(lines)[0], SEED_SUBARRAY, True)
    scaled = resize_capacity(lines, store._sections(lines)[0], SEED_SUBARRAY, target_mppt=mppt_inputs)
    arrays, _ = store._sections(lines)
    if (checked := grid_inventory(lines, arrays))["counts_valid"] is not True:
        raise ValueError("The derived seed has incomplete or inconsistent grid counts: " + "; ".join(checked["issues"] + checked["unchecked_checks"]))
    row = next(item for item in checked["subarrays"] if item["id"] == SEED_SUBARRAY)
    if row["independent_mppt_inputs_declared"] != mppt_inputs or row["explicit_mppt_inputs_match_declared"] is not True:
        raise ValueError("The derived seed does not declare the target OND input count exactly")
    flags_after, strings_after = _field_value(store, lines, arrays[SEED_SUBARRAY], "Flags"), _field_value(store, lines, arrays[SEED_SUBARRAY], "NStringCh")
    port = (scaled["mppt_groups"] or [{}])[0]

    body = "".join(lines)
    data = (b"\xef\xbb\xbf" if variant_bom or not body.isascii() else b"") + body.encode("utf-8")
    store._require_no_geometry(store._text(data)[1], require_shading_section=True)
    _, final_lines = store._text(final := store._replace(data, {"PAN": pan_name, "OND": ond_name}, SEED_SUBARRAY))
    block, plan = _verify_and_plan_seed_variant(
        store, final_lines, pan_name=pan_name, pan_fields=pan_fields, ond_electrical=ond_electrical, capacity=capacity,
        orientation_id=orientation, count_error_prefix="The derived seed lost complete counts after the component references: ",
    )

    changes.extend([
        {"object": "pvSubArray", "field": "Flags", "before": flags_before, "after": flags_after, "reason": "Switch to the verified independent-MPPT mode"},
        {"object": "pvSubArray", "field": "NInvMPPT", "before": mode["inputs_before"], "after": mode["inputs_after"], "reason": "Declare the exclusive explicit MPPT inputs"},
        {"object": "pvSubArray", "field": "NInvMPPT", "before": scaled["mppt_inputs_before"], "after": scaled["mppt_inputs_after"], "reason": "Populate every declared hardware MPPT input"},
        {"object": "pvSubArray", "field": "NStringCh", "before": scaled["strings_before"], "after": scaled["strings_after"], "reason": "Keep the proportional string allocation integral"},
        {"object": "pvSubArray", "field": "NStrOrient1", "before": strings_before, "after": strings_after, "reason": "All strings stay in the common fixed plane"},
        {"object": "pvCircuit", "field": "StringNode NElements", "before": 1, "after": 1, "reason": "Parallel-string leaves stay a single explicit serialized branch"},
        {"object": "pvCircuit", "field": "MPPTNode NElements", "before": port.get("before"), "after": port.get("after"), "reason": "Scale the repeatable MPPT branch to the declared input count"},
        {"object": "pvSubArray", "field": "PVModule/GInverter", "before": f"{module_before}/{inverter_before}", "after": f"{pan_name}/{ond_name}", "reason": "Reference the resolved loose components"},
    ])
    retained = [{"field": "PRCorrTemp", "value": "False", "reason": "Standard annual-PR requirement is retained from the seed"},
                *({"field": k, "value": block.one(k)[1], "reason": "Retained from the demo seed; no loss or native default was fitted"} for k in RETAINED_FIELDS if k in block.fields)]
    shading = [i for i, ln in enumerate(final_lines) if ln.strip() == "PVObject_Ombrage=pvShading"]
    shading_end = next((i for i in range(shading[0] + 1, len(final_lines)) if final_lines[i].strip() == "End of PVObject pvShading"), None) if shading else None
    if shading_end is not None:
        retained.append({"field": "pvShading Flags", "value": _field_value(store, final_lines, (shading[0], shading_end), "Flags"), "reason": "The empty shading declaration is retained; no geometry was added"})
    return {
        "prj_text": project_data.decode("utf-8"), "vc0_text": final.decode("utf-8"), "profile": profile, "mppt_inputs": mppt_inputs, "capacity_plan": plan,
        "source": {"library": str(seed_project.parent), "project": seed_project.name, "variant": seed_variant.name, "subarray": SEED_SUBARRAY, "orientation": orientation,
                   "version": version, "flags_before": flags_before, "flags_after": flags_after, "mppt_inputs_before": int(mppt_before),
                   "strings_before": strings_before, "strings_after": strings_after, "project_bom_preserved": project_bom, "variant_bom_preserved": variant_bom},
        "changes": changes, "retained_defaults": retained, "read_only_seed": True,
        "warnings": [
            "The seed variant's historical BatchSimul ParamsFileName reference is retained; this preparation passes no batch parameters and that reference's effect is not verified.",
            "The PRJ Comment, MeteoFileName and the embedded site/meteo snapshot come from the installed demo seed; the destination workspace is not checked for that MET file here.",
            "Explicit NElements=1 rows only declare counts PVsyst already serialized as implicit 1 on the one exclusive inverter/string branch; no physical object, loss or native default was added, removed or refitted.",
            "The derived texts are validated through the adapter's own parsers and count checks; native PVsyst structural and simulation validation remains outstanding.",
        ],
    }


def synthesized_seed_texts(cli, store, *, ond_electrical, pan_name, ond_name, capacity=None,
                           pan_fields=None, site_name=None, met_name=None, allow_window_creation=None) -> dict:
    """Build a derived PRJ/VC0 pair purely in memory via project/synthesize.py (zero DataRO seed)."""
    profile, mppt_inputs = _cli_profile(cli, allow_window_creation=allow_window_creation), _mppt_inputs(ond_electrical)
    _component_path(store, "PAN", pan_name)
    _component_path(store, "OND", ond_name)
    pnom_ac, pnom_field = 7.5, (ond_electrical.get("fields") or {}).get("PNomConv")
    if isinstance(pnom_field, dict) and pnom_field.get("status") == "declared":
        try:
            if (val := float(pnom_field["value"])) > 0:
                pnom_ac = val
        except (TypeError, ValueError):
            pass

    synth = synthesize_project_and_variant(
        store=store if (site_name or met_name) else None, project_comment="Synthesized Grid Template", variant_comment="First simulation",
        site_name=site_name, met_name=met_name, pan_name=pan_name, ond_name=ond_name, n_mod_serie=15, n_string_ch=mppt_inputs,
        n_inverter=1, n_inv_mppt=mppt_inputs, independent_mppt=True, pnom_ac_in_mppt=pnom_ac / mppt_inputs, tilt_deg=25.0, azimuth_deg=0.0, with_bom=True,
    )
    store._project_text(synth["prj_text"].encode("utf-8"))
    _, final_lines = store._text(synth["vc0_text"].encode("utf-8"))
    block, plan = _verify_and_plan_seed_variant(
        store, final_lines, pan_name=pan_name, pan_fields=pan_fields, ond_electrical=ond_electrical, capacity=capacity,
        orientation_id=1, count_error_prefix="The synthesized template has incomplete or inconsistent grid counts: ",
    )
    changes = [
        {"object": "pvProject/pvVCalcul", "field": "synthesis", "before": None, "after": SYNTH_VERSION, "reason": "Synthesized pure-text PRJ/VC0 directly without any DataRO demo seed"},
        {"object": "pvSubArray", "field": "NInvMPPT", "before": None, "after": mppt_inputs, "reason": "Populate every declared hardware MPPT input"},
        {"object": "pvSubArray", "field": "PVModule/GInverter", "before": None, "after": f"{pan_name}/{ond_name}", "reason": "Reference the resolved loose components"},
    ]
    retained = [{"field": "PRCorrTemp", "value": "False", "reason": "Standard annual-PR requirement declared on synthesized root"},
                *({"field": k, "value": block.one(k)[1], "reason": "Standard PVsyst 8.1 grid default emitted by pure-text synthesizer"} for k in RETAINED_FIELDS if k in block.fields)]
    return {
        "prj_text": synth["prj_text"], "vc0_text": synth["vc0_text"], "profile": profile, "mppt_inputs": mppt_inputs, "capacity_plan": plan,
        "source": {"mode": "synthesized", "library": None, "project": None, "variant": None, "subarray": SEED_SUBARRAY, "orientation": 1,
                   "version": SYNTH_VERSION, "flags_before": None, "flags_after": "$001E0402", "mppt_inputs_before": None,
                   "strings_before": None, "strings_after": str(mppt_inputs), "project_bom_preserved": True, "variant_bom_preserved": True},
        "changes": changes, "retained_defaults": retained, "read_only_seed": False,
        "warnings": [
            "PRJ/VC0 were synthesized directly in pure text without reading any DataRO _DEMO_* seed files.",
            "The synthesized texts are validated through the adapter's own parsers and count checks; native PVsyst structural and simulation validation remains outstanding.",
        ],
    }


def prepare_standard_template(cli, store, *, ond_electrical, pan_name, ond_name, new_project,
                              dry_run=True, capacity=None, pan_fields=None,
                              allow_window_creation=None, allow_synthesize=False) -> dict:
    """Create a new workspace PRJ/VC0 from pure-text synthesis (when ``allow_synthesize=True``) or the 8.1 demo seed."""
    with store.lock:
        return _prepare(cli, store, ond_electrical, pan_name, ond_name, new_project, dry_run, capacity, pan_fields, allow_window_creation, allow_synthesize=allow_synthesize)


def _prepare(cli, store, ond_electrical, pan_name, ond_name, new_project, dry_run, capacity, pan_fields,
             allow_window_creation=None, *, allow_synthesize=False):
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run,
              "request": {"project": new_project, "module": pan_name, "inverter": ond_name, "capacity": capacity is not None},
              "source": {}, "definition": {}, "changes": [], "retained_defaults": [], "issues": [], "warnings": [],
              "created": None, "capacity_plan": None, "seed_preserved": None, "native_verification_required": True}
    if type(dry_run) is not bool:
        _issue(result, "dry_run", "Use a strict boolean")
    for field, value in (("new_project", new_project), ("pan_name", pan_name), ("ond_name", ond_name)):
        if not isinstance(value, str) or not value:
            _issue(result, field, "Required input is missing")
    if result["issues"]:
        return result
    try:
        project_path(cli.workspace, new_project)
        if Path(store.workspace) != Path(cli.workspace):
            raise ValueError("The store and the CLI must share one resolved workspace")
        if any(store._belongs_to_project(path, new_project) for path in _workspace_entries(store)):
            raise FileExistsError("The destination project already exists; no file is overwritten")
    except (ValueError, OSError) as exc:
        _issue(result, "new_project", exc)
        return result
    try:
        derived = (synthesized_seed_texts if allow_synthesize else standard_seed_texts)(
            cli, store, ond_electrical=ond_electrical, pan_name=pan_name, ond_name=ond_name,
            capacity=capacity, pan_fields=pan_fields, allow_window_creation=allow_window_creation,
        )
    except (ValueError, OSError, KeyError, PVsystError, StopIteration) as exc:
        _issue(result, "seed", cli._redact(str(exc)))
        return result
    result.update({
        "stage": "derived", "source": derived["source"], "profile": derived["profile"], "mppt_inputs": derived["mppt_inputs"],
        "capacity_plan": derived["capacity_plan"], "changes": derived["changes"], "retained_defaults": derived["retained_defaults"],
        "warnings": list(derived["warnings"]),
        "definition": {"variant": SEED_VARIANT, "subarray": SEED_SUBARRAY, "mppt_inputs": derived["mppt_inputs"],
                       "strings": derived["source"]["strings_after"], "changes": len(derived["changes"]), "retained_fields": len(derived["retained_defaults"])},
    })
    if capacity is not None and derived["capacity_plan"]["status"] != "ready":
        result["issues"].extend(derived["capacity_plan"]["issues"])
        result["stage"] = "capacity"
        return result
    if dry_run:
        result["status"] = "ready"
        return result
    result["stage"] = "create"
    try:
        created = store.create_project(new_project, derived["prj_text"], {SEED_VARIANT: derived["vc0_text"]})
    except FileExistsError as exc:
        _issue(result, "new_project", f"{exc}; no file was overwritten")
        result["stage"] = "conflict"
        return result
    except (ValueError, OSError) as exc:
        result.update(status="failed", error=cli._redact(str(exc))[-12000:])
        return result
    result.update(status="created", stage="created", created=created, files=created["files"], template_project=created["project"], variant=SEED_VARIANT)
    return result


def _validate_ground_array_modules(*, module_rows: int, module_columns: int, module_width_m: float, module_length_m: float,
                                   module_name: str, module_orient: str, spacing_x_m: float, spacing_y_m: float) -> tuple[int, int, float, float, float, float, str]:
    n_rows, n_cols = _pos_int(module_rows, "module_rows"), _pos_int(module_columns, "module_columns")
    mw, ml = _finite(module_width_m, "module_width_m", minimum=0.1), _finite(module_length_m, "module_length_m", minimum=0.1)
    sx, sy = _finite(spacing_x_m, "spacing_x_m", minimum=0.0), _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    if module_orient not in ("moPortrait", "moLandscape"):
        raise ValueError("module_orient must be 'moPortrait' or 'moLandscape'")
    return n_rows, n_cols, mw, ml, sx, sy, _clean_text(module_name, "module_name")


def _synthesize_ground_array_shading(
    *, obj_type: str, shading_comment: str, obj_comment: str, n_elems: int, origin: tuple[float, float, float],
    tilt_deg: float, base_slope_deg: float, azimuth_deg: float, orientation_id: int, name: str, mw: float, ml: float,
    n_rows: int, n_cols: int, module_orient: str, sx: float, sy: float, dx_module: float, dy_module: float,
    long_shed: float, larg_shed: float, pitch_ns: float, pitch_ew: float, version: str, extra_lines: list[str] = (),
) -> list[str]:
    return [
        *pv_shading_header(shading_comment, 1), f"      PVObject_1={obj_type}", f"        Comment={obj_comment}",
        f"        Version={version}", "        Flags=$02200074", "        NoObjCreated=1",
        "        ColorTechn=$00FF0000", "        ColorSurf=$00FF9999", "        ColorBord=$00505050",
        f"        DOrigine={origin[0]:.6f},{origin[1]:.6f},{origin[2]:.6f}", f"        DInclin={tilt_deg:.6f}",
        f"        BaseSlope={base_slope_deg:.6f}", f"        DAzimut={azimuth_deg:.6f}", "        Source=osPVsyst",
        f"      NElemChamps={n_elems}", f"      NoOrient={_pos_int(orientation_id, 'orientation_id')}",
        f"      PanneauPVName={name};{mw:.3f};{ml:.3f}", f"      ModSpacingX={sx:.3f}", f"      ModSpacingY={sy:.3f}",
        f"      NModInHeight={n_rows}", f"      NModInWidth={n_cols}", f"      ModuleOrient={module_orient}",
        f"      DXModule={dx_module:.3f}", f"      DYModule={dy_module:.3f}",
        f"        LongShed={long_shed:.6f}", f"        LargShed={larg_shed:.6f}", "        Cadre=0.000,0.000,0.000,0.000,",
        "        TypeOrigine=2", f"        PitchNS={pitch_ns:.3f}", f"        PitchEW={pitch_ew:.3f}",
        *extra_lines, f"      End of PVObject {obj_type}", "    End of ListeObjets", "  End of PVObject pvShading",
    ]


def synthesize_ground_shed_shading(*, shed_count: int = 10, pitch_m: float = 10.0, tilt_deg: float = 20.0, azimuth_deg: float = 0.0,
                                   base_slope_deg: float = 0.0, module_rows: int = 2, module_columns: int = 25,
                                   module_width_m: float = 1.052, module_length_m: float = 2.131, module_name: str = "PV Module",
                                   module_orient: str = "moPortrait", spacing_x_m: float = 0.020, spacing_y_m: float = 0.020,
                                   origin: tuple[float, float, float] = (0.0, 0.0, 0.0), orientation_id: int = 1,
                                   version: str = SYNTH_VERSION) -> list[str]:
    """Synthesize a complete 3D PVObject_Ombrage=pvShading containing a pvShdArrayShed field."""
    n_sheds, pitch = _pos_int(shed_count, "shed_count"), _finite(pitch_m, "pitch_m", minimum=0.1)
    tilt = _finite(tilt_deg, "tilt_deg", minimum=0.0, maximum=90.0)
    azim = _finite(azimuth_deg, "azimuth_deg", minimum=-180.0, maximum=180.0)
    slope = _finite(base_slope_deg, "base_slope_deg", minimum=-45.0, maximum=45.0)
    n_rows, n_cols, mw, ml, sx, sy, name = _validate_ground_array_modules(
        module_rows=module_rows, module_columns=module_columns, module_width_m=module_width_m, module_length_m=module_length_m,
        module_name=module_name, module_orient=module_orient, spacing_x_m=spacing_x_m, spacing_y_m=spacing_y_m,
    )
    cell_w, cell_h = (mw, ml) if module_orient == "moPortrait" else (ml, mw)
    larg_shed, long_shed = n_rows * cell_h + max(0, n_rows - 1) * sy, n_cols * cell_w + max(0, n_cols - 1) * sx
    return _synthesize_ground_array_shading(
        obj_type="pvShdArrayShed", shading_comment="Ground shed 3D shading scene", obj_comment="Shed field #1",
        n_elems=n_sheds, origin=origin, tilt_deg=tilt, base_slope_deg=slope, azimuth_deg=azim, orientation_id=orientation_id,
        name=name, mw=mw, ml=ml, n_rows=n_rows, n_cols=n_cols, module_orient=module_orient, sx=sx, sy=sy,
        dx_module=cell_w + sx, dy_module=cell_h + sy, long_shed=long_shed, larg_shed=larg_shed, pitch_ns=pitch, pitch_ew=0.0, version=version,
    )


def synthesize_ground_tracker_shading(*, tracker_count: int = 10, pitch_ew_m: float = 12.0, axis_azimuth_deg: float = 0.0,
                                      module_rows: int = 2, module_columns: int = 25, module_width_m: float = 1.052,
                                      module_length_m: float = 2.131, module_name: str = "PV Module", module_orient: str = "moPortrait",
                                      spacing_x_m: float = 0.020, spacing_y_m: float = 0.020, origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
                                      orientation_id: int = 1, version: str = SYNTH_VERSION) -> list[str]:
    """Synthesize a complete 3D PVObject_Ombrage=pvShading containing a pvShdArrayTracking field."""
    n_trk, pitch = _pos_int(tracker_count, "tracker_count"), _finite(pitch_ew_m, "pitch_ew_m", minimum=0.1)
    azim = _finite(axis_azimuth_deg, "axis_azimuth_deg", minimum=-180.0, maximum=180.0)
    n_rows, n_cols, mw, ml, sx, sy, name = _validate_ground_array_modules(
        module_rows=module_rows, module_columns=module_columns, module_width_m=module_width_m, module_length_m=module_length_m,
        module_name=module_name, module_orient=module_orient, spacing_x_m=spacing_x_m, spacing_y_m=spacing_y_m,
    )
    chord_mod, length_mod = (ml, mw) if module_orient == "moPortrait" else (mw, ml)
    long_shed, larg_shed = n_rows * chord_mod + max(0, n_rows - 1) * sy, n_cols * length_mod + max(0, n_cols - 1) * sx
    return _synthesize_ground_array_shading(
        obj_type="pvShdArrayTracking", shading_comment="Ground tracker 3D shading scene", obj_comment="Array of trackers #1",
        n_elems=n_trk, origin=origin, tilt_deg=0.0, base_slope_deg=0.0, azimuth_deg=azim, orientation_id=orientation_id,
        name=name, mw=mw, ml=ml, n_rows=n_rows, n_cols=n_cols, module_orient=module_orient, sx=sx, sy=sy,
        dx_module=chord_mod + sy, dy_module=larg_shed, long_shed=long_shed, larg_shed=larg_shed, pitch_ns=0.0, pitch_ew=pitch,
        version=version, extra_lines=["        AxisOrig=2"],
    )


def _resolve_pan_module_dimensions(store, pan_name: str) -> dict | None:
    """Auto-extract (width_m, length_m, model_name) from a readable workspace/builtin .PAN file."""
    components = getattr(store, "components", None) if store is not None else None
    if components is None:
        return None
    for library in ("workspace", "builtin"):
        if library == "builtin" and getattr(components, "builtin", None) is None:
            continue
        try:
            if not (path := components._path("PAN", pan_name, library)).is_file():
                continue
            info = components._inspect("PAN", raw := components._read(path))
            if info.get("format") != "text" or info.get("structural_errors") or info.get("known_value_errors"):
                continue
            fields = pan_validation_fields(components._text("PAN", raw)[1])
            widths, heights = fields.get("Width") or fields.get("LargApp") or [], fields.get("Height") or fields.get("LongApp") or []
            if len(widths) != 1 or len(heights) != 1:
                continue
            width_m, length_m = float(widths[0]), float(heights[0])
            if not (math.isfinite(width_m) and width_m > 0 and math.isfinite(length_m) and length_m > 0):
                continue
            models = fields.get("Model") or []
            return {"width_m": width_m, "length_m": length_m, "model_name": models[0].strip() if len(models) == 1 and models[0].strip() else None,
                    "library": library, "path": str(path)}
        except (ValueError, OSError, KeyError):
            continue
    return None


def synthesize_utility_project(
    store, *, project_comment: str = "Utility PV Project", variant_comment: str = "Utility Variant",
    site_name: str, met_name: str, field_type: str = "Sheds", pan_name: str = "Generic_Mono_440W_Half_Bifacial.PAN",
    ond_name: str = "Generic_50kW.OND", n_mod_serie: int = 20, n_string_ch: int = 6, n_inverter: int = 1,
    n_inv_mppt: int = 1, independent_mppt: bool = False, pnom_ac_in_mppt: float = 50.0, tilt_deg: float = 20.0,
    azimuth_deg: float = 0.0, axis_azimuth_deg: float = 0.0, min_phi_deg: float = -60.0, max_phi_deg: float = 60.0,
    backtracking: bool = True, irrad_optimiz: bool = False, central_gap_m: float = 0.0, pitch_slope_deg: float = 0.0,
    wind_stow: bool = False, wind_stow_speed_ms: float = 12.0, wind_stow_position_deg: float = 0.0, pitch_m: float = 10.0,
    collector_width_m: float | None = None, shed_count: int = 4, inactive_band_width_1_m: float | None = None,
    inactive_band_width_2_m: float | None = None, electrical_effect: bool | None = None, shading_factor: float | None = None,
    cell_width_m: float | None = None, module_rows: int = 2, module_columns: int = 15, module_orient: str = "moPortrait",
    spacing_x_m: float = 0.020, spacing_y_m: float = 0.020, module_width_m: float | None = None, module_length_m: float | None = None,
    module_name: str | None = None, bifacial: dict | None = None, include_3d_scene: bool | None = None,
    site_polygon: list[list[float]] | None = None, setback_m: float = 0.0, ground_clearance_m: float = 0.5,
    max_table_columns: int | None = None, obstacles: list[dict] | None = None, base_slope_deg: float = 0.0,
    terrain: dict | None = None, u_factor: float = 29.0, lid_loss: float = 0.0, pmism_mpp: float | None = None,
    module_quality_loss: float | None = None, dc_wiring_loss_pct: float = 1.5, dc_wiring_resistance_mohm: float = 0.0,
    sub_orientations: list[dict] | None = None, top_spacing_m: float = 0.2, dome_wiring: str = "auto",
    allow_missing_met: bool = False, version: str = SYNTH_VERSION, with_bom: bool = True,
) -> dict:
    """Zero-template synthesizer for utility ground sheds, east-west domes, or single-axis trackers."""
    if field_type not in ("Sheds", "UnlimSheds", "RegularSheds", "UnlimTracking", "TrackHorizNSAxis", "Dome"):
        raise ValueError(f"Unsupported utility field_type: {field_type!r}")
    if module_orient not in ("moPortrait", "moLandscape"):
        raise ValueError("module_orient must be 'moPortrait' or 'moLandscape'")

    pan_dims = _resolve_pan_module_dimensions(store, pan_name)
    if module_width_m is not None and module_length_m is not None:
        mw, ml, dim_source = _finite(module_width_m, "module_width_m", minimum=0.1), _finite(module_length_m, "module_length_m", minimum=0.1), "caller"
    elif pan_dims is not None:
        mw = _finite(module_width_m, "module_width_m", minimum=0.1) if module_width_m is not None else pan_dims["width_m"]
        ml = _finite(module_length_m, "module_length_m", minimum=0.1) if module_length_m is not None else pan_dims["length_m"]
        dim_source = f"pan:{pan_dims['library']}"
    else:
        mw = _finite(module_width_m, "module_width_m", minimum=0.1) if module_width_m is not None else 1.052
        ml = _finite(module_length_m, "module_length_m", minimum=0.1) if module_length_m is not None else 2.131
        dim_source = "default"
    resolved_mod_name = module_name or (pan_dims["model_name"] if pan_dims and pan_dims.get("model_name") else None) or "Mono 440 Wp Twin 144 half-cells bifacial"

    n_rows, n_cols = _pos_int(module_rows, "module_rows"), _pos_int(module_columns, "module_columns")
    sx, sy = _finite(spacing_x_m, "spacing_x_m", minimum=0.0), _finite(spacing_y_m, "spacing_y_m", minimum=0.0)
    need_3d = include_3d_scene if include_3d_scene is not None else (field_type in ("RegularSheds", "TrackHorizNSAxis", "Dome") or site_polygon is not None or bool(obstacles))
    if field_type == "Dome" and not need_3d:
        raise ValueError("East-west dome layout requires include_3d_scene=True")
    if (site_polygon is not None or bool(obstacles)) and not need_3d:
        raise ValueError("site_polygon and obstacles require include_3d_scene=True")
    chord_mod = ml if module_orient == "moPortrait" else mw
    scene_collector_width = round(n_rows * chord_mod + max(0, n_rows - 1) * sy, 3)
    if need_3d:
        if collector_width_m is not None and abs((override_w := _finite(collector_width_m, "collector_width_m", minimum=0.1)) - scene_collector_width) > 1e-3:
            raise ValueError(f"collector_width_m ({override_w:.3f}) disagrees with the 3D scene chord width ({scene_collector_width:.3f}); omit collector_width_m or match the scene dimensions")
        resolved_collector_width = scene_collector_width
    else:
        resolved_collector_width = _finite(collector_width_m, "collector_width_m", minimum=0.1) if collector_width_m is not None else scene_collector_width
    resolved_pitch = _finite(pitch_m, "pitch_m", minimum=0.1)
    if resolved_pitch <= resolved_collector_width:
        raise ValueError(f"pitch_m ({resolved_pitch:.3f}) must exceed collector_width_m ({resolved_collector_width:.3f}) so adjacent ground rows do not overlap (GCR < 1.0)")
    resolved_top_spacing = _finite(top_spacing_m, "top_spacing_m", minimum=0.0)
    if field_type == "Dome":
        from pvsyst_auto.scene.dome import dome_footprint_depth_m
        dome_footprint = dome_footprint_depth_m(resolved_collector_width, tilt_deg, resolved_top_spacing)
        if resolved_pitch <= dome_footprint:
            raise ValueError(f"pitch_m ({resolved_pitch:.3f} m) must exceed dome footprint depth ({dome_footprint:.3f} m)")

    eff_base_slope = _finite(base_slope_deg, "base_slope_deg", minimum=-45.0, maximum=45.0)
    opt_orient_fields = {"inactive_band_width_1_m": inactive_band_width_1_m, "inactive_band_width_2_m": inactive_band_width_2_m,
                         "electrical_effect": electrical_effect, "shading_factor": shading_factor, "cell_width_m": cell_width_m}
    orient_spec = {
        "id": 1, "field_type": field_type, "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg, "base_slope_deg": eff_base_slope,
        "axis_azimuth_deg": axis_azimuth_deg, "min_phi_deg": min_phi_deg, "max_phi_deg": max_phi_deg, "backtracking": backtracking,
        "irrad_optimiz": irrad_optimiz, "central_gap_m": central_gap_m, "pitch_slope_deg": pitch_slope_deg, "wind_stow": wind_stow,
        "wind_stow_speed_ms": wind_stow_speed_ms, "wind_stow_position_deg": wind_stow_position_deg, "pitch_m": resolved_pitch,
        "collector_width_m": resolved_collector_width, "shed_count": shed_count,
        "module_columns": n_rows if field_type in ("Sheds", "UnlimSheds", "UnlimTracking") else n_cols, "bifacial": bifacial,
        **{k: v for k, v in opt_orient_fields.items() if v is not None},
    }

    shading_lines = ground_3d_layout = terrain_summary = terrain_mesh = None
    if terrain is not None:
        from pvsyst_auto.scene.terrain import build_terrain_mesh
        terrain_mesh = build_terrain_mesh(terrain, workspace=store.workspace)
        terrain_summary, need_3d = terrain_mesh.summary(), True
    if need_3d:
        mod_kw = {"module_rows": n_rows, "module_columns": n_cols, "module_width_m": mw, "module_length_m": ml,
                  "module_name": resolved_mod_name, "module_orient": module_orient, "spacing_x_m": sx, "spacing_y_m": sy, "version": version}
        if field_type == "Dome":
            from pvsyst_auto.scene.dome import build_dome_3d_shading, synthesize_ground_dome_shading
            if site_polygon is not None or terrain_mesh is not None:
                shading_lines, ground_3d_layout = build_dome_3d_shading(
                    site_polygon=site_polygon, terrain_mesh=terrain_mesh, terrain_opts=terrain,
                    shed_count=shed_count, pitch_m=resolved_pitch, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg,
                    base_slope_deg=eff_base_slope, top_spacing_m=resolved_top_spacing,
                    setback_m=setback_m, ground_clearance_m=ground_clearance_m,
                    max_table_columns=max_table_columns, obstacles=obstacles, **mod_kw,
                )
            else:
                clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
                shading_lines = synthesize_ground_dome_shading(
                    shed_count=shed_count, pitch_m=resolved_pitch, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg,
                    base_slope_deg=eff_base_slope, top_spacing_m=resolved_top_spacing,
                    origin=(0.0, 0.0, clearance), obstacles=obstacles, **mod_kw,
                )
        elif field_type in ("TrackHorizNSAxis", "UnlimTracking"):
            if site_polygon is not None:
                from pvsyst_auto.scene.tracker_layout import (
                    build_tracker_3d_shading,
                    drape_tracker_tables_on_terrain,
                    plan_tracker_polygon_layout,
                )
                ground_3d_layout = plan_tracker_polygon_layout(
                    site_polygon, axis_azimuth_deg=axis_azimuth_deg, pitch_ew_m=resolved_pitch,
                    module_width_m=mw, module_length_m=ml, module_rows=n_rows,
                    module_orientation="landscape" if module_orient == "moLandscape" else "portrait",
                    spacing_x_m=sx, spacing_y_m=sy, setback_m=setback_m,
                    ground_clearance_m=ground_clearance_m, max_table_columns=max_table_columns, n_mod_serie=n_mod_serie,
                )
                if terrain_mesh is not None and bool((terrain or {}).get("drape_tables", True)):
                    draped_tables = drape_tracker_tables_on_terrain(
                        ground_3d_layout["tables"],
                        terrain_mesh,
                        ground_clearance_m=ground_clearance_m,
                        follow_slope=bool((terrain or {}).get("follow_slope", True)),
                        max_axis_tilt_deg=float((terrain or {}).get("max_axis_tilt_deg", 15.0)),
                    )
                    ground_3d_layout["tables"] = draped_tables
                    shading_lines, trk_terrain = build_tracker_3d_shading(
                        draped_tables, module_name=resolved_mod_name,
                        module_width_m=mw, module_length_m=ml, module_orient=module_orient,
                        spacing_x_m=sx, spacing_y_m=sy, ground_clearance_m=ground_clearance_m,
                        obstacles=obstacles, terrain_mesh=terrain_mesh,
                        terrain_opts={**(terrain or {}), "drape_tables": False}, version=version,
                    )
                else:
                    shading_lines, trk_terrain = build_tracker_3d_shading(
                        ground_3d_layout["tables"], module_name=resolved_mod_name,
                        module_width_m=mw, module_length_m=ml, module_orient=module_orient,
                        spacing_x_m=sx, spacing_y_m=sy, ground_clearance_m=ground_clearance_m,
                        obstacles=obstacles, terrain_mesh=terrain_mesh, terrain_opts=terrain, version=version,
                    )
                if trk_terrain is not None:
                    terrain_summary = trk_terrain
            elif terrain_mesh is not None:
                from pvsyst_auto.scene.terrain import build_terrain_tracker_shading
                shading_lines, terrain_summary = build_terrain_tracker_shading(
                    terrain_mesh, tracker_count=shed_count, pitch_ew_m=resolved_pitch, axis_azimuth_deg=axis_azimuth_deg,
                    ground_clearance_m=ground_clearance_m, follow_slope=bool(terrain.get("follow_slope", True)),
                    max_axis_tilt_deg=float(terrain.get("max_axis_tilt_deg", 15.0)), obstacles=obstacles, **mod_kw,
                )
            else:
                clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
                shading_lines = synthesize_ground_tracker_shading(
                    tracker_count=shed_count, pitch_ew_m=resolved_pitch, axis_azimuth_deg=axis_azimuth_deg,
                    origin=(0.0, 0.0, clearance), **mod_kw,
                )
                if obstacles:
                    from pvsyst_auto.scene.author import obstacle_profiles
                    from pvsyst_auto.scene.reader import emit_shading_obstacles
                    if parsed_obs := obstacle_profiles(obstacles):
                        extra_lines = emit_shading_obstacles(parsed_obs, start_index=2)
                        shading_lines = [(f"    ListeObjets, list of={1 + len(parsed_obs)} TShdObject" if ln.strip() == "ListeObjets, list of=1 TShdObject" else ln) for ln in shading_lines]
                        end_pos = next(i for i, ln in enumerate(shading_lines) if ln.strip() == "End of ListeObjets")
                        shading_lines[end_pos:end_pos] = extra_lines
        elif site_polygon is not None or terrain_mesh is not None:
            from pvsyst_auto.scene.reader import build_ground_3d_shading, plan_ground_polygon_layout
            if site_polygon is not None:
                ground_3d_layout = plan_ground_polygon_layout(
                    site_polygon, tilt_deg=tilt_deg, azimuth_deg=azimuth_deg, pitch_m=resolved_pitch, module_width_m=mw,
                    module_length_m=ml, module_rows=n_rows, module_orientation="landscape" if module_orient == "moLandscape" else "portrait",
                    spacing_x_m=sx, spacing_y_m=sy, setback_m=setback_m, ground_clearance_m=ground_clearance_m,
                    max_table_columns=max_table_columns, base_slope_deg=eff_base_slope,
                )
                tables_3d = ground_3d_layout["tables"]
            else:
                assert terrain_mesh is not None
                dim_w, dim_h = (ml, mw) if module_orient == "moLandscape" else (mw, ml)
                long_s, larg_s = n_cols * dim_w + max(0, n_cols - 1) * sx, n_rows * dim_h + max(0, n_rows - 1) * sy
                cx0 = 0.5 * (terrain_mesh.bounds["min_x"] + terrain_mesh.bounds["max_x"])
                cy0 = 0.5 * (terrain_mesh.bounds["min_y"] + terrain_mesh.bounds["max_y"])
                az_rad, start_off = math.radians(azimuth_deg), -0.5 * (shed_count - 1) * resolved_pitch
                step_x, step_y = math.sin(az_rad), math.cos(az_rad)
                tables_3d = [{
                    "name": f"TerrainShed_{r + 1:02d}", "row_index": r + 1, "module_rows": n_rows, "module_columns": n_cols,
                    "module_count": n_rows * n_cols, "long_shed_m": round(long_s, 6), "larg_shed_m": round(larg_s, 6),
                    "origin_m": [round(cx0 + (start_off + r * resolved_pitch) * step_x, 6),
                                 round(cy0 + (start_off + r * resolved_pitch) * step_y, 6), round(ground_clearance_m, 6)],
                    "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg, "base_slope_deg": eff_base_slope,
                } for r in range(shed_count)]
            if terrain_mesh is not None and bool(terrain.get("drape_tables", True)):
                from pvsyst_auto.scene.terrain import drape_shed_tables_on_terrain
                tables_3d = drape_shed_tables_on_terrain(tables_3d, terrain_mesh, ground_clearance_m=ground_clearance_m,
                                                         follow_slope=bool(terrain.get("follow_slope", True)), max_base_slope_deg=float(terrain.get("max_base_slope_deg", 20.0)))
                if ground_3d_layout is not None:
                    ground_3d_layout["tables"] = tables_3d
            shading_lines = build_ground_3d_shading(tables_3d, module_name=resolved_mod_name, module_width_m=mw, module_length_m=ml,
                                                    module_orient=module_orient, spacing_x_m=sx, spacing_y_m=sy,
                                                    obstacles=obstacles, terrain_mesh=terrain_mesh, version=version)
        else:
            clearance = _finite(ground_clearance_m, "ground_clearance_m", minimum=0.0)
            shading_lines = synthesize_ground_shed_shading(shed_count=shed_count, pitch_m=resolved_pitch, tilt_deg=tilt_deg,
                                                           azimuth_deg=azimuth_deg, base_slope_deg=eff_base_slope,
                                                           origin=(0.0, 0.0, clearance), **mod_kw)
            if obstacles:
                from pvsyst_auto.scene.author import obstacle_profiles
                from pvsyst_auto.scene.reader import emit_shading_obstacles
                if parsed_obs := obstacle_profiles(obstacles):
                    extra_lines = emit_shading_obstacles(parsed_obs, start_index=2)
                    shading_lines = [(f"    ListeObjets, list of={1 + len(parsed_obs)} TShdObject" if ln.strip() == "ListeObjets, list of=1 TShdObject" else ln) for ln in shading_lines]
                    end_pos = next(i for i, ln in enumerate(shading_lines) if ln.strip() == "End of ListeObjets")
                    shading_lines[end_pos:end_pos] = extra_lines

    def _subarray_entry(sub_id: int, comment: str, n_mod: int, n_str: int, n_inv: int, n_mppt: int) -> dict:
        return {
            "subarray_id": sub_id, "orientation_id": sub_id, "comment": comment, "pan_name": pan_name, "ond_name": ond_name,
            "n_mod_serie": n_mod, "n_string_ch": n_str, "n_inverter": n_inv, "n_inv_mppt": n_mppt,
            "independent_mppt": independent_mppt, "pnom_ac_in_mppt": pnom_ac_in_mppt, "lid_loss": lid_loss,
            **({"pmism_mpp": pmism_mpp} if pmism_mpp is not None else {}),
            **({"module_quality_loss": module_quality_loss} if module_quality_loss is not None else {}),
            "dc_wiring_loss_pct": dc_wiring_loss_pct, "dc_wiring_resistance_mohm": dc_wiring_resistance_mohm,
        }

    sub_orient_summaries: list[dict] | None = None
    dome_meta: dict | None = None
    if field_type == "Dome":
        if bifacial is not None and bifacial.get("enabled", True):
            raise ValueError("Bifacial 2D model is not supported for field_type='Dome'")
        from pvsyst_auto.scene.dome import dome_footprint_depth_m, opposite_azimuth_deg, resolve_dome_subarrays
        back_azim = opposite_azimuth_deg(azimuth_deg)
        orientations_list = [
            {"id": 1, "field_type": "Dome", "pair_orientation_id": 2, "tilt_deg": tilt_deg, "azimuth_deg": azimuth_deg, "base_slope_deg": eff_base_slope},
            {"id": 2, "field_type": "DomeBackfaces", "pair_orientation_id": 1, "tilt_deg": tilt_deg, "azimuth_deg": back_azim, "base_slope_deg": eff_base_slope},
        ]
        subarrays, effective_dome_wiring = resolve_dome_subarrays(
            dome_wiring=dome_wiring, pan_name=pan_name, ond_name=ond_name,
            n_mod_serie=n_mod_serie, n_string_ch=n_string_ch, n_inverter=n_inverter,
            n_inv_mppt=n_inv_mppt, pnom_ac_in_mppt=pnom_ac_in_mppt,
            lid_loss=lid_loss, pmism_mpp=pmism_mpp, module_quality_loss=module_quality_loss,
            dc_wiring_loss_pct=dc_wiring_loss_pct, dc_wiring_resistance_mohm=dc_wiring_resistance_mohm,
        )
        dome_meta = {
            "dome_wiring": effective_dome_wiring,
            "top_spacing_m": round(resolved_top_spacing, 3),
            "back_azimuth_deg": back_azim,
            "dome_footprint_depth_m": round(dome_footprint_depth_m(resolved_collector_width, tilt_deg, resolved_top_spacing), 4),
        }
    elif sub_orientations:
        if need_3d or field_type not in ("Sheds", "UnlimSheds"):
            raise ValueError("sub_orientations is only supported for unlimited ground_sheds (FieldType='Sheds')")
        orientations_list, subarrays, sub_orient_summaries = [], [], []
        total_sub_mods = sum(int(item["n_mod_serie"]) * int(item["n_string_ch"]) for item in sub_orientations)
        for idx_o, item in enumerate(sub_orientations, start=1):
            pfx = f"sub_orientations[{idx_o - 1}]"
            sub_pitch = _finite(item.get("pitch_m", resolved_pitch), f"{pfx}.pitch_m", minimum=0.1)
            sub_cw = _finite(item.get("collector_width_m", resolved_collector_width), f"{pfx}.collector_width_m", minimum=0.1)
            if sub_pitch <= sub_cw:
                raise ValueError(f"{pfx}: pitch_m ({sub_pitch:.3f}) must exceed collector_width_m ({sub_cw:.3f}) so adjacent ground rows do not overlap (GCR < 1.0)")
            sub_tilt = _finite(item["tilt_deg"], f"{pfx}.tilt_deg", minimum=0.0, maximum=90.0)
            sub_azim = _finite(item["azimuth_deg"], f"{pfx}.azimuth_deg", minimum=-180.0, maximum=180.0)
            sub_sheds = _pos_int(item.get("shed_count", shed_count), f"{pfx}.shed_count")
            sub_slope = _finite(item.get("base_slope_deg", eff_base_slope), f"{pfx}.base_slope_deg", minimum=-45.0, maximum=45.0)
            sub_label = _clean_text(item.get("label") or f"Sub-array #{idx_o} (Tilt {_fmt_angle(sub_tilt)}, Azim {_fmt_angle(sub_azim)})", f"{pfx}.label")
            orientations_list.append({
                "id": idx_o, "field_type": field_type, "tilt_deg": sub_tilt, "azimuth_deg": sub_azim, "base_slope_deg": sub_slope,
                "comment": f"{sub_label}, Tilt {_fmt_angle(sub_tilt)} deg, Azim. {_fmt_angle(sub_azim)} deg",
                "pitch_m": sub_pitch, "collector_width_m": sub_cw, "shed_count": sub_sheds, "module_columns": n_rows, "bifacial": bifacial,
                **{k: v for k, dv in opt_orient_fields.items() if (v := item.get(k, dv)) is not None},
            })
            snm, sns, sni, snp = _pos_int(item["n_mod_serie"], f"{pfx}.n_mod_serie"), _pos_int(item["n_string_ch"], f"{pfx}.n_string_ch"), _pos_int(item["n_inverter"], f"{pfx}.n_inverter"), _pos_int(item["n_inv_mppt"], f"{pfx}.n_inv_mppt")
            subarrays.append(_subarray_entry(idx_o, sub_label, snm, sns, sni, snp))
            sub_mods = snm * sns
            sub_orient_summaries.append({
                "orientation_id": idx_o, "subarray_id": idx_o, "label": sub_label, "tilt_deg": sub_tilt, "azimuth_deg": sub_azim,
                "pitch_m": sub_pitch, "collector_width_m": sub_cw, "gcr": round(sub_cw / sub_pitch, 4), "shed_count": sub_sheds,
                "inverter_count": sni, "mppt_count": snp, "string_count": sns, "module_count": sub_mods,
                "share_pct": round((sub_mods / total_sub_mods) * 100.0, 2) if total_sub_mods > 0 else 0.0,
            })
    else:
        orientations_list = [orient_spec]
        sub_mppt = n_inv_mppt if n_inv_mppt is not None else (n_string_ch if independent_mppt else 1)
        subarrays = [_subarray_entry(1, "PV Array", n_mod_serie, n_string_ch, n_inverter, sub_mppt)]

    result = synthesize_project_and_variant(
        store, project_comment=project_comment, variant_comment=variant_comment, site_name=site_name, met_name=met_name,
        orientations=orientations_list, subarrays=subarrays, pan_name=pan_name, ond_name=ond_name, u_factor=u_factor,
        shading_lines=shading_lines, version=version, with_bom=with_bom, allow_missing_met=allow_missing_met,
    )
    effective_gcr_width = (2.0 * resolved_collector_width) if field_type == "Dome" else resolved_collector_width
    result["utility_summary"] = {
        "field_type": field_type, "include_3d_scene": need_3d, "pitch_m": resolved_pitch, "collector_width_m": resolved_collector_width,
        "gcr": round(effective_gcr_width / resolved_pitch, 4), "shed_count": shed_count, "module_rows": n_rows, "module_columns": n_cols,
        "module_orient": module_orient, "module_width_m": mw, "module_length_m": ml, "module_dimension_source": dim_source,
        "bifacial_enabled": bool(bifacial and bifacial.get("enabled", True)), "base_slope_deg": eff_base_slope, "obstacle_count": len(obstacles or ()),
        **({"dome": dome_meta} if dome_meta is not None else {}),
        **({"sub_orientations": sub_orient_summaries} if sub_orient_summaries is not None else {}),
        **({"ground_3d_layout": ground_3d_layout} if ground_3d_layout is not None else {}),
        **({"terrain": terrain_summary} if terrain_summary is not None else {}),
    }
    return result
