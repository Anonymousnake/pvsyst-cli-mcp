"""Derive one standard workspace template from the installed PVsyst 8.1 demo seed.

The seed is the read-only ``_DEMO_Residential`` project inside the DataRO library that
sits next to the configured PVsystCLI. Nothing is guessed: the derived variant only
declares the counts PVsyst itself serialized as implicit 1 on its single exclusive
inverter/string branch, then switches the verified independent-MPPT mode and scales the
full homogeneous hardware MPPT allocation to the declared target OND input count. Loss
parameters, native defaults and the physical topology stay untouched, the installed seed
is never written, and no other workspace file is edited.

A caller that supplies ``capacity`` gets a ``plan_capacity`` selection computed on the
in-memory derived text, so a seed can be judged before anything is written. The PRJ/VC0
text itself is returned only by :func:`standard_seed_texts`, never by
:func:`prepare_standard_template`, because tool results must not carry the full template.
Those texts keep their leading U+FEFF marker when the seed had one, so encoding them back
to UTF-8 reproduces the installed byte-level convention; parsing still uses the
BOM-tolerant adapter decoders.
"""
from __future__ import annotations

from pathlib import Path
import re

from pvsyst_auto.native.cli import PVsystError
from pvsyst_auto.components.editor import root_block
from pvsyst_auto.project.circuit import circuit_nodes, inventory as grid_inventory, positive_integer, resize_capacity, set_independent_mppt
from pvsyst_auto.components.pan import validation_fields as pan_validation_fields
from pvsyst_auto.project.paths import project_path
from pvsyst_auto.engineering.sizing import plan_capacity

SEED_STEM = "_DEMO_Residential"
SEED_VARIANT = "VC0"
SEED_SUBARRAY = 1
SEED_VERSIONS = ("8.1.0", "8.1.6")
DATA_RO_FOLDER = re.compile(r"PVsyst8\.1(?:\.\d+)?_Data", re.I)
CLI_VERSION = re.compile(r"\b8\.1(?:\.\d+)?\b")
MAX_MPPT_INPUTS = 64
INDEPENDENT_MPPT_BIT = 0x400
RETAINED_FIELDS = ("NModSerie", "LIDLoss", "PMismMPP", "PQualityModU", "PQualityModI",
                   "PQualityModP", "PMismUFixe", "PNomAC_InMPPT")
SCOPE = ("New-only construction from the installed demo seed; counts and references are "
         "declared, not physically certified")


def _issue(result: dict, field: str, message) -> None:
    result["issues"].append({"field": field, "message": str(message)})


def _workspace_entries(store) -> list:
    """Existing project members, or none when the workspace has no readable Projects folder."""
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
        raise ValueError(f"The installed CLI is not an 8.1 release ({version!r}); "
                         "the installed 8.1 seed is not replaced by another release")
    return {"version": version, "commands": sorted(capabilities)}


def _locate_seed(cli) -> tuple[Path, Path]:
    """The only accepted seed source: the unique 8.1 DataRO library next to the CLI."""
    install = Path(cli.cli)
    if not install.is_file():
        raise ValueError("The configured PVsystCLI path is not an existing file")
    data_ro = install.parent / "DataRO"
    if not data_ro.is_dir():
        raise ValueError("The installed CLI has no DataRO library next to it")
    libraries = sorted((path for path in data_ro.iterdir()
                        if path.is_dir() and DATA_RO_FOLDER.fullmatch(path.name)),
                       key=lambda path: path.name.casefold())
    if not libraries:
        installed = sorted(path.name for path in data_ro.iterdir()
                           if path.is_dir() and re.fullmatch(r"PVsyst[0-9.]+_Data", path.name, re.I))
        raise ValueError("No installed PVsyst8.1 DataRO library was found"
                         + (f" (installed: {', '.join(installed)})" if installed else "")
                         + "; another release's seed is not substituted")
    if len(libraries) > 1:
        raise ValueError("Multiple installed PVsyst8.1 DataRO libraries; the seed source is ambiguous")
    projects = libraries[0] / "Projects"
    project = projects / f"{SEED_STEM}.PRJ"
    variant = projects / f"{SEED_STEM}.{SEED_VARIANT}"
    if not project.is_file() or not variant.is_file():
        raise ValueError(f"The installed 8.1 library has no complete "
                         f"{SEED_STEM}.PRJ/{SEED_STEM}.{SEED_VARIANT} seed")
    return project, variant


def _mppt_inputs(ond_electrical) -> int:
    """The target inverter's declared hardware MPPT count, 1..64; never inferred."""
    if not isinstance(ond_electrical, dict):
        raise ValueError("Provide the resolved target OND electrical inventory")
    item = (ond_electrical.get("fields") or {}).get("NbMPPT")
    if not isinstance(item, dict) or item.get("status") != "declared":
        raise ValueError("The target OND must declare NbMPPT; no hardware input count is inferred")
    value = item.get("value")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_MPPT_INPUTS:
        raise ValueError(f"The target OND must declare an integer NbMPPT in 1..{MAX_MPPT_INPUTS}")
    return value


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _field_value(store, lines, bounds, key) -> str:
    indices = store._field_indices(lines, bounds, key)
    if len(indices) != 1:
        raise ValueError(f"The seed subarray must declare one {key}")
    return lines[indices[0]].split("=", 1)[1].strip()


def _explicit_line(lines: list[str], node) -> str:
    """One NElements row with the node's own indentation and line ending."""
    start = lines[node.line - 1]
    indent = re.match(r"[ \t]*", start).group(0)
    ending = start[len(start.rstrip("\r\n")):] or "\n"
    return f"{indent}  NElements=1{ending}"


def _branch_nodes(lines, array_id: int):
    groups = [node for top in circuit_nodes(lines) for node in _walk(top)
              if node.kind == "InverterNode" and node.one("SubArrayId") == str(array_id)]
    return groups


def _declare_explicit_counts(lines: list[str], array_id: int) -> list[dict]:
    """Declare the implicit NElements=1 of the one exclusive inverter/string branch.

    Rows are inserted deepest first so the remaining 1-based node lines stay valid, the
    branch is re-read afterwards, and a node that already serializes another count stops
    the construction instead of silently changing the layout the caller verified.
    """
    groups = _branch_nodes(lines, array_id)
    if len(groups) != 1:
        raise ValueError("The seed must have one exclusive inverter branch for the selected subarray")
    group = groups[0]
    if not group.children or any(child.kind != "MPPTNode" for child in group.children):
        raise ValueError("The seed must have direct explicit MPPT children only")
    leaves = [leaf for port in group.children for leaf in port.children]
    if (any(port.one("SubArrayId") != str(array_id) for port in group.children)
            or any(leaf.one("SubArrayId") != str(array_id) for leaf in leaves)):
        raise ValueError("The seed inverter/MPPT branch must belong to the selected subarray only")
    if any(leaf.kind != "StringNode" or leaf.children for leaf in leaves):
        raise ValueError("The seed must have one empty parallel-string leaf per MPPT input")
    pending = []
    for node in (group, *leaves):
        declared = node.fields.get("NElements")
        if declared is None:
            pending.append(node)
        elif declared != ["1"]:
            raise ValueError(f"Circuit line {node.line}: {node.kind} already declares "
                             f"NElements={declared[0]}; implicit single counts are not rewritten")
    added = []
    for node in sorted(pending, key=lambda item: item.line, reverse=True):
        lines.insert(node.line, _explicit_line(lines, node))
        # Each later insertion lands above this row and pushes it down by one line.
        above = sum(other.line < node.line for other in pending)
        added.append({"object": "pvCircuit", "node": node.kind, "field": "NElements", "value": "1",
                      "line": node.line + 1 + above,
                      "reason": "Declare the single serialized branch explicitly for guarded resizing"})
    if added:
        checked = _branch_nodes(lines, array_id)
        if len(checked) != 1:
            raise ValueError("Declaring explicit counts changed the exclusive inverter branch count")
        group = checked[0]
        for node in (group, *[leaf for port in group.children for leaf in port.children]):
            if node.fields.get("NElements") != ["1"]:
                raise ValueError(f"Circuit line {node.line}: {node.kind} must declare NElements=1 "
                                 "after the insertion")
    return added


def _component_path(store, kind: str, name: str) -> Path:
    path = store.components._path(kind, name)
    if not path.is_file() and store.components.builtin:
        path = store.components._path(kind, name, "builtin")
    if not path.is_file():
        raise FileNotFoundError(f"Component {name} must exist as a loose file")
    return path


def standard_seed_texts(cli, store, *, ond_electrical, pan_name, ond_name,
                        capacity=None, pan_fields=None, allow_window_creation=None) -> dict:
    """Build the derived PRJ/VC0 text in memory; nothing is written and the seed is untouched.

    ``prj_text`` and ``vc0_text`` are decoded as plain UTF-8 so a seed UTF-8 BOM survives
    as a leading U+FEFF in the returned string, while all structure checks still run on the
    BOM-tolerant decoders. ``source`` reports the two markers separately as
    ``project_bom_preserved`` and ``variant_bom_preserved``; no combined flag is emitted
    because the two seed members can differ.
    """
    profile = _cli_profile(cli, allow_window_creation=allow_window_creation)
    seed_project, seed_variant = _locate_seed(cli)
    project_data = store._read(seed_project)
    store._project_text(project_data)
    variant_data = store._read(seed_variant)
    project_bom = project_data.startswith(b"\xef\xbb\xbf")
    variant_bom = variant_data.startswith(b"\xef\xbb\xbf")
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
    child = next((index for index in range(1, len(lines)) if lines[index].strip().startswith("PVObject_")), None)
    if child is None:
        raise ValueError("The seed variant has no child PVObject block")
    corrected = store._field_indices(lines, (0, child), "PRCorrTemp")
    if len(corrected) != 1 or lines[corrected[0]].split("=", 1)[1].strip() != "False":
        raise ValueError("The seed variant must explicitly declare PRCorrTemp=False")
    bounds = arrays[SEED_SUBARRAY]
    version = _field_value(store, lines, bounds, "Version")
    flags_before = _field_value(store, lines, bounds, "Flags")
    strings_before = _field_value(store, lines, bounds, "NStringCh")
    mppt_before = _field_value(store, lines, bounds, "NInvMPPT")
    module_before = _field_value(store, lines, bounds, "PVModule")
    inverter_before = _field_value(store, lines, bounds, "GInverter")
    if _field_value(store, lines, bounds, "SubArrayId") != str(SEED_SUBARRAY):
        raise ValueError("The seed subarray identifier does not match its section")
    if version not in SEED_VERSIONS:
        raise ValueError("The seed subarray must declare Version=8.1.0/8.1.6")
    if (not re.fullmatch(r"\$[0-9A-Fa-f]{1,8}", flags_before)
            or int(flags_before[1:], 16) & INDEPENDENT_MPPT_BIT):
        raise ValueError("The seed subarray must be in ordinary NInvMPPT=1 mode with explicit Flags")
    if mppt_before != "1":
        raise ValueError("The seed subarray must declare ordinary NInvMPPT=1")
    if _field_value(store, lines, bounds, "PowerSharingInverterId") != "-1":
        raise ValueError("The seed subarray must declare unshared PowerSharingInverterId=-1")
    if _field_value(store, lines, bounds, "NStrOrient1") != strings_before:
        raise ValueError("The seed subarray must keep all strings in one orientation")
    block = root_block(lines, bounds)
    if any(key.startswith("NStrOrient") and key != "NStrOrient1" and block.one(key)[1] != "0"
           for key in block.fields):
        raise ValueError("The seed subarray must keep all strings in one orientation")
    orientation = positive_integer(block.one("NoOrientation")[1], "NoOrientation")
    store._ordinary_fixed_plane(lines, store._orientation(lines, orientation))
    mppt_inputs = _mppt_inputs(ond_electrical)

    changes = _declare_explicit_counts(lines, SEED_SUBARRAY)
    arrays, _ = store._sections(lines)
    mode = set_independent_mppt(lines, arrays, SEED_SUBARRAY, True)
    scaled = resize_capacity(lines, arrays, SEED_SUBARRAY, target_mppt=mppt_inputs)
    arrays, _ = store._sections(lines)
    checked = grid_inventory(lines, arrays)
    if checked["counts_valid"] is not True:
        raise ValueError("The derived seed has incomplete or inconsistent grid counts: "
                         + "; ".join(checked["issues"] + checked["unchecked_checks"]))
    row = next(item for item in checked["subarrays"] if item["id"] == SEED_SUBARRAY)
    if (row["independent_mppt_inputs_declared"] != mppt_inputs
            or row["explicit_mppt_inputs_match_declared"] is not True):
        raise ValueError("The derived seed does not declare the target OND input count exactly")
    flags_after = _field_value(store, lines, arrays[SEED_SUBARRAY], "Flags")
    strings_after = _field_value(store, lines, arrays[SEED_SUBARRAY], "NStringCh")
    block = root_block(lines, arrays[SEED_SUBARRAY])
    port = (scaled["mppt_groups"] or [{}])[0]

    # The derived VC is read as Windows-1252 when BOM-less, so a non-ASCII body forces the BOM too.
    body = "".join(lines)
    data = (b"\xef\xbb\xbf" if variant_bom or not body.isascii() else b"") + body.encode("utf-8")
    store._require_no_geometry(store._text(data)[1], require_shading_section=True)
    final = store._replace(data, {"PAN": pan_name, "OND": ond_name}, SEED_SUBARRAY)
    _, final_lines = store._text(final)
    final_arrays, _ = store._sections(final_lines)
    rechecked = grid_inventory(final_lines, final_arrays)
    if rechecked["counts_valid"] is not True:
        raise ValueError("The derived seed lost complete counts after the component references: "
                         + "; ".join(rechecked["issues"] + rechecked["unchecked_checks"]))
    store._ordinary_fixed_plane(final_lines, store._orientation(final_lines, orientation))

    plan = None
    if capacity is not None:
        if pan_fields is None:
            pan_fields = pan_validation_fields(
                store.components._read(_component_path(store, "PAN", pan_name)).decode("utf-8-sig"))
        plan = plan_capacity(store, final_lines, SEED_SUBARRAY, pan_fields, ond_electrical, capacity)

    changes.extend([
        {"object": "pvSubArray", "field": "Flags", "before": flags_before, "after": flags_after,
         "reason": "Switch to the verified independent-MPPT mode"},
        {"object": "pvSubArray", "field": "NInvMPPT", "before": mode["inputs_before"],
         "after": mode["inputs_after"], "reason": "Declare the exclusive explicit MPPT inputs"},
        {"object": "pvSubArray", "field": "NInvMPPT", "before": scaled["mppt_inputs_before"],
         "after": scaled["mppt_inputs_after"], "reason": "Populate every declared hardware MPPT input"},
        {"object": "pvSubArray", "field": "NStringCh", "before": scaled["strings_before"],
         "after": scaled["strings_after"], "reason": "Keep the proportional string allocation integral"},
        {"object": "pvSubArray", "field": "NStrOrient1", "before": strings_before,
         "after": strings_after, "reason": "All strings stay in the common fixed plane"},
        {"object": "pvCircuit", "field": "StringNode NElements", "before": 1, "after": 1,
         "reason": "Parallel-string leaves stay a single explicit serialized branch"},
        {"object": "pvCircuit", "field": "MPPTNode NElements", "before": port.get("before"),
         "after": port.get("after"), "reason": "Scale the repeatable MPPT branch to the declared input count"},
        {"object": "pvSubArray", "field": "PVModule/GInverter",
         "before": f"{module_before}/{inverter_before}", "after": f"{pan_name}/{ond_name}",
         "reason": "Reference the resolved loose components"},
    ])
    retained = [{"field": "PRCorrTemp", "value": "False",
                 "reason": "Standard annual-PR requirement is retained from the seed"}]
    retained.extend({"field": key, "value": block.one(key)[1],
                     "reason": "Retained from the demo seed; no loss or native default was fitted"}
                    for key in RETAINED_FIELDS if key in block.fields)
    shading = [index for index, line in enumerate(final_lines)
               if line.strip() == "PVObject_Ombrage=pvShading"]
    shading_end = next((index for index in range(shading[0] + 1, len(final_lines))
                        if final_lines[index].strip() == "End of PVObject pvShading"), None) if shading else None
    if shading_end is not None:
        retained.append({"field": "pvShading Flags",
                         "value": _field_value(store, final_lines, (shading[0], shading_end), "Flags"),
                         "reason": "The empty shading declaration is retained; no geometry was added"})
    return {"prj_text": project_data.decode("utf-8"), "vc0_text": final.decode("utf-8"),
            "profile": profile, "mppt_inputs": mppt_inputs, "capacity_plan": plan,
            "source": {"library": str(seed_project.parent), "project": seed_project.name,
                       "variant": seed_variant.name, "subarray": SEED_SUBARRAY,
                       "orientation": orientation, "version": version,
                       "flags_before": flags_before, "flags_after": flags_after,
                       "mppt_inputs_before": int(mppt_before), "strings_before": strings_before,
                       "strings_after": strings_after,
                       "project_bom_preserved": project_bom,
                       "variant_bom_preserved": variant_bom},
            "changes": changes, "retained_defaults": retained, "read_only_seed": True,
            "warnings": [
                "The seed variant's historical BatchSimul ParamsFileName reference is retained; this "
                "preparation passes no batch parameters and that reference's effect is not verified.",
                "The PRJ Comment, MeteoFileName and the embedded site/meteo snapshot come from the "
                "installed demo seed; the destination workspace is not checked for that MET file here.",
                "Explicit NElements=1 rows only declare counts PVsyst already serialized as implicit 1 on "
                "the one exclusive inverter/string branch; no physical object, loss or native default was "
                "added, removed or refitted.",
                "The derived texts are validated through the adapter's own parsers and count checks; native "
                "PVsyst structural and simulation validation remains outstanding.",
            ]}


def prepare_standard_template(cli, store, *, ond_electrical, pan_name, ond_name, new_project,
                              dry_run=True, capacity=None, pan_fields=None,
                              allow_window_creation=None) -> dict:
    """Create a new workspace PRJ/VC0 from the installed 8.1 demo seed, or preview it.

    ``dry_run`` previews the derived structure and, with ``capacity`` and optional
    ``pan_fields``, the selected capacity without writing anything. ``dry_run=False`` writes
    exactly one PRJ/VC0 pair through the existing new-only project creation; an existing
    destination is blocked, the seed stays untouched, and nothing created here is silently
    removed afterwards.

    status is blocked (nothing was written), ready (dry-run preview), created (the pair was
    written) or failed (a creation-stage interface error with the stage retained).
    """
    with store.lock:
        return _prepare(cli, store, ond_electrical, pan_name, ond_name, new_project,
                        dry_run, capacity, pan_fields, allow_window_creation)


def _prepare(cli, store, ond_electrical, pan_name, ond_name, new_project, dry_run, capacity, pan_fields,
             allow_window_creation=None):
    result = {"status": "blocked", "stage": "preflight", "dry_run": dry_run, "scope": SCOPE,
              "request": {"project": new_project, "module": pan_name, "inverter": ond_name,
                          "capacity": capacity is not None},
              "source": {}, "definition": {}, "changes": [], "retained_defaults": [],
              "issues": [], "warnings": [], "created": None, "capacity_plan": None,
              "seed_preserved": None, "native_verification_required": True}

    if type(dry_run) is not bool:
        _issue(result, "dry_run", "Use a strict boolean")
    for field, value in (("new_project", new_project), ("pan_name", pan_name), ("ond_name", ond_name)):
        if not isinstance(value, str) or not value:
            _issue(result, field, "Required input is missing")
    if result["issues"]:
        return result

    def guard():
        project_path(cli.workspace, new_project)
        if Path(store.workspace) != Path(cli.workspace):
            raise ValueError("The store and the CLI must share one resolved workspace")
        if any(store._belongs_to_project(path, new_project) for path in _workspace_entries(store)):
            raise FileExistsError("The destination project already exists; no file is overwritten")

    try:
        guard()
    except (ValueError, OSError) as exc:
        _issue(result, "new_project", exc)
        return result

    try:
        derived = standard_seed_texts(cli, store, ond_electrical=ond_electrical, pan_name=pan_name,
                                      ond_name=ond_name, capacity=capacity, pan_fields=pan_fields,
                                      allow_window_creation=allow_window_creation)
    except (ValueError, OSError, KeyError, PVsystError, StopIteration) as exc:
        _issue(result, "seed", cli._redact(str(exc)))
        return result

    result.update({"stage": "derived", "source": derived["source"], "profile": derived["profile"],
                   "mppt_inputs": derived["mppt_inputs"], "capacity_plan": derived["capacity_plan"],
                   "changes": derived["changes"], "retained_defaults": derived["retained_defaults"],
                   "warnings": list(derived["warnings"]),
                   "definition": {"variant": SEED_VARIANT, "subarray": SEED_SUBARRAY,
                                  "mppt_inputs": derived["mppt_inputs"],
                                  "strings": derived["source"]["strings_after"],
                                  "changes": len(derived["changes"]),
                                  "retained_fields": len(derived["retained_defaults"])}})
    if capacity is not None and derived["capacity_plan"]["status"] != "ready":
        result["issues"].extend(derived["capacity_plan"]["issues"])
        result["stage"] = "capacity"
        return result
    if dry_run:
        result["status"] = "ready"
        return result

    result["stage"] = "create"
    try:
        created = store.create_project(new_project, derived["prj_text"],
                                       {SEED_VARIANT: derived["vc0_text"]})
    except FileExistsError as exc:
        _issue(result, "new_project", f"{exc}; no file was overwritten")
        result["stage"] = "conflict"
        return result
    except (ValueError, OSError) as exc:
        result.update(status="failed", error=cli._redact(str(exc))[-12000:])
        return result
    result.update(status="created", stage="created", created=created, files=created["files"],
                  template_project=created["project"], variant=SEED_VARIANT)
    return result
